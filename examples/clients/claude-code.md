# Claude Code

Verified with Claude Code 2.1.301 on Linux, over stdio and Streamable HTTP
(a `fetch` and a `search` call each returned through the client).

## stdio (local)

```bash
claude mcp add allsearch --scope user \
  --env ALLSEARCH_ENV_FILE=/path/to/allsearch/.env \
  -- /path/to/allsearch/.venv/bin/allsearch --transport stdio --log-level WARNING
claude mcp list
```

Tools appear as `mcp__allsearch__search`, `mcp__allsearch__fetch`,
`mcp__allsearch__read_result` and `mcp__allsearch__health`.

For a one-off run that does not touch your settings, put the server in a JSON
file (same shape as [stdio.json](stdio.json)) and pass it explicitly:

```bash
claude -p "Use the allsearch fetch tool on https://news.ycombinator.com/item?id=23508923 ..." \
  --mcp-config mcp.json --strict-mcp-config \
  --allowedTools "mcp__allsearch__fetch,mcp__allsearch__read_result"
```

## Streamable HTTP (remote or shared)

```bash
claude mcp add --transport http allsearch https://search.example.com/mcp \
  --header "Authorization: Bearer $ALLSEARCH_TOKEN"
```

The server answers 401 without a valid token. Result ids are scoped to the
token's principal, so another user's `result_id` reads as unknown.
