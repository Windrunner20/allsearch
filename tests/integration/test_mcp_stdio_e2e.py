"""Real MCP protocol coverage without paid provider calls.

In-process sessions use the SDK memory transport and Fake providers.
Subprocess tests speak stdio to ``python -m allsearch`` and to the Pi bridge
CLI, with provider base URLs pointed at a localhost mock.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams

from allsearch.config import AllSearchConfig
from allsearch.errors import ProviderUnavailableError, TransportError
from allsearch.models import ProviderResultItem, ProviderSearchResult
from allsearch.server import build_mcp
from tests.conftest import FakeTavily, FakeXAI

ROOT = Path(__file__).parents[2]
BRIDGE = ROOT / "integrations" / "pi" / "mcp_bridge.py"


class SlowXAI(FakeXAI):
    async def search(self, query: str, **kwargs: Any) -> ProviderSearchResult:
        self.calls.append({"query": query, **kwargs})
        await asyncio.sleep(2)
        raise AssertionError("primary search was not cancelled by the deadline")


def _item(title: str, url: str) -> ProviderResultItem:
    return ProviderResultItem(title=title, url=url, snippet=title, provider="xai")


def _thin_primary() -> ProviderSearchResult:
    return ProviderSearchResult(
        provider="xai",
        query="partial",
        answer="只有一条来源",
        results=[_item("Only", "https://example.com/only")],
        citations=[{"title": "Only", "url": "https://example.com/only"}],
    )


def _duplicate_primary() -> ProviderSearchResult:
    return ProviderSearchResult(
        provider="xai",
        query="dedupe",
        answer="同一页的跟踪参数应合并",
        results=[
            _item("A", "https://example.com/a?utm_source=one"),
            _item("A again", "https://example.com/a?utm_source=two"),
            _item("B", "https://example.org/b"),
            _item("C", "https://docs.example.com/c"),
        ],
        citations=[
            {"title": "A", "url": "https://example.com/a?utm_source=one"},
            {"title": "A again", "url": "https://example.com/a?utm_source=two"},
            {"title": "B", "url": "https://example.org/b"},
            {"title": "C", "url": "https://docs.example.com/c"},
        ],
    )


@asynccontextmanager
async def mcp_client(mcp: Any) -> Any:
    low = mcp._mcp_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: low.run(
                    server_read,
                    server_write,
                    low.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                async with ClientSession(client_read, client_write) as session:
                    init = await session.initialize()
                    yield session, init
            finally:
                tg.cancel_scope.cancel()


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    text = "\n".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise AssertionError(f"tool payload is not an object: {parsed!r}")
    return parsed


async def _install(orch: Any, xai: FakeXAI, tavily: FakeTavily | None = None) -> None:
    orch.xai = xai
    if tavily is not None:
        orch.tavily = tavily


@pytest.mark.asyncio
async def test_stdio_session_lists_tools_and_parses_search(full_config: AllSearchConfig):
    base_config = full_config
    xai = FakeXAI(_duplicate_primary())
    tavily = FakeTavily()
    orch, mcp = build_mcp(base_config)
    await _install(orch, xai, tavily)
    try:
        async with mcp_client(mcp) as (session, init):
            assert init.protocolVersion == "2025-11-25"
            assert init.serverInfo.name
            tools = await session.list_tools()
            assert sorted(tool.name for tool in tools.tools) == ["fetch", "health", "search"]

            result = await session.call_tool(
                "search",
                {"query": "ordinary web page", "depth": "balanced", "fresh": True},
            )
            assert result.isError is False
            payload = _payload(result)
            assert payload["schema_version"] == "1.1"
            assert payload["status"] == "ok"
            urls = [item["url"] for item in payload["results"]]
            assert urls.count("https://example.com/a") == 1
            assert not any("utm_" in url for url in urls)
            assert "https://example.org/b" in urls
            assert tavily.calls == []
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_tool_argument_and_unsafe_url_errors_stay_structured(full_config: AllSearchConfig):
    base_config = full_config
    orch, mcp = build_mcp(base_config)
    await _install(orch, FakeXAI(), FakeTavily())
    try:
        async with mcp_client(mcp) as (session, _init):
            missing = await session.call_tool("search", {})
            assert missing.isError is True
            assert missing.content

            fetched = await session.call_tool("fetch", {"url": "http://127.0.0.1/secret"})
            assert fetched.isError is False
            payload = _payload(fetched)
            assert payload["status"] == "error"
            assert payload["errors"][0]["code"] == "unsafe_url"
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_primary_outage_does_not_call_backup(full_config: AllSearchConfig):
    base_config = full_config
    xai = FakeXAI(ProviderUnavailableError("primary down", provider="xai"))
    tavily = FakeTavily()
    orch, mcp = build_mcp(base_config)
    await _install(orch, xai, tavily)
    try:
        async with mcp_client(mcp) as (session, _init):
            result = await session.call_tool(
                "search", {"query": "primary down", "depth": "balanced", "fresh": True}
            )
            payload = _payload(result)
            assert result.isError is False
            assert payload["status"] == "error"
            assert any(item["code"] == "provider_unavailable" for item in payload["errors"])
            assert any(
                stage["provider"] == "tavily" and stage["outcome"] == "skipped"
                for stage in payload["route"]["stages"]
            )
            assert tavily.calls == []
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_backup_rate_limit_keeps_partial_primary_results(full_config: AllSearchConfig):
    base_config = full_config
    xai = FakeXAI(_thin_primary())
    tavily = FakeTavily(TransportError("429 rate limit", retryable=True, status_code=429))
    orch, mcp = build_mcp(base_config)
    await _install(orch, xai, tavily)
    try:
        async with mcp_client(mcp) as (session, _init):
            result = await session.call_tool(
                "search", {"query": "need a second source", "depth": "balanced", "fresh": True}
            )
            payload = _payload(result)
            assert payload["status"] == "partial"
            assert payload["results"][0]["url"] == "https://example.com/only"
            assert any(item["provider"] == "tavily" for item in payload["errors"])
            assert len(tavily.calls) == 1
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_search_deadline_returns_without_starting_backup(full_config: AllSearchConfig):
    base_config = full_config
    cfg = replace(
        base_config,
        reliability=replace(base_config.reliability, total_budget_seconds=0.4),
    )
    xai = SlowXAI()
    tavily = FakeTavily()
    orch, mcp = build_mcp(cfg)
    await _install(orch, xai, tavily)
    try:
        async with mcp_client(mcp) as (session, _init):
            result = await session.call_tool(
                "search", {"query": "too slow", "depth": "balanced", "fresh": True}
            )
            payload = _payload(result)
            assert payload["status"] == "error"
            assert any(item["code"] == "deadline_exceeded" for item in payload["errors"])
            assert tavily.calls == []
            assert len(xai.calls) == 1
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_chinese_news_query_triggers_news_backup(full_config: AllSearchConfig):
    base_config = full_config
    xai = FakeXAI()
    tavily = FakeTavily()
    orch, mcp = build_mcp(base_config)
    await _install(orch, xai, tavily)
    try:
        async with mcp_client(mcp) as (session, _init):
            await session.call_tool(
                "search",
                {"query": "今天的最新新闻", "depth": "balanced", "fresh": True},
            )
            assert len(tavily.calls) == 1
            assert tavily.calls[0]["topic"] == "news"
    finally:
        await orch.aclose()


class _MockHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        server = self.server
        assert isinstance(server, _MockServer)
        server.requests.append(self.path)
        if self.path.rstrip("/") != "/v1/responses":
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(
            {
                "output_text": "本地模拟回答",
                "citations": [
                    {"title": "甲", "url": "https://example.com/a"},
                    {"title": "乙", "url": "https://example.org/b"},
                    {"title": "丙", "url": "https://docs.example.com/c"},
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _MockServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _MockHandler)
        self.requests: list[str] = []


@pytest.fixture
def mock_provider() -> Iterator[_MockServer]:
    server = _MockServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _isolated_env(port: int) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ALLSEARCH_"):
            env[key] = ""
    env.update(
        {
            "ALLSEARCH_XAI_API_KEY": "mock-xai",
            "ALLSEARCH_XAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "ALLSEARCH_XAI_MODEL": "mock-grok",
            "ALLSEARCH_XAI_FALLBACK_MODELS": "none",
            "ALLSEARCH_ALLOW_DEGRADED_SEARCH": "false",
            "ALLSEARCH_LOG_LEVEL": "ERROR",
            "ALLSEARCH_OUTPUT_MODE": "full",
            "ALLSEARCH_NATIVE_SOURCES": "false",
            "ALLSEARCH_REWRITE_ENABLED": "false",
        }
    )
    return env


@pytest.mark.asyncio
async def test_subprocess_stdio_initialize_list_and_call(mock_provider: _MockServer):
    env = _isolated_env(mock_provider.server_address[1])
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "allsearch", "--transport", "stdio", "--log-level", "ERROR"],
        cwd=str(ROOT),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.protocolVersion == "2025-11-25"
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {"search", "fetch", "health"}

            search = await session.call_tool(
                "search",
                {"query": "普通网页", "depth": "balanced", "fresh": True, "max_results": 5},
            )
            assert search.isError is False
            payload = _payload(search)
            assert payload["status"] == "ok"
            assert any(item["url"] == "https://example.com/a" for item in payload["results"])

            missing = await session.call_tool("search", {})
            assert missing.isError is True

            blocked = await session.call_tool("fetch", {"url": "http://127.0.0.1/secret"})
            assert blocked.isError is False
            assert _payload(blocked)["status"] == "error"
    assert mock_provider.requests == ["/v1/responses"]


def test_pi_bridge_cli_chain_uses_stdio_server(mock_provider: _MockServer):
    env = _isolated_env(mock_provider.server_address[1])
    arguments = base64.b64encode(
        json.dumps(
            {"query": "普通网页", "depth": "balanced", "fresh": True, "max_results": 5},
            ensure_ascii=False,
        ).encode()
    ).decode()
    completed = subprocess.run(
        [sys.executable, str(BRIDGE), "search", arguments, "--max-bytes", "4096"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(lines[-1])
    assert envelope["ok"] is True
    assert envelope["tool"] == "search"
    assert envelope["status"] == "ok"
    assert "https://example.com/a" in envelope["digest"]
    artifact = json.loads(Path(envelope["artifact_path"]).read_text(encoding="utf-8"))
    assert artifact["response"]["schema_version"] == "1.1"
    assert artifact["response"]["results"]
    assert mock_provider.requests == ["/v1/responses"]
