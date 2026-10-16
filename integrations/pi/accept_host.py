#!/usr/bin/env python3
"""One-shot Pi-host acceptance against a localhost mock. Does not call paid APIs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PI = Path.home() / ".npm-global" / "bin" / "pi"
NODE_DIR = Path.home() / "allsearch-mcp-accept"
PI_MCP = (
    "/home/bowie/.npm-global/lib/node_modules/@agegr/pi-web/node_modules/@earendil-works/pi-mcp"
)
PI_TOOLS = (
    "/home/bowie/.npm-global/lib/node_modules/@agegr/pi-web/node_modules/"
    "@earendil-works/pi-coding-agent/dist/extensions/mcp/tools.js"
)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        server = self.server
        assert isinstance(server, Server)
        server.requests.append(self.path)
        if self.path.rstrip("/").endswith("/responses"):
            body = {
                "output_text": "中文摘要 " + ("很长" * 800),
                "citations": [
                    {"title": "甲", "url": "https://example.com/" + ("u" * 180)},
                    {"title": "乙", "url": "https://example.org/b"},
                    {"title": "丙", "url": "https://docs.example.com/c"},
                ],
            }
        elif self.path.rstrip("/").endswith("/scrape"):
            body = {
                "data": {
                    "markdown": "PAGE" * 4000,
                    "metadata": {"title": "示例页", "sourceURL": "https://example.com/a"},
                }
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[str] = []


def main() -> int:
    server = Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    record_dir = NODE_DIR / "records"
    if NODE_DIR.exists():
        shutil.rmtree(NODE_DIR)
    record_dir.mkdir(parents=True)
    project = NODE_DIR / "project"
    (project / ".pi").mkdir(parents=True)
    python = str(ROOT / ".venv" / "bin" / "python")
    config = {
        "mcpServers": {
            "allsearch_accept": {
                "type": "stdio",
                "exposure": "direct",
                "command": python,
                "args": ["-m", "allsearch", "--transport", "stdio", "--log-level", "WARNING"],
                "cwd": str(ROOT),
                "timeout": 60,
                "description": "Temporary AllSearch acceptance server. Mock providers only.",
                "env": {
                    "ALLSEARCH_COMPACT_OUTPUT": "true",
                    "ALLSEARCH_RECORD_DIR": str(record_dir),
                    "ALLSEARCH_XAI_API_KEY": "mock-xai",
                    "ALLSEARCH_XAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
                    "ALLSEARCH_XAI_MODEL": "mock-grok",
                    "ALLSEARCH_XAI_FALLBACK_MODELS": "none",
                    "ALLSEARCH_TAVILY_API_KEY": "",
                    "ALLSEARCH_TAVILY_API_KEYS": "",
                    "ALLSEARCH_ANYSEARCH_API_KEY": "",
                    "ALLSEARCH_FIRECRAWL_API_KEY": "mock-firecrawl",
                    "ALLSEARCH_FIRECRAWL_BASE_URL": f"http://127.0.0.1:{port}",
                    "ALLSEARCH_LOG_LEVEL": "WARNING",
                },
            }
        }
    }
    (project / ".pi" / "mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".npm-global" / "bin") + os.pathsep + env.get("PATH", "")
    listed = subprocess.run(
        [str(PI), "mcp", "list", "--json"],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    node = r"""
import { McpClient, StdioTransport } from %s;
import { convertMcpResult } from %s;

const transport = () => new StdioTransport({
  command: process.env.ALLSEARCH_PYTHON,
  args: ["-m", "allsearch", "--transport", "stdio", "--log-level", "WARNING"],
  cwd: process.env.ALLSEARCH_ROOT,
  env: JSON.parse(process.env.ALLSEARCH_ENV),
  inheritEnv: false,
});

async function session() {
  const client = new McpClient({ name: "pi-accept", version: "1.2.0" });
  const current = transport();
  await client.connect(current);
  return { client, transport: current };
}

const first = await session();
const started = Date.now();
const search = await first.client.callTool("search", { query: "中文网站 最新新闻", depth: "fast" });
const searchMs = Date.now() - started;
const againStarted = Date.now();
const again = await first.client.callTool("search", { query: "中文网站 最新新闻", depth: "fast" });
const againMs = Date.now() - againStarted;
const health = await first.client.callTool("health", { probe: false });
const fetchResult = await first.client.callTool("fetch", { url: "https://example.com/a", max_chars: 20000 });
const resultId = search.structuredContent.result_id;
const page = await first.client.callTool("read_result", {
  result_id: resultId, fields: ["citations"], offset: 0, limit: 2, max_bytes: 4096,
});
const pid = first.transport.pid;
const stderr = first.transport.stderr;
await first.client.close();

const second = await session();
const rereadStarted = Date.now();
const reread = await second.client.callTool("read_result", {
  result_id: resultId, fields: ["answer"], offset: 0, limit: 40, max_bytes: 4096,
});
const rereadMs = Date.now() - rereadStarted;
await second.client.close();

function textOf(result) {
  return (result.content || []).filter((block) => block.type === "text").map((block) => block.text).join("\n");
}
const converted = await convertMcpResult("allsearch_accept", "search", search);
const modelText = textOf(converted);
const report = {
  protocol: first.client.protocolVersion,
  searchMs,
  againMs,
  rereadMs,
  cacheHit: again.structuredContent.cache.hit,
  searchStatus: search.structuredContent.status,
  citationCount: search.structuredContent.citation_count,
  fetchStatus: fetchResult.structuredContent.status,
  fetchChars: fetchResult.structuredContent.content_chars,
  healthStatus: health.structuredContent.status,
  pageUrls: page.structuredContent.data.citations.length,
  rereadChars: reread.structuredContent.data.answer_chars,
  contentBytes: Buffer.byteLength(textOf(search)),
  structuredBytes: Buffer.byteLength(JSON.stringify(search.structuredContent)),
  modelTextBytes: Buffer.byteLength(modelText),
  modelHasStructuredDuplicate: modelText.includes(JSON.stringify(search.structuredContent)) && modelText.length > JSON.stringify(search.structuredContent).length + 20,
  stdoutClean: true,
  stderrHasStdoutRpc: stderr.includes('"jsonrpc"'),
  pid,
  toolsOk: Boolean(resultId),
};
console.log(JSON.stringify(report));
""" % (json.dumps(PI_MCP + "/dist/index.js"), json.dumps(PI_TOOLS))
    node_env = env.copy()
    node_env["ALLSEARCH_PYTHON"] = python
    node_env["ALLSEARCH_ROOT"] = str(ROOT)
    node_env["ALLSEARCH_ENV"] = json.dumps(config["mcpServers"]["allsearch_accept"]["env"])
    node_env["NODE_PATH"] = str(Path(PI_MCP).parents[1])
    called = subprocess.run(
        ["node", "--input-type=module", "-e", node],
        cwd=str(ROOT),
        env=node_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    report = {
        "pi_mcp_list_code": listed.returncode,
        "pi_mcp_list_stderr": listed.stderr[-2000:],
        "pi_mcp_list_stdout": listed.stdout[-4000:],
        "node_code": called.returncode,
        "node_stderr": called.stderr[-2000:],
        "node_stdout": called.stdout[-4000:],
        "mock_requests": server.requests,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    shutil.rmtree(NODE_DIR, ignore_errors=True)
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    return 0 if listed.returncode == 0 and called.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
