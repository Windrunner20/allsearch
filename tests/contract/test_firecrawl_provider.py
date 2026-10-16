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


def _provider(health=None):
    from allsearch.health import HealthRegistry

    cfg = FirecrawlConfig(
        api_key="fc-test",
        base_url="https://api.firecrawl.dev",
        scrape_path="/v2/scrape",
        only_main_content=True,
        max_pages=3,
    )
    health = health or HealthRegistry(failure_threshold=3)
    health.register("firecrawl", True)
    return FirecrawlProvider(cfg, HttpTransport(timeout_seconds=5, max_retries=2), health), health


@pytest.mark.asyncio
@respx.mock
async def test_unsupported_site_is_a_target_error_not_a_provider_failure():
    from allsearch.errors import TargetError

    route = respx.post("https://api.firecrawl.dev/v2/scrape").mock(
        return_value=httpx.Response(403, json={"success": False, "error": "we do not support this site"})
    )
    provider, health = _provider()
    for _ in range(5):
        with pytest.raises(TargetError) as info:
            await provider.scrape("https://www.reddit.com/r/x/comments/1/")
        assert info.value.code == "site_not_supported"
    assert route.call_count == 5  # no retries
    assert health.is_available("firecrawl")
    assert health.get("firecrawl").consecutive_failures == 0


@pytest.mark.asyncio
@respx.mock
async def test_per_url_failures_only_count_when_spread_over_distinct_hosts():
    from allsearch.errors import TargetError

    respx.post("https://api.firecrawl.dev/v2/scrape").mock(
        return_value=httpx.Response(500, json={"success": False, "error": "All scraping engines failed!"})
    )
    provider, health = _provider()
    for _ in range(4):
        with pytest.raises(TargetError):
            await provider.scrape("https://tieba.baidu.com/p/1")
    assert health.get("firecrawl").consecutive_failures == 0
    for host in ("a.example", "b.example", "c.example"):
        with pytest.raises(TargetError):
            await provider.scrape(f"https://{host}/x")
    # windows [tieba, a, b] and [a, b, c] each span three hosts
    assert health.get("firecrawl").consecutive_failures == 2


@pytest.mark.asyncio
@respx.mock
async def test_real_auth_failure_still_counts():
    from allsearch.errors import AuthError

    respx.post("https://api.firecrawl.dev/v2/scrape").mock(return_value=httpx.Response(401, json={"error": "Unauthorized"}))
    provider, health = _provider()
    with pytest.raises(AuthError):
        await provider.scrape("https://example.com/")
    assert health.get("firecrawl").last_error_code == "auth_error"


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_is_retried_but_engine_failure_is_not():
    """Regression: the final live A/B lost a docs.python.org fetch to one 429."""
    route = respx.post("https://api.firecrawl.dev/v2/scrape").mock(
        side_effect=[
            httpx.Response(429, json={"error": "Rate limit exceeded"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"success": True, "data": {"markdown": "venv docs", "metadata": {"title": "venv"}}}),
        ]
    )
    provider, _ = _provider()
    result = await provider.scrape("https://docs.python.org/3/library/venv.html")
    assert result.content == "venv docs" and route.call_count == 2

    engine = respx.post("https://api.firecrawl.dev/v2/scrape").mock(
        side_effect=None, return_value=httpx.Response(500, json={"error": "All scraping engines failed!"})
    )
    before = engine.call_count  # respx reuses the route object for the same pattern
    from allsearch.errors import TargetError

    with pytest.raises(TargetError):
        await provider.scrape("https://tieba.baidu.com/p/1")
    assert engine.call_count - before == 1
