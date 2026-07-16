from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from allsearch.config import TavilyConfig
from allsearch.providers.tavily import TavilyProvider
from allsearch.transport import HttpTransport

FIXTURE = Path(__file__).parent / "fixtures" / "tavily_search.json"


@pytest.mark.asyncio
@respx.mock
async def test_tavily_request_contract():
    cfg = TavilyConfig(
        api_key="tvly-test",
        base_url="https://api.tavily.com",
        search_path="/search",
        default_depth="basic",
        max_results=8,
    )
    route = respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text()))
    )
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = TavilyProvider(cfg, transport)
    result = await provider.search("test query", max_results=5, topic="news", search_depth="advanced")
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["api_key"] == "tvly-test"
    assert body["include_answer"] is True
    assert body["include_raw_content"] is False
    assert body["search_depth"] == "advanced"
    assert body["topic"] == "news"
    assert result.answer == "Tavily short answer"
    assert len(result.results) == 2
    await transport.aclose()
