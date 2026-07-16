from __future__ import annotations

import asyncio

import pytest

from allsearch.server import _ensure_list, build_mcp


def test_ensure_list_coerces_scalar():
    assert _ensure_list("a.com") == ["a.com"]
    assert _ensure_list(["a", "b"]) == ["a", "b"]
    assert _ensure_list(None) is None


@pytest.mark.asyncio
async def test_build_mcp_registers_three_tools(base_config):
    orch, mcp = build_mcp(base_config)
    tools = getattr(mcp, "_tool_manager", None)
    if tools is not None and hasattr(tools, "list_tools"):
        names = sorted(t.name for t in tools.list_tools())
    elif hasattr(mcp, "_tools"):
        names = sorted(mcp._tools.keys())
    else:
        tm = getattr(mcp, "_tool_manager", mcp)
        tool_dict = getattr(tm, "_tools", {})
        names = sorted(tool_dict.keys())
    assert names == ["fetch", "health", "search"]
    await orch.aclose()
