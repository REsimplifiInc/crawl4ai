from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest

from crawl4ai import (
    AsyncScraplingCrawlerStrategy,
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
)


class FakeScraplingResponse:
    body = (
        "<html><body><h1>Stealth listing</h1>"
        + ("<p>Listing details and market information.</p>" * 80)
        + "</body></html>"
    ).encode()
    status = 200
    headers: ClassVar[dict[str, str]] = {
        "content-type": "text/html; charset=utf-8",
        "x-source": "fake",
    }
    url = "https://example.test/final"


class FakeSession:
    instances: ClassVar[list[FakeSession]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.fetch_calls: list[tuple[str, dict]] = []
        type(self).instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def fetch(self, url: str, **kwargs):
        self.fetch_calls.append((url, kwargs))
        return FakeScraplingResponse()


class RecordingPage:
    def __init__(self):
        self.events: list[str] = []

    async def wait_for_function(self, _condition, timeout):
        self.events.append(f"wait_function:{timeout}")

    def locator(self, selector):
        page = self

        class Locator:
            def __init__(self):
                self.first = self

            async def wait_for(self, *, state, timeout):
                page.events.append(f"wait_selector:{selector}:{state}:{timeout}")

        return Locator()

    async def evaluate(self, script, *_args):
        self.events.append("evaluate")
        if "document.title" in script:
            return "Stealth listing"
        return None

    async def screenshot(self):
        self.events.append("screenshot")
        return b"screenshot"

    async def pdf(self):
        self.events.append("pdf")
        return b"pdf"


@pytest.fixture(autouse=True)
def reset_fake_sessions():
    FakeSession.instances.clear()
    yield
    FakeSession.instances.clear()


@pytest.mark.asyncio
async def test_scrapling_strategy_maps_browser_and_fetch_config_and_response():
    browser_config = BrowserConfig(
        headless=True,
        proxy_config={
            "server": "http://proxy.test:8080",
            "username": "u",
            "password": "p",
        },
        user_data_dir="/tmp/scrapling-profile",
        user_agent="Mozilla/5.0 custom",
        headers={"X-Test": "yes"},
    )
    strategy = AsyncScraplingCrawlerStrategy(
        browser_config=browser_config,
        session_factory=FakeSession,
        scrapling_options={"solve_cloudflare": True, "hide_canvas": True},
    )

    await strategy.start()
    response = await strategy.crawl(
        "https://example.test/listing",
        CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=17_000,
            delay_before_return_html=1.25,
            wait_for="css:.listing",
        ),
    )
    await strategy.close()

    session = FakeSession.instances[0]
    assert session.started
    assert session.closed
    assert session.kwargs["headless"] is True
    assert session.kwargs["proxy"] == {
        "server": "http://proxy.test:8080",
        "username": "u",
        "password": "p",
    }
    assert session.kwargs["user_data_dir"] == "/tmp/scrapling-profile"
    assert session.kwargs["useragent"] == "Mozilla/5.0 custom"
    assert session.kwargs["extra_headers"] == {"X-Test": "yes"}
    assert session.kwargs["solve_cloudflare"] is True
    assert session.kwargs["hide_canvas"] is True

    url, fetch_kwargs = session.fetch_calls[0]
    assert url == "https://example.test/listing"
    assert fetch_kwargs["timeout"] == 17_000
    assert fetch_kwargs["wait"] == 1_250
    assert "wait_selector" not in fetch_kwargs
    assert callable(fetch_kwargs["page_action"])
    assert response.html == FakeScraplingResponse.body.decode()
    assert response.status_code == 200
    assert response.response_headers == FakeScraplingResponse.headers
    assert response.redirected_url == "https://example.test/final"


@pytest.mark.asyncio
async def test_scrapling_strategy_uses_run_identity_and_default_scrapling_user_agent():
    strategy = AsyncScraplingCrawlerStrategy(session_factory=FakeSession)

    await strategy.crawl(
        "https://example.test/listing",
        CrawlerRunConfig(locale="fr-FR", timezone_id="Europe/Paris"),
    )

    session = FakeSession.instances[0]
    assert session.kwargs["locale"] == "fr-FR"
    assert session.kwargs["timezone_id"] == "Europe/Paris"
    assert "useragent" not in session.kwargs


@pytest.mark.asyncio
async def test_scrapling_strategy_restarts_preflight_session_for_run_identity():
    strategy = AsyncScraplingCrawlerStrategy(session_factory=FakeSession)

    async with AsyncWebCrawler(
        crawler_strategy=strategy,
        config=BrowserConfig(),
    ) as crawler:
        await crawler.arun(
            "https://example.test/listing",
            config=CrawlerRunConfig(locale="fr-FR", timezone_id="Europe/Paris"),
        )

    assert len(FakeSession.instances) == 2
    assert FakeSession.instances[0].kwargs.get("locale") is None
    assert FakeSession.instances[1].kwargs["locale"] == "fr-FR"
    assert FakeSession.instances[1].kwargs["timezone_id"] == "Europe/Paris"


@pytest.mark.asyncio
async def test_scrapling_strategy_removes_stale_identity_headers_for_default_user_agent():
    strategy = AsyncScraplingCrawlerStrategy(
        browser_config=BrowserConfig(
            headers={"sec-ch-ua": '"Chromium";v="116"', "X-Test": "yes"}
        ),
        session_factory=FakeSession,
    )

    await strategy.start()

    assert FakeSession.instances[0].kwargs["extra_headers"] == {"X-Test": "yes"}


@pytest.mark.asyncio
async def test_scrapling_strategy_runs_wait_before_actions_and_returns_js_result():
    strategy = AsyncScraplingCrawlerStrategy(session_factory=FakeSession)
    captures = {}
    page = RecordingPage()
    action = strategy._build_page_action(
        CrawlerRunConfig(
            wait_for="css:.listing",
            js_code="return document.title;",
            screenshot=True,
        ),
        captures,
    )

    await action(page)

    assert page.events == [
        "wait_selector:.listing:attached:60000",
        "evaluate",
        "screenshot",
    ]
    assert captures["js_execution_result"] == {
        "success": True,
        "results": [{"success": True, "result": "Stealth listing"}],
    }
    assert captures["screenshot"] == b"screenshot"


@pytest.mark.asyncio
async def test_scrapling_strategy_browser_processes_raw_content():
    strategy = AsyncScraplingCrawlerStrategy(session_factory=FakeSession)

    response = await strategy.crawl(
        "raw:<html><body>dynamic</body></html>",
        CrawlerRunConfig(process_in_browser=True, base_url="https://example.test/"),
    )

    session = FakeSession.instances[0]
    assert session.fetch_calls[0][0].startswith("file://")
    assert response.redirected_url == "https://example.test/"


@pytest.mark.asyncio
async def test_scrapling_strategy_retries_failed_session_start():
    class RetryableSession(FakeSession):
        starts = 0

        async def start(self):
            type(self).starts += 1
            if type(self).starts == 1:
                raise RuntimeError("startup failed")
            await super().start()

    strategy = AsyncScraplingCrawlerStrategy(session_factory=RetryableSession)

    with pytest.raises(RuntimeError, match="startup failed"):
        await strategy.start()
    await strategy.start()

    assert RetryableSession.starts == 2


@pytest.mark.asyncio
async def test_scrapling_strategy_keeps_crawl4ai_html_processing():
    strategy = AsyncScraplingCrawlerStrategy(session_factory=FakeSession)

    async with AsyncWebCrawler(
        crawler_strategy=strategy,
        config=BrowserConfig(),
    ) as crawler:
        result = await crawler.arun(
            "https://example.test/listing",
            config=CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS, word_count_threshold=0
            ),
        )

    assert result.success
    assert result.status_code == 200
    assert "Stealth listing" in result.html
    assert result.markdown


@pytest.mark.asyncio
async def test_scrapling_strategy_reports_missing_optional_dependency():
    strategy = AsyncScraplingCrawlerStrategy()

    with (
        patch(
            "crawl4ai.scrapling_strategy.import_module",
            side_effect=ModuleNotFoundError("scrapling"),
        ),
        pytest.raises(RuntimeError, match=r"crawl4ai\[scrapling\]"),
    ):
        await strategy.start()


def test_scrapling_strategy_rejects_session_reuse():
    strategy = AsyncScraplingCrawlerStrategy(session_factory=FakeSession)

    with pytest.raises(ValueError, match="session_id"):
        strategy._validate_run_config(SimpleNamespace(session_id="session-1"))
