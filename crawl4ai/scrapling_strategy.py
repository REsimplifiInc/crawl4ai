"""Crawl4AI crawler strategy backed by Scrapling's stealth browser.

The integration deliberately sits at the crawler-strategy boundary. Scrapling
owns browser startup and anti-bot behavior, while ``AsyncWebCrawler`` keeps
ownership of HTML processing, extraction, and markdown generation.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import re
import tempfile
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Any

from .async_configs import BrowserConfig, CrawlerRunConfig
from .async_crawler_strategy import AsyncCrawlerStrategy
from .async_logger import AsyncLogger
from .models import AsyncCrawlResponse

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([^;\s\"']+)", re.IGNORECASE)
_FULL_PAGE_SCROLL_JS = """
async ({max_scroll_steps, scroll_delay}) => {
    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    let previousHeight = 0;
    for (let step = 0; step < max_scroll_steps; step += 1) {
        const height = Math.max(
            document.body?.scrollHeight || 0,
            document.documentElement?.scrollHeight || 0,
        );
        window.scrollTo(0, height);
        await pause(scroll_delay * 1000);
        const nextHeight = Math.max(
            document.body?.scrollHeight || 0,
            document.documentElement?.scrollHeight || 0,
        );
        if (nextHeight <= previousHeight && window.innerHeight + window.scrollY >= nextHeight) {
            break;
        }
        previousHeight = nextHeight;
    }
}
"""
_LEGACY_DEFAULT_CHROMIUM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36"
)


class AsyncScraplingCrawlerStrategy(AsyncCrawlerStrategy):
    """Crawl4AI strategy that fetches pages through Scrapling's stealth session.

    Scrapling remains optional. Importing this class does not import Scrapling;
    the dependency is loaded only when the strategy starts, which preserves the
    normal Crawl4AI installation and default Playwright path.
    """

    def __init__(
        self,
        browser_config: BrowserConfig | None = None,
        logger: AsyncLogger | None = None,
        session_factory: Callable[..., Any] | None = None,
        scrapling_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.browser_config = browser_config or BrowserConfig.from_kwargs(kwargs)
        self.logger = logger or AsyncLogger(verbose=self.browser_config.verbose)
        self.session_factory = session_factory
        self.scrapling_options = dict(scrapling_options or {})
        self._session: Any | None = None
        self._session_identity: tuple[str | None, str | None] | None = None

    async def __aenter__(self) -> Any:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(
        self,
        *,
        locale: str | None = None,
        timezone_id: str | None = None,
    ) -> None:
        """Start one reusable Scrapling browser session."""
        if self._session is not None:
            return

        factory = self.session_factory or self._load_session_factory()
        identity = self._resolve_session_identity(locale, timezone_id)
        session_kwargs = self._build_session_kwargs(
            locale=identity[0], timezone_id=identity[1]
        )
        session = factory(**session_kwargs)
        try:
            start = getattr(session, "start", None)
            if callable(start):
                result = start()
                if inspect.isawaitable(result):
                    await result
        except Exception:
            close = getattr(session, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
            raise
        self._session = session
        self._session_identity = identity

    async def close(self) -> None:
        """Close the Scrapling browser session and release its resources."""
        session, self._session = self._session, None
        self._session_identity = None
        if session is None:
            return
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def kill_session(self, session_id: str) -> None:
        """Reject Crawl4AI page-session cleanup because state is not reusable."""
        raise ValueError(
            "AsyncScraplingCrawlerStrategy does not support Crawl4AI session_id reuse; "
            "combine page actions into one crawl or use another browser mode."
        )

    def update_user_agent(self, user_agent: str) -> None:
        """Set the user agent before the Scrapling session is started."""
        if self._session is not None and user_agent != self.browser_config.user_agent:
            raise ValueError(
                "Set BrowserConfig.user_agent before starting "
                "AsyncScraplingCrawlerStrategy; Scrapling fixes it per browser context."
            )
        self.browser_config.user_agent = user_agent

    async def crawl(
        self,
        url: str,
        config: CrawlerRunConfig | None = None,
        **kwargs: Any,
    ) -> AsyncCrawlResponse:
        """Fetch one URL and return the response shape expected by Crawl4AI."""
        config = config or CrawlerRunConfig.from_kwargs(kwargs)
        self._validate_run_config(config)

        is_raw_url = url.startswith(("raw://", "raw:"))
        is_file_url = url.startswith("file://")
        if is_raw_url and not self._needs_browser_processing(config):
            html = url[6:] if url.startswith("raw://") else url[4:]
            return AsyncCrawlResponse(
                html=html,
                response_headers={},
                status_code=200,
                redirected_url=getattr(config, "base_url", None),
            )

        if is_file_url and not self._needs_browser_processing(config):
            html = await asyncio.to_thread(
                Path(url[7:]).read_text,
                encoding="utf-8",
            )
            return AsyncCrawlResponse(
                html=html,
                response_headers={},
                status_code=200,
                redirected_url=getattr(config, "base_url", None),
            )

        if not (url.startswith(("http://", "https://")) or is_raw_url or is_file_url):
            raise ValueError(
                "URL must start with 'http://', 'https://', 'file://', or 'raw:'"
            )

        await self._ensure_session(config)
        assert self._session is not None

        captures: dict[str, Any] = {}
        fetch_kwargs = self._build_fetch_kwargs(config, captures)
        browser_url = url
        temporary_path: Path | None = None
        if is_raw_url:
            raw_html = url[6:] if url.startswith("raw://") else url[4:]
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".html", delete=False
            ) as temporary_file:
                temporary_file.write(raw_html)
            temporary_path = Path(temporary_file.name)
            browser_url = temporary_path.as_uri()

        try:
            response = await self._session.fetch(browser_url, **fetch_kwargs)
            if page_action_error := captures.get("page_action_error"):
                raise RuntimeError(
                    f"Scrapling page action failed: {page_action_error}"
                ) from page_action_error
            crawl_response = self._response_to_crawl4ai(response, captures)
            if is_raw_url or is_file_url:
                crawl_response.redirected_url = getattr(config, "base_url", None)
            return crawl_response
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _load_session_factory() -> Callable[..., Any]:
        try:
            module = import_module("scrapling.fetchers")
            return module.AsyncStealthySession
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "Scrapling stealth support is optional; install the Crawl4AI extra "
                "with `pip install crawl4ai[scrapling]` (or `scrapling[fetchers]`)."
            ) from exc

    def _build_session_kwargs(
        self,
        *,
        locale: str | None = None,
        timezone_id: str | None = None,
    ) -> dict[str, Any]:
        config = self.browser_config
        kwargs: dict[str, Any] = {
            "headless": config.headless,
            "cookies": config.cookies,
            "extra_headers": config.headers,
            "user_data_dir": config.user_data_dir,
            "cdp_url": config.cdp_url,
            "locale": locale,
            "timezone_id": timezone_id,
        }
        if (
            config.user_agent
            and config.user_agent != _LEGACY_DEFAULT_CHROMIUM_USER_AGENT
        ):
            kwargs["useragent"] = config.user_agent
        proxy = self._proxy_value(config.proxy_config)
        if proxy is not None:
            kwargs["proxy"] = proxy
        kwargs.update(self.scrapling_options)
        return {key: value for key, value in kwargs.items() if value is not None}

    def _build_fetch_kwargs(
        self, config: CrawlerRunConfig, captures: dict[str, Any]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timeout": config.page_timeout,
            "wait": max(0, int((config.delay_before_return_html or 0) * 1000)),
            "load_dom": True,
            "network_idle": config.wait_until == "networkidle",
        }

        wait_selector = self._css_wait_selector(config.wait_for)
        has_page_action = self._needs_page_action(config)
        if wait_selector is not None and not has_page_action:
            kwargs["wait_selector"] = wait_selector
        if (
            config.wait_for and (wait_selector is None or has_page_action)
        ) or has_page_action:
            kwargs["page_action"] = self._build_page_action(config, captures)
        if config.capture_network_requests or config.capture_console_messages:
            kwargs["page_setup"] = self._build_page_setup(config, captures)

        proxy = self._proxy_value(config.proxy_config)
        if proxy is not None:
            kwargs["proxy"] = proxy
        return kwargs

    def _build_page_action(
        self, config: CrawlerRunConfig, captures: dict[str, Any]
    ) -> Callable[..., Any]:
        async def page_action(page: Any) -> dict[str, Any]:
            result: dict[str, Any] = {}
            try:
                if config.js_code_before_wait:
                    result["before_wait"] = await self._evaluate_scripts(
                        page, config.js_code_before_wait
                    )
                if config.wait_for:
                    await self._wait_for_condition(
                        page,
                        config.wait_for,
                        config.wait_for_timeout or config.page_timeout,
                    )
                if config.js_code:
                    js_values = await self._evaluate_scripts(page, config.js_code)
                    result["js"] = js_values
                    scripts = (
                        config.js_code
                        if isinstance(config.js_code, list)
                        else [config.js_code]
                    )
                    values = (
                        js_values if isinstance(config.js_code, list) else [js_values]
                    )
                    captures["js_execution_result"] = {
                        "success": True,
                        "results": [
                            {"success": True, "result": value}
                            for _script, value in zip(scripts, values, strict=True)
                        ],
                    }
                if config.scan_full_page:
                    await page.evaluate(
                        _FULL_PAGE_SCROLL_JS,
                        {
                            "max_scroll_steps": config.max_scroll_steps or 10,
                            "scroll_delay": config.scroll_delay,
                        },
                    )
                if config.screenshot:
                    if config.screenshot_wait_for:
                        await asyncio.sleep(config.screenshot_wait_for)
                    captures["screenshot"] = await page.screenshot()
                if config.pdf:
                    captures["pdf"] = await page.pdf()
            except Exception as exc:
                captures["page_action_error"] = exc
                raise
            return result

        return page_action

    @staticmethod
    def _build_page_setup(
        config: CrawlerRunConfig, captures: dict[str, Any]
    ) -> Callable[..., Any]:
        async def page_setup(page: Any) -> None:
            if config.capture_network_requests:
                requests: list[dict[str, Any]] = []
                captures["network_requests"] = requests

                def on_request(request: Any) -> None:
                    requests.append(
                        {
                            "event_type": "request",
                            "url": request.url,
                            "method": request.method,
                        }
                    )

                async def on_response(response: Any) -> None:
                    requests.append(
                        {
                            "event_type": "response",
                            "url": response.url,
                            "status": response.status,
                            "headers": dict(response.headers),
                        }
                    )

                page.on("request", on_request)
                page.on("response", on_response)
            if config.capture_console_messages:
                messages: list[dict[str, Any]] = []
                captures["console_messages"] = messages

                def on_console(message: Any) -> None:
                    messages.append({"type": message.type, "text": message.text})

                page.on("console", on_console)

        return page_setup

    @staticmethod
    def _needs_page_action(config: CrawlerRunConfig) -> bool:
        return bool(
            config.js_code_before_wait
            or config.js_code
            or config.scan_full_page
            or config.screenshot
            or config.pdf
        )

    @staticmethod
    def _css_wait_selector(wait_for: str | None) -> str | None:
        if not wait_for:
            return None
        if wait_for.startswith("js:"):
            return None
        return wait_for.removeprefix("css:")

    @staticmethod
    async def _wait_for_condition(page: Any, condition: str, timeout_ms: int) -> None:
        if condition.startswith("js:"):
            await page.wait_for_function(condition[3:], timeout=timeout_ms)
            return
        await page.locator(condition.removeprefix("css:")).first.wait_for(
            state="attached", timeout=timeout_ms
        )

    @staticmethod
    async def _evaluate_scripts(page: Any, scripts: str | list[str]) -> Any:
        values: list[Any] = []
        for script in scripts if isinstance(scripts, list) else [scripts]:
            values.append(await page.evaluate(f"(async () => {{ {script} }})()"))
        return values[-1] if len(values) == 1 else values

    async def _ensure_session(self, config: CrawlerRunConfig) -> None:
        identity = self._resolve_session_identity(
            getattr(config, "locale", None), getattr(config, "timezone_id", None)
        )
        if self._session is None:
            await self.start(locale=identity[0], timezone_id=identity[1])
            return
        if self._session_identity != identity:
            raise ValueError(
                "Scrapling fixes locale and timezone_id per browser context; "
                "set them before the first crawl or use a separate strategy."
            )

    def _resolve_session_identity(
        self, locale: str | None, timezone_id: str | None
    ) -> tuple[str | None, str | None]:
        return (
            self.scrapling_options.get("locale") or locale,
            self.scrapling_options.get("timezone_id") or timezone_id,
        )

    @staticmethod
    def _needs_browser_processing(config: CrawlerRunConfig) -> bool:
        return any(
            bool(getattr(config, field, False))
            for field in (
                "process_in_browser",
                "screenshot",
                "pdf",
                "capture_mhtml",
                "js_code_before_wait",
                "js_code",
                "wait_for",
                "scan_full_page",
                "remove_overlay_elements",
                "remove_consent_popups",
                "simulate_user",
                "magic",
                "process_iframes",
                "capture_console_messages",
                "capture_network_requests",
                "virtual_scroll_config",
            )
        )

    @staticmethod
    def _proxy_value(proxy: Any) -> dict[str, str] | str | None:
        if proxy is None:
            return None
        if isinstance(proxy, dict):
            return {
                key: str(value) for key, value in proxy.items() if value is not None
            }
        values = {
            key: getattr(proxy, key, None) for key in ("server", "username", "password")
        }
        return {key: str(value) for key, value in values.items() if value is not None}

    @staticmethod
    def _decode_body(body: Any, headers: dict[str, str]) -> str:
        if isinstance(body, str):
            return body
        if isinstance(body, memoryview):
            body = body.tobytes()
        if not isinstance(body, bytes):
            return str(body or "")
        content_type = next(
            (value for key, value in headers.items() if key.lower() == "content-type"),
            "",
        )
        charset_match = _CHARSET_RE.search(content_type)
        encoding = charset_match.group(1) if charset_match else "utf-8"
        return body.decode(encoding, errors="replace")

    @classmethod
    def _response_to_crawl4ai(
        cls, response: Any, captures: dict[str, Any]
    ) -> AsyncCrawlResponse:
        headers = {
            str(key): str(value)
            for key, value in dict(getattr(response, "headers", {}) or {}).items()
        }
        body = getattr(response, "body", None)
        if callable(body):
            body = body()
        if inspect.isawaitable(body):
            raise TypeError("Scrapling response body must be available synchronously")
        if body is None:
            body = getattr(response, "html_content", None)
        if body is None or body == "None":
            body = getattr(response, "text", "")
        screenshot = captures.get("screenshot")
        if isinstance(screenshot, bytes):
            screenshot = base64.b64encode(screenshot).decode("ascii")
        return AsyncCrawlResponse(
            html=cls._decode_body(body, headers),
            response_headers=headers,
            js_execution_result=captures.get("js_execution_result"),
            status_code=int(
                getattr(response, "status", getattr(response, "status_code", 200)) or 0
            ),
            screenshot=screenshot,
            pdf_data=captures.get("pdf"),
            redirected_url=getattr(response, "url", None),
            redirected_status_code=getattr(response, "redirected_status_code", None),
            network_requests=captures.get("network_requests"),
            console_messages=captures.get("console_messages"),
        )

    @staticmethod
    def _validate_run_config(config: CrawlerRunConfig) -> None:
        if getattr(config, "session_id", None):
            raise ValueError(
                "AsyncScraplingCrawlerStrategy does not support Crawl4AI session_id reuse; "
                "combine page actions into one crawl or use another browser mode."
            )
        if getattr(config, "capture_mhtml", False):
            raise ValueError(
                "AsyncScraplingCrawlerStrategy does not support MHTML capture"
            )
