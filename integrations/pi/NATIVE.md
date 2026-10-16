# Native Pi MCP

Pi loads AllSearch from `~/.pi/agent/mcp.json` as the stdio server `allsearch`, with `exposure: "direct"` and `ALLSEARCH_COMPACT_OUTPUT=true`.

Tools:

- `mcp__allsearch__search` returns a bounded summary and `result_id`.
- `mcp__allsearch__fetch` returns a short preview and `result_id`.
- `mcp__allsearch__read_result` pages the stored result. It does not call a provider.
- `mcp__allsearch__health` reports provider and circuit state.

Full responses are stored in `~/.local/state/allsearch/records` (mode `0700` / `0600`, one-hour TTL). The code default for compact output remains off, so the legacy bridge still receives full tool payloads if it is explicitly started. The Pi server turns compact output on.

The legacy extension is not loaded. Its source remains in this directory. See `ROLLBACK.md` before enabling it again. Do not enable both.

`pi mcp list` shows connection state and tools. In a session, `/mcp` uses the same configuration. A session that was already open keeps the tools it loaded at startup; start a new session to drop the extension and pick up the native server.
