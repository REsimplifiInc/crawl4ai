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
    assert fetch_kwargs["wait_selector"] == ".listing"
    assert response.html == FakeScraplingResponse.body.decode()
    assert response.status_code == 200
    assert response.response_headers == FakeScraplingResponse.headers
    assert response.redirected_url == "https://example.test/final"


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
