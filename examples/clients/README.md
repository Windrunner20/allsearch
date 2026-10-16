# MCP client configuration examples

AllSearch is a standard MCP server. Every client below starts the same
process (stdio) or talks to the same endpoint (Streamable HTTP); nothing in
the server is client-specific.

Replace `/path/to/allsearch` with your checkout, or use the `allsearch`
command installed by `pip install` / `uv tool install`. Keys stay in a `.env`
file (see the main README); do not paste them into client configs that sync
or get committed.

| Client | File | Verified in this repo |
| --- | --- | --- |
| Generic stdio (`mcpServers` JSON) | [stdio.json](stdio.json) | yes: MCP Python SDK client (tests/integration) |
| Generic Streamable HTTP | [http.json](http.json) | yes: MCP Python SDK client (tests/integration) |
| Claude Code | [claude-code.md](claude-code.md) | yes: Claude Code 2.1.301, stdio and HTTP |
| Pi Coding Agent | [pi.json](pi.json) | yes: native Pi MCP, stdio |
| MCP Inspector | [inspector.md](inspector.md) | yes: `@modelcontextprotocol/inspector --cli` |
| Claude Desktop | [claude-desktop.json](claude-desktop.json) | no (not installable here); uses the generic stdio shape |
| Cursor | [cursor.json](cursor.json) | no (not installable here); uses the generic stdio shape |
| VS Code | [vscode-mcp.json](vscode-mcp.json) | no (not installable here); uses the VS Code `servers` shape |

"Verified" means a tool call returned a result through that client during
the 0.3.0 work (see CHANGELOG). Unverified files follow each client's
documented format but were not exercised.
