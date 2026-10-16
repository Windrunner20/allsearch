from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from allsearch.compact import build_fetch_summary, build_search_summary, project_record
from allsearch.errors import TransportError
from allsearch.models import ProviderResultItem, ProviderSearchResult, SearchRequest
from allsearch.records import RecordStore
from allsearch.server import build_mcp
from tests.conftest import FakeTavily, FakeXAI


def _urls(count: int, width: int = 20) -> list[dict[str, str]]:
    return [
        {"id": f"c{index}", "title": f"title {index}", "url": f"https://example.com/{index}/" + ("u" * width)}
        for index in range(count)
    ]


def test_summary_keeps_every_citation_url_within_budget():
    citations = _urls(6)
    full = {
        "schema_version": "1.0",
        "status": "ok",
        "query": "q",
        "answer": "A" * 5000,
        "results": [
            {
                "id": "r1",
                "title": "t",
                "url": citations[0]["url"],
                "snippet": "s" * 2000,
                "content": "page" * 5000,
                "matched_providers": ["xai"],
            }
        ],
        "citations": citations,
        "errors": [{"provider": "tavily", "code": "transport_error", "message": "x" * 2000, "retryable": True}],
        "warnings": ["warn"],
        "route": {"stages": [{"provider": "xai", "operation": "search", "outcome": "ok", "reason": "primary"}]},
        "evidence": {"unique_urls": 6, "unique_domains": 1},
        "cache": {"hit": False, "age_seconds": 0},
        "timing_ms": 10,
    }
    summary = build_search_summary(full, "abc", max_bytes=8192)
    encoded = json.dumps(summary, ensure_ascii=False).encode()
    assert len(encoded) <= 8192
    assert "pagepagepage" not in encoded.decode()
    assert summary["answer_chars"] == 5000
    assert len(summary["answer"]) < 5000
    assert summary["errors"][0]["code"] == "transport_error"
    assert [item["url"] for item in summary["citations"]] == [item["url"] for item in citations]
    assert "content" not in summary["results"][0]


def test_fetch_summary_hides_the_page_body():
    full = {
        "schema_version": "1.0",
        "status": "ok",
        "url": "https://example.com/page",
        "final_url": "https://example.com/page",
        "title": "Page",
        "provider": "firecrawl",
        "content": "BODY" * 5000,
        "truncated": False,
        "errors": [],
        "warnings": [],
    }
    summary = build_fetch_summary(full, "fetch1", max_bytes=4096)
    assert len(json.dumps(summary, ensure_ascii=False).encode()) <= 4096
    assert summary["content_chars"] == 20000
    assert len(summary["content_preview"]) < summary["content_chars"]
    assert summary["read"]["fields"] == ["content"]


def test_record_permissions_ttl_and_capacity(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=10, max_entries=2, max_bytes=10_000_000)
    if os.name == "posix":
        assert (tmp_path.stat().st_mode & 0o777) == 0o700
    first = store.put("search", {"answer": "one"}, now=1000)
    second = store.put("search", {"answer": "two"}, now=1001)
    third = store.put("search", {"answer": "three"}, now=1002)
    assert store.get(first, now=1003) is None
    assert store.get(second, now=1003)["response"]["answer"] == "two"
    assert store.get(third, now=1003)["response"]["answer"] == "three"
    [path] = list(tmp_path.glob(f"*_{third}.json"))
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert store.get(second, now=1001 + 11) is None
    assert store.get("../" + third, now=1003) is None
    assert store.get(third, now=1003) is not None


@pytest.mark.asyncio
async def test_compact_search_reuses_process_cache_and_pages_citations(base_config, tmp_path):
    cfg = replace(
        base_config,
        compact_output=True,
        record_dir=str(tmp_path),
        compact_max_bytes=8192,
        server_name="allsearch_native",
    )
    xai = FakeXAI()
    long_answer = "完整答案" * 1000
    xai.result = ProviderSearchResult(
        provider="xai",
        query="q",
        answer=long_answer,
        results=[
            ProviderResultItem(
                title="One",
                url="https://example.com/one",
                snippet="snippet " * 80,
                content="FULL-PAGE",
                provider="xai",
            ),
            ProviderResultItem(title="Two", url="https://example.org/two", snippet="s", provider="xai"),
            ProviderResultItem(title="Three", url="https://docs.example.com/three", snippet="s", provider="xai"),
        ],
        citations=[
            {"title": "One", "url": "https://example.com/one"},
            {"title": "Two", "url": "https://example.org/two"},
            {"title": "Three", "url": "https://docs.example.com/three"},
        ],
    )
    orch, mcp = build_mcp(cfg)
    orch.xai = xai
    orch.tavily = FakeTavily()
    try:
        names = sorted(tool.name for tool in mcp._tool_manager.list_tools())
        assert names == ["fetch", "health", "read_result", "search"]
        search = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "search")
        first = _sc(await search.fn(query="bounded", depth="fast", fresh=False))
        second = _sc(await search.fn(query="bounded", depth="fast", fresh=False))
        assert len(xai.calls) == 1
        # Identical call in the same session: same record, flagged as a repeat.
        assert first["result_id"] == second["result_id"]
        assert first["cache"]["hit"] is False
        assert second["session"]["repeat"] is True
        encoded = json.dumps(first, ensure_ascii=False)
        assert "FULL-PAGE" not in encoded
        assert len(encoded.encode()) <= 8192
        assert first["citation_count"] == 3
        reader = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "read_result")
        page = _sc(await reader.fn(result_id=first["result_id"], fields=["citations"], offset=0, limit=1, max_bytes=4096))
        assert page["data"]["citations_total"] == 3
        assert len(page["data"]["citations"]) == 1
        assert page["next_offset"] == 1
        answer = _sc(await reader.fn(result_id=first["result_id"], fields=["answer"], offset=0, limit=20, max_bytes=4096))
        assert answer["data"]["answer_chars"] == len(long_answer)
        assert len(answer["data"]["answer"]) <= 20
        assert len(xai.calls) == 1
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_read_result_unknown_id_is_a_tool_error(base_config, tmp_path):
    cfg = replace(base_config, compact_output=True, record_dir=str(tmp_path))
    orch, mcp = build_mcp(cfg)
    reader = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "read_result")
    try:
        with pytest.raises(Exception, match="unknown or expired"):
            await reader.fn(result_id="0" * 24)
    finally:
        await orch.aclose()


def _sc(result):
    """structuredContent of a CallToolResult; the text block must carry the same object."""
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent


def test_native_mcp_config_is_separate_from_the_extension():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "integrations", "pi", "mcp.native.json")
    config = json.loads(open(path, encoding="utf-8").read())
    server = config["mcpServers"]["allsearch_native"]
    assert server["type"] == "stdio"
    assert "mcp_bridge.py" not in json.dumps(server)
    # Compact output is the 0.3 default, so the config does not need to set it,
    # and it must not force the legacy full mode.
    assert server["env"].get("ALLSEARCH_OUTPUT_MODE", "compact") == "compact"
    assert server["env"].get("ALLSEARCH_COMPACT_OUTPUT", "true") == "true"
    assert "allsearch_search" not in json.dumps(config)


def test_summary_pages_urls_that_do_not_fit(tmp_path):
    citations = _urls(30, width=400)
    full = {
        "schema_version": "1.0",
        "status": "partial",
        "query": "q",
        "answer": "short",
        "results": [],
        "citations": citations,
        "errors": [],
        "warnings": [],
        "route": {"stages": []},
        "evidence": {},
        "cache": {},
        "timing_ms": 1,
    }
    summary = build_search_summary(full, "abc", max_bytes=2048)
    assert len(json.dumps(summary).encode()) <= 2048
    assert summary["citation_count"] == 30
    assert summary["citations_truncated"] is True
    store = RecordStore(tmp_path, ttl_seconds=60, max_entries=10, max_bytes=10_000_000)
    result_id = store.put("search", full)
    record = store.get(result_id)
    assert record is not None
    collected: list[str] = []
    offset = 0
    while offset is not None:
        page = project_record(record, fields=["citations"], offset=offset, limit=4, max_bytes=4096)
        collected.extend(item["url"] for item in page["data"]["citations"])
        offset = page["next_offset"]
        assert len(json.dumps(page).encode()) <= 8192
    assert collected == [item["url"] for item in citations]


def test_result_id_rejects_path_traversal(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=60, max_entries=5, max_bytes=1_000_000)
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-read", encoding="utf-8")
    for raw in ("../secret.txt", "..", "/etc/passwd", "abcd/../../secret.txt", "A" * 16, ".." * 8):
        assert store.get(raw) is None
    assert secret.read_text(encoding="utf-8") == "do-not-read"


def test_concurrent_puts_keep_the_entry_cap(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=60, max_entries=8, max_bytes=10_000_000)

    def write(index: int) -> str:
        return store.put("search", {"n": index, "text": "中文引用" * 20})

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(24)))
    assert len(set(ids)) == 24
    assert len(list(tmp_path.glob("*.json"))) <= 8
    assert not list(tmp_path.glob(".tmp-*"))


@pytest.mark.asyncio
async def test_compact_record_redacts_secrets_and_keeps_error_codes(base_config, tmp_path):
    cfg = replace(base_config, compact_output=True, record_dir=str(tmp_path))
    secret = "sk-testsecretvalue123456"
    xai = FakeXAI(TransportError(f"upstream api_key={secret}", retryable=True, status_code=502))
    orch, mcp = build_mcp(cfg)
    orch.xai = xai
    orch.tavily = FakeTavily()
    try:
        search = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "search")
        result = await search.fn(query="失败也不带密钥", depth="fast", fresh=True)
        assert result.isError is True
        summary = _sc(result)
        assert summary["errors"][0]["code"] == "transport_error"
        assert secret not in json.dumps(summary, ensure_ascii=False)
        stored = Path(tmp_path).read_text() if False else "\n".join(
            path.read_text(encoding="utf-8") for path in Path(tmp_path).glob("*.json")
        )
        assert secret not in stored
        assert "test-xai-key" not in stored
        assert summary["errors"][0]["provider"] == "xai"
    finally:
        await orch.aclose()


def test_full_mode_does_not_add_read_result(full_config):
    _orch, mcp = build_mcp(full_config)
    names = sorted(tool.name for tool in mcp._tool_manager.list_tools())
    assert names == ["fetch", "health", "search"]
    assert SearchRequest(query="still valid").query == "still valid"
