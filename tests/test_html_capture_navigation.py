import pytest
from playwright.async_api import Error
from patchright.async_api import Error as PatchrightError

from crawl4ai.async_crawler_strategy import get_page_content


class PageContentSequence:
    def __init__(self, *results):
        self.results = list(results)
        self.wait_for_load_state_calls = []

    async def content(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def wait_for_load_state(self, state, timeout):
        self.wait_for_load_state_calls.append((state, timeout))


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [Error, PatchrightError])
async def test_get_page_content_retries_transient_navigation_error(error_type):
    page = PageContentSequence(
        error_type(
            "Page.content: Unable to retrieve content because the page is navigating and changing the content."
        ),
        "<html>ready</html>",
    )

    assert await get_page_content(page, retry_delay=0) == "<html>ready</html>"
    assert page.wait_for_load_state_calls == [("domcontentloaded", 5000)]


@pytest.mark.asyncio
async def test_get_page_content_does_not_retry_unrelated_errors():
    page = PageContentSequence(Error("Target page, context or browser has been closed"))

    with pytest.raises(Error, match="browser has been closed"):
        await get_page_content(page, retry_delay=0)

    assert page.wait_for_load_state_calls == []
