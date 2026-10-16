# Pi Coding Agent

Pi is one MCP client among several; AllSearch has no Pi-specific code path.
Use Pi's built-in MCP support with the generic stdio configuration in
[examples/clients/pi.json](../../examples/clients/pi.json) (or
[mcp.native.json](mcp.native.json), which is the same server started from a
checkout's `.venv`).

Verified on 2026-10-16 with Pi's native MCP: `pi mcp list` shows
`allsearch: connected, 4 tools`, and a `pi -p` run called
`mcp__allsearch__fetch` and answered from its result.

Tools:

- `mcp__allsearch__search` returns a bounded summary, a `coverage` block and a `result_id`.
- `mcp__allsearch__fetch` returns a short preview and a `result_id`; forum threads on
  Hacker News, V2EX and Stack Exchange come back with comment ids and authors.
- `mcp__allsearch__read_result` pages the stored result. It does not call a provider.
- `mcp__allsearch__health` reports provider and circuit state.

Since 0.3.0 compact output is the server default, so no environment variable
is needed for it. The process is long-lived for the whole Pi session, so the
in-memory cache, circuit breakers and the per-session loop counters are
shared across the session's calls.

When running `pi -p` from a script, close stdin (`< /dev/null`); otherwise Pi
waits on it and the run looks hung.

## Legacy extension

`index.ts` and `mcp_bridge.py` in this directory are the 0.1/0.2 Pi extension.
It is not the recommended setup and is kept only so existing installs can
roll back ([ROLLBACK.md](ROLLBACK.md)). The bridge pins
`ALLSEARCH_OUTPUT_MODE=full`, because it formats full payloads itself. Do not
enable the extension and the native MCP server together: both can search, and
the model may call both.
