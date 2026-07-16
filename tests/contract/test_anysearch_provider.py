from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from allsearch.cache import MemoryCache
from allsearch.config import AnySearchConfig
from allsearch.providers.anysearch import AnySearchProvider, parse_directory_markdown
from allsearch.transport import HttpTransport

DIR_FIX = Path(__file__).parent / "fixtures" / "anysearch_directory.md"
SECTIONS_FIX = Path(__file__).parent / "fixtures" / "anysearch_directory_sections.md"
SEARCH_FIX = Path(__file__).parent / "fixtures" / "anysearch_search.json"
SEARCH_MD_FIX = Path(__file__).parent / "fixtures" / "anysearch_search_markdown.md"


@pytest.mark.asyncio
@respx.mock
async def test_anysearch_directory_then_search():
    cfg = AnySearchConfig(
        api_key="any-key",
        endpoint="https://api.anysearch.com/mcp",
        enabled=True,
        verticals=("security",),
        directory_ttl_seconds=60,
        max_results=8,
    )
    # AnySearch returns the directory as a Markdown table inside the MCP text content.
    directory_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": DIR_FIX.read_text()}]},
    }
    search_payload = json.loads(SEARCH_FIX.read_text())

    calls = {"n": 0}
    sent_search_args: dict = {}

    def side_effect(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content.decode())
        name = body["params"]["name"]
        if name == "get_sub_domains":
            return httpx.Response(200, json=directory_payload)
        assert name == "search"
        sent_search_args.update(body["params"]["arguments"])
        return httpx.Response(200, json=search_payload)

    respx.post("https://api.anysearch.com/mcp").mock(side_effect=side_effect)
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = AnySearchProvider(cfg, transport, cache=MemoryCache())
    result = await provider.search("CVE-2024-1234 details", domain="security", max_results=8)
    assert calls["n"] >= 2
    assert result.results
    assert "nvd.nist.gov" in result.results[0].url
    # The Markdown directory must yield a valid sub_domain + required param.
    assert sent_search_args.get("domain") == "security"
    assert sent_search_args.get("sub_domain") == "security.cve"
    assert sent_search_args.get("sub_domain_params") == {"cve_id": "CVE-2024-1234"}
    # second search should reuse directory cache
    await provider.search("CVE-2024-9999", domain="security")
    # only one extra search call (directory cached)
    assert calls["n"] == 3
    await transport.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_anysearch_current_section_directory_and_cve_params():
    cfg = AnySearchConfig(
        api_key="any-key",
        endpoint="https://api.anysearch.com/mcp",
        enabled=True,
        verticals=("security",),
        directory_ttl_seconds=60,
        max_results=8,
    )
    directory_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": SECTIONS_FIX.read_text()}]},
    }
    search_payload = json.loads(SEARCH_FIX.read_text())
    sent_search_args: dict = {}

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["params"]["name"] == "get_sub_domains":
            return httpx.Response(200, json=directory_payload)
        sent_search_args.update(body["params"]["arguments"])
        return httpx.Response(200, json=search_payload)

    respx.post("https://api.anysearch.com/mcp").mock(side_effect=side_effect)
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = AnySearchProvider(cfg, transport, cache=MemoryCache())
    result = await provider.search("CVE-2024-1234 vulnerability details", domain="security")
    assert result.results
    assert sent_search_args["sub_domain"] == "security.vuln"
    assert sent_search_args["sub_domain_params"] == {
        "type": "cve",
        "value": "CVE-2024-1234",
    }
    await transport.aclose()


def test_parse_current_section_directory():
    entries = parse_directory_markdown(SECTIONS_FIX.read_text())
    assert len(entries) == 4
    vuln = next(e for e in entries if e["sub_domain"] == "security.vuln")
    assert vuln["required_params"] == ["type", "value"]
    assert "CVE" in vuln["description"]


def test_parse_current_search_markdown():
    cfg = AnySearchConfig(
        api_key="any-key",
        endpoint="https://api.anysearch.com/mcp",
        enabled=True,
        verticals=("security",),
        directory_ttl_seconds=60,
        max_results=8,
    )
    provider = AnySearchProvider(cfg, HttpTransport(timeout_seconds=5, max_retries=0))
    results, warnings = provider.parse_results(SEARCH_MD_FIX.read_text(), domain="security")
    assert warnings == []
    assert [r.title for r in results] == [
        "CVE-2024-1234 Detail - NVD",
        "CVE-2024-1234 Advisory",
        "Invalid result",
    ]
    assert results[0].url == "https://nvd.nist.gov/vuln/detail/CVE-2024-1234"
    assert results[1].url == "https://example.com/advisory/cve-2024-1234"
    assert results[2].url == ""
    assert all(r.url != "https://**" for r in results)
    assert "CVSS" in results[0].snippet


@pytest.mark.asyncio
@respx.mock
async def test_anysearch_refuses_vertical_without_subdomain():
    """If the directory yields no usable sub_domain, vertical search is refused."""
    cfg = AnySearchConfig(
        api_key="any-key",
        endpoint="https://api.anysearch.com/mcp",
        enabled=True,
        verticals=("security",),
        directory_ttl_seconds=60,
        max_results=8,
    )
    empty_directory = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "| domain | sub_domain |\n|---|---|\n"}]},
    }
    respx.post("https://api.anysearch.com/mcp").mock(return_value=httpx.Response(200, json=empty_directory))
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = AnySearchProvider(cfg, transport, cache=MemoryCache())
    result = await provider.search("nothing relevant", domain="security", max_results=8)
    assert result.results == []
    assert "anysearch_no_sub_domain_available" in result.raw_warnings
    await transport.aclose()
