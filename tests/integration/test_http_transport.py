"""Streamable HTTP end to end: a real server process, real MCP clients, no paid calls.

Covers bearer auth, per-principal result isolation, the unauthenticated health
probe, stdio/HTTP parity of the tool surface, and concurrent clients.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from allsearch.http_security import hash_token
from tests.integration.test_mcp_stdio_e2e import _MockServer

ROOT = Path(__file__).parents[2]
ALICE = "as_alice_token_for_tests_0123456789"
BOB = "as_bob_token_for_tests_9876543210xx"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(mock_port: int, record_dir: Path, tokens_file: Path | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALLSEARCH_")}
    env.update(
        {
            "ALLSEARCH_XAI_API_KEY": "mock-xai",
            "ALLSEARCH_XAI_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
            "ALLSEARCH_XAI_MODEL": "mock-grok",
            "ALLSEARCH_XAI_FALLBACK_MODELS": "none",
            "ALLSEARCH_ANYSEARCH_ENABLED": "false",
            "ALLSEARCH_NATIVE_SOURCES": "false",
            "ALLSEARCH_REWRITE_ENABLED": "false",
            "ALLSEARCH_RECORD_DIR": str(record_dir),
            "ALLSEARCH_LOG_LEVEL": "WARNING",
            "ALLSEARCH_ENV_FILE": os.devnull,
        }
    )
    if tokens_file is not None:
        env["ALLSEARCH_HTTP_TOKENS_FILE"] = str(tokens_file)
    return env


@pytest.fixture
def mock_provider() -> Iterator[_MockServer]:
    import threading

    server = _MockServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def http_server(mock_provider: _MockServer, tmp_path: Path) -> Iterator[str]:
    tokens = tmp_path / "tokens"
    tokens.write_text(
        f"# test principals\nalice sha256:{hash_token(ALICE)}\nbob sha256:{hash_token(BOB)}\n",
        encoding="utf-8",
    )
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "allsearch", "--transport", "streamable-http", "--port", str(port)],
        cwd=str(tmp_path),
        env=_env(mock_provider.server_address[1], tmp_path / "records", tokens),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError(proc.stderr.read().decode() if proc.stderr else "server did not start")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _payload(result: Any) -> dict[str, Any]:
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent


async def _session_call(base: str, token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    out = []
    async with streamablehttp_client(base + "/mcp", headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, args in calls:
                out.append(await session.call_tool(name, args))
    return out


def test_unauthenticated_requests_are_rejected(http_server: str):
    assert httpx.get(http_server + "/healthz").json() == {"status": "ok"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream"}
    assert httpx.post(http_server + "/mcp", json=body, headers=headers).status_code == 401
    bad = {**headers, "Authorization": "Bearer not-a-real-token-at-all-000000"}
    response = httpx.post(http_server + "/mcp", json=body, headers=bad)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.asyncio
async def test_result_ids_do_not_cross_principals(http_server: str):
    [search] = await _session_call(http_server, ALICE, [("search", {"query": "隔离测试", "depth": "fast"})])
    summary = _payload(search)
    assert summary["status"] == "ok"
    rid = summary["result_id"]
    [own] = await _session_call(http_server, ALICE, [("read_result", {"result_id": rid, "fields": ["citations"]})])
    assert own.isError is False
    assert _payload(own)["data"]["citations_total"] == 3
    [other] = await _session_call(http_server, BOB, [("read_result", {"result_id": rid})])
    assert other.isError is True
    assert "unknown or expired" in other.content[0].text


@pytest.mark.asyncio
async def test_concurrent_clients_and_parity_with_stdio(http_server: str, mock_provider: _MockServer, tmp_path: Path):
    calls = [
        ("search", {"query": f"并发 {i}", "depth": "fast"}) for i in range(3)
    ]
    results = await asyncio.gather(
        _session_call(http_server, ALICE, calls),
        _session_call(http_server, BOB, calls),
    )
    http_payloads = [_payload(r) for batch in results for r in batch]
    assert all(p["status"] == "ok" for p in http_payloads)
    assert len({p["result_id"] for p in http_payloads}) == 6

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "allsearch", "--transport", "stdio", "--log-level", "ERROR"],
        cwd=str(tmp_path),
        env=_env(mock_provider.server_address[1], tmp_path / "stdio-records", None),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            stdio_tools = await session.list_tools()
            stdio_payload = _payload(await session.call_tool(*calls[0]))
    async with streamablehttp_client(http_server + "/mcp", headers={"Authorization": f"Bearer {ALICE}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            http_tools = await session.list_tools()

    def schema(tools: Any) -> dict[str, Any]:
        return {t.name: (t.inputSchema, t.outputSchema, t.annotations) for t in tools.tools}

    assert schema(stdio_tools) == schema(http_tools)
    ignore = {"result_id", "read", "timing_ms", "session", "route", "cache"}
    assert {k: v for k, v in stdio_payload.items() if k not in ignore} == {
        k: v for k, v in http_payloads[0].items() if k not in ignore
    }


def test_anonymous_public_bind_is_refused(tmp_path: Path, mock_provider: _MockServer):
    env = _env(mock_provider.server_address[1], tmp_path / "r", None)
    env["ALLSEARCH_HTTP_ALLOW_ANONYMOUS"] = "true"
    completed = subprocess.run(
        [sys.executable, "-m", "allsearch", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", str(_free_port())],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert "refusing anonymous HTTP" in completed.stderr
