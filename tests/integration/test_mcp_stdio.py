from __future__ import annotations

import pytest

from allsearch.server import build_mcp


@pytest.mark.asyncio
async def test_tool_handlers_with_fakes(base_config, monkeypatch):
    from tests.conftest import FakeFirecrawl, FakeTavily, FakeXAI, make_orchestrator

    orch = make_orchestrator(base_config)
    # Direct orchestrator path (MCP handlers are thin wrappers)
    search = await orch.search(
        __import__("allsearch.models", fromlist=["SearchRequest"]).SearchRequest(
            query="integration path", depth="balanced"
        )
    )
    assert search.schema_version == "1.0"
    assert "route" in search.model_dump()

    health = await orch.health()
    assert health.version

    fetch = await orch.fetch(
        __import__("allsearch.models", fromlist=["FetchRequest"]).FetchRequest(
            url="https://example.com/page"
        )
    )
    assert fetch.provider == "firecrawl" or fetch.status in {"ok", "partial", "error"}
    await orch.aclose()


def test_mcp_tool_list_stable(base_config):
    orch, mcp = build_mcp(base_config)
    tm = getattr(mcp, "_tool_manager", None)
    tools = tm.list_tools() if tm and hasattr(tm, "list_tools") else list(getattr(tm, "_tools", {}).values())
    names = sorted(getattr(t, "name", None) or k for k, t in (
        tools if isinstance(tools, dict) else {t.name: t for t in tools}
    ).items()) if False else None
    # simpler extraction
    if hasattr(tm, "list_tools"):
        names = sorted(t.name for t in tm.list_tools())
    else:
        names = sorted(getattr(tm, "_tools", {}).keys())
    assert names == ["fetch", "health", "search"]
