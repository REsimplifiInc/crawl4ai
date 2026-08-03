import pytest

from crawl4ai import AsyncWebCrawler, BrowserConfig
from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.browser_manager import BrowserManager, _ScraplingRuntimeBackend


class FakeContext:
    async def close(self):
        self.closed = True


class FakeSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.context = FakeContext()
        self.started = False
        self.closed = False
        self.solver_calls = []
        self.__class__.instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def _cloudflare_solver(self, page):
        self.solver_calls.append(page)


def test_scrapling_runtime_uses_playwright_strategy_and_backend():
    config = BrowserConfig(
        browser_runtime="scrapling",
        scrapling_options={"solve_cloudflare": True},
    )

    crawler = AsyncWebCrawler(config=config)
    manager = BrowserManager(config)

    assert isinstance(crawler.crawler_strategy, AsyncPlaywrightCrawlerStrategy)
    assert isinstance(manager.runtime_backend, _ScraplingRuntimeBackend)
    assert config.use_persistent_context is True
    assert config.use_managed_browser is True


@pytest.mark.asyncio
async def test_scrapling_backend_owns_session_and_returns_context():
    config = BrowserConfig(
        browser_runtime="scrapling",
        scrapling_options={"solve_cloudflare": True},
    )
    manager = BrowserManager(config)
    manager.runtime_backend.session_factory = FakeSession

    context = await manager.runtime_backend.launch_persistent_context("/tmp/profile")

    session = FakeSession.instances[-1]
    assert context is session.context
    assert session.started is True
    assert session.kwargs["solve_cloudflare"] is True
    assert session.kwargs["user_data_dir"] == "/tmp/profile"

    await manager.runtime_backend.close()
    assert session.closed is True


@pytest.mark.asyncio
async def test_scrapling_backend_runs_cloudflare_solver_after_navigation():
    config = BrowserConfig(
        browser_runtime="scrapling",
        scrapling_options={"solve_cloudflare": True},
    )
    manager = BrowserManager(config)
    manager.runtime_backend.session_factory = FakeSession
    await manager.runtime_backend.launch_persistent_context("/tmp/profile")

    page = object()
    await manager.after_navigation(page, object())

    assert manager.runtime_backend.session.solver_calls == [page]
    await manager.runtime_backend.close()
