# MCP Inspector

Verified with `@modelcontextprotocol/inspector` in CLI mode over stdio
(`tools/list` and a `tools/call` of `fetch`).

The Inspector parses `--log-level` itself, so pass server settings with `-e`:

```bash
npx -y @modelcontextprotocol/inspector --cli \
  -e ALLSEARCH_ENV_FILE=/path/to/allsearch/.env -e ALLSEARCH_LOG_LEVEL=WARNING \
  /path/to/allsearch/.venv/bin/allsearch --method tools/list

npx -y @modelcontextprotocol/inspector --cli \
  -e ALLSEARCH_ENV_FILE=/path/to/allsearch/.env -e ALLSEARCH_LOG_LEVEL=WARNING \
  /path/to/allsearch/.venv/bin/allsearch --method tools/call \
  --tool-name fetch --tool-arg 'url=https://news.ycombinator.com/item?id=23508923'
```

Without `--cli` the Inspector opens its web UI. Choose STDIO with the same
command, or Streamable HTTP with `http://127.0.0.1:8000/mcp` and an
`Authorization: Bearer …` header.
