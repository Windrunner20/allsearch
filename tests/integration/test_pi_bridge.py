from __future__ import annotations

import importlib.util
from pathlib import Path


BRIDGE = Path(__file__).parents[2] / "integrations" / "pi" / "mcp_bridge.py"
spec = importlib.util.spec_from_file_location("allsearch_pi_bridge", BRIDGE)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_search_digest_is_bounded_and_keeps_sources(tmp_path):
    artifact = tmp_path / "search.json"
    data = {
        "status": "ok",
        "query": "latest version",
        "answer": "answer " * 1_000,
        "results": [
            {
                "title": f"Source {index}",
                "url": f"https://example.com/{index}",
                "snippet": "snippet " * 200,
                "provider": "xai",
                "matched_providers": ["xai", "tavily"],
            }
            for index in range(20)
        ],
        "route": {
            "depth": "verify",
            "mode": "auto",
            "stages": [
                {"provider": "xai", "operation": "search", "outcome": "ok", "reason": "primary"},
                {"provider": "tavily", "operation": "search", "outcome": "ok", "reason": "verify"},
            ],
        },
        "evidence": {"unique_urls": 20, "unique_domains": 3, "cross_provider_matches": 4, "pages_fetched": 2},
        "warnings": [],
        "errors": [],
    }
    digest = bridge.format_search_digest(data, 4_096, str(artifact))
    assert len(digest.encode("utf-8")) <= 4_096
    assert "https://example.com/0" in digest
    assert str(artifact) in digest
    assert "Untrusted external web content" in digest


def test_fetch_digest_is_bounded(tmp_path):
    artifact = tmp_path / "fetch.json"
    data = {
        "status": "ok",
        "provider": "firecrawl",
        "title": "Page",
        "final_url": "https://example.com/page",
        "content": "large page content\n" * 10_000,
        "warnings": [],
    }
    digest = bridge.format_fetch_digest(data, 3_000, str(artifact))
    assert len(digest.encode("utf-8")) <= 3_000
    assert "Content preview" in digest
    assert str(artifact) in digest
