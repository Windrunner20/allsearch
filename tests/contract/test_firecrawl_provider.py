from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from allsearch.config import FirecrawlConfig
from allsearch.providers.firecrawl import FirecrawlProvider
from allsearch.transport import HttpTransport

FIXTURE = Path(__file__).parent / "fixtures" / "firecrawl_scrape.json"


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_scrape_contract():
    cfg = FirecrawlConfig(
        api_key="fc-test",
        base_url="https://api.firecrawl.dev",
        scrape_path="/v2/scrape",
        only_main_content=True,
        max_pages=3,
    )
    route = respx.post("https://api.firecrawl.dev/v2/scrape").mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text()))
    )
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = FirecrawlProvider(cfg, transport)
    result = await provider.scrape("https://example.com/page")
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {
        "url": "https://example.com/page",
        "formats": ["markdown"],
        "onlyMainContent": True,
    }
    assert "Bearer fc-test" in route.calls.last.request.headers.get("Authorization", "")
    assert result.title == "Title"
    assert "Main article" in result.content
    await transport.aclose()
