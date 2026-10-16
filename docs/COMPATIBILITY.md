# Compatibility matrix (0.3.0)

"Verified" means it was exercised on 2026-10-16 in this repository's
environment (Linux aarch64, Python 3.14 and 3.11) and the result is recorded
in tests or in the development log. "CI" means it is covered by the GitHub
Actions workflow but has not yet produced a result. "Not verified" means
only the documented configuration format was followed.

## MCP protocol

| Behaviour | stdio | Streamable HTTP | How |
| --- | --- | --- | --- |
| initialize, protocol version negotiation (2025-11-25) | verified | verified | `tests/integration/test_mcp_stdio_e2e.py`, `test_http_transport.py` |
| tools/list, JSON Schema arguments, outputSchema, annotations | verified | verified | same; schemas compared stdio vs HTTP |
| tools/call: `content` + identical `structuredContent` | verified | verified | `_payload` helpers assert equality |
| Argument validation errors (`isError`) | verified | verified | missing `query` |
| Tool errors as `isError` with structured body (compact mode) | verified (in-process) | verified | failing search (`test_compact.py`), unknown `result_id` over HTTP |
| Bounded output and `read_result` paging | verified | verified | fuzz tests, `test_compact_stdio.py` |
| Repeated calls in one session, repeat detection | verified | verified | `test_compact_stdio.py`, `test_compact.py` |
| Concurrent clients | n/a | verified | two principals × three calls in parallel |
| Reconnect (new session after close) | verified | verified | separate sessions in the HTTP tests |
| Bearer auth, 401, per-principal `result_id` isolation | n/a | verified | `test_http_transport.py` |
| Rate limit 429, body limit 413, `/healthz` | n/a | verified | `test_http_security.py` |
| SSE transport (deprecated) | n/a | not verified | kept for compatibility, same security wrapper |

## Clients

| Client | stdio | HTTP | Notes |
| --- | --- | --- | --- |
| MCP Python SDK 1.26 client | verified | verified | integration tests |
| MCP Inspector (CLI) | verified | not verified | `tools/list`, `tools/call fetch` |
| Claude Code 2.1.301 | verified | verified | `fetch` (stdio) and `search` (HTTP + token) returned through the client |
| Pi Coding Agent (native MCP) | verified | not verified | `pi mcp list`, `pi -p` calling `mcp__allsearch__fetch` |
| Claude Desktop | not verified | not verified | config in `examples/clients/claude-desktop.json` |
| Cursor | not verified | not verified | config in `examples/clients/cursor.json` |
| VS Code | not verified | not verified | config in `examples/clients/vscode-mcp.json` |

## Platforms and packaging

| Item | Status |
| --- | --- |
| Linux, Python 3.11 and 3.14 | verified (full offline suite) |
| Linux / macOS / Windows × Python 3.11–3.13 | CI |
| Wheel + sdist build, install into a clean venv, CLI runs | verified locally; also in CI |
| Docker image build, refuses to start without tokens | CI only (no Docker daemon access here) |
| POSIX permissions 0700/0600 | verified on Linux; Windows uses the profile ACL |
