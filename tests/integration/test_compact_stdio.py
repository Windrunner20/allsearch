"""One long-lived stdio process must serve compact search without a second provider call."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parents[2]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        server = self.server
        assert isinstance(server, _Server)
        server.requests.append(self.path)
        payload = json.dumps(
            {
                "output_text": "长连接回答",
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

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[str] = []


@pytest.fixture
def mock_provider(tmp_path: Path) -> Iterator[tuple[_Server, Path]]:
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _env(port: int, record_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ALLSEARCH_"):
            env[key] = ""
    env.update(
        {
            "ALLSEARCH_COMPACT_OUTPUT": "true",
            "ALLSEARCH_RECORD_DIR": str(record_dir),
            "ALLSEARCH_XAI_API_KEY": "mock-xai",
            "ALLSEARCH_XAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "ALLSEARCH_XAI_MODEL": "mock-grok",
            "ALLSEARCH_XAI_FALLBACK_MODELS": "none",
            "ALLSEARCH_LOG_LEVEL": "ERROR",
        }
    )
    return env


def _payload(result: object) -> dict:
    structured = getattr(result, "structuredContent", None)
    assert isinstance(structured, dict)
    text = "\n".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )
    assert json.loads(text)["result_id"] == structured["result_id"]
    assert len(text.encode()) <= 8192
    assert len(json.dumps(structured).encode()) <= 8192
    return structured


@pytest.mark.asyncio
async def test_stdio_session_reuses_cache_and_pages_records(mock_provider: tuple[_Server, Path]):
    server, record_dir = mock_provider
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "allsearch", "--transport", "stdio", "--log-level", "ERROR"],
        cwd=str(ROOT),
        env=_env(server.server_address[1], record_dir),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {"search", "fetch", "health", "read_result"}
            first = _payload(await session.call_tool("search", {"query": "长连接", "depth": "fast"}))
            second = _payload(await session.call_tool("search", {"query": "长连接", "depth": "fast"}))
            assert first["cache"]["hit"] is False
            # An identical call in the same session replays the stored record.
            assert second["result_id"] == first["result_id"]
            assert second["session"]["repeat"] is True
            assert "already answered" in second["session"]["advice"]
            assert server.requests == ["/v1/responses"]
            page = _payload(
                await session.call_tool(
                    "read_result",
                    {"result_id": first["result_id"], "fields": ["citations"], "offset": 0, "limit": 2},
                )
            )
            assert page["data"]["citations_total"] >= 3
            assert len(page["data"]["citations"]) == 2
            urls = {item["url"] for item in page["data"]["citations"]}
            assert urls <= {
                "https://example.com/a",
                "https://example.org/b",
                "https://docs.example.com/c",
            }
    stored = list(record_dir.glob("*.json"))
    assert stored
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in stored)
