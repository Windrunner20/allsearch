# Changelog

## 0.3.0 (2026-10-16)

### Changed (may affect existing setups)

- **Compact output is the default.** `search` and `fetch` return bounded
  summaries plus a `result_id`; full results come from the new default tool
  `read_result`. Restore 0.2 payloads with `ALLSEARCH_OUTPUT_MODE=full`
  (or the old `ALLSEARCH_COMPACT_OUTPUT=false`). The legacy Pi bridge pins
  full mode itself.
- Tool results carry one compact JSON text block plus identical
  `structuredContent`; in compact mode `status: "error"` sets `isError: true`.
- Schema version 1.1 (additive): `coverage`, `threads`, `next_steps`,
  `intent` on search; `thread` on fetch; `threads_read`/`comments_read` in
  evidence; `usage` on route stages.
- Result ids are 24 hex characters and scoped to their owner; 0.2 record
  files are removed on first cleanup. Default record directory is
  per-platform (unchanged on Linux).
- Default total search budget 45 s → 60 s; Grok may use 65% of it.
- `.env` lookup: `ALLSEARCH_ENV_FILE`, working directory, user config
  directory, source checkout.
- Streamable HTTP requires a bearer token unless anonymous mode is enabled on
  a loopback address.
- New dependency: `uvicorn` (already pulled in by `mcp[cli]`).

### Added

- Evidence-driven stop rule and targeted supplements (`intent.py`,
  `sites.py`, `coverage.py`).
- Site-native discovery and thread reading: Hacker News, V2EX, Stack Exchange,
  Wikipedia; Reddit with an OAuth app (`native.py`). `fetch` uses them for
  forum URLs.
- Login walls, captchas, paywalls and unsupported sites are reported as error
  codes instead of being returned as page content.
- Session ledger: repeat detection, new-evidence counters, rolling budgets.
- HTTP security: hashed tokens file, per-principal rate limit, body limit,
  `/healthz`, `allsearch hash-token`, `allsearch check`.
- LICENSE, Dockerfile, compose example, client examples, CI on Linux, macOS
  and Windows with a wheel and Docker build job.
- Benchmarks: grader v2, frozen v1 grader, audit, live A/B, agent A/B,
  attribution, holdout set.

### Fixed

- "Hacker News" was treated as a request for news, sending HN queries to a
  Tavily news search.
- Firecrawl per-URL failures (unsupported site, target timeout) opened the
  Firecrawl circuit and broke unrelated fetches; they were also retried.
- Secret redaction rewrote `rfc-editor.org` and `sk-hynix` news slugs.
- SSRF checks missed IPv4-mapped IPv6 and CGNAT, and blocked public sites
  behind proxy fake-IP resolvers.
- Compact summaries could exceed their byte budget (long URLs, unbounded
  error, route and evidence fields).
- Records were written non-atomically and readable before `chmod`.
- Forum mirror URLs (hk./cn.v2ex.com, `?p=2`) were separate results.
- The degraded-answer fallback was skipped for comment queries.

### Migration

1. Clients that parse the full 0.2 JSON: set `ALLSEARCH_OUTPUT_MODE=full`, or
   switch to the summary plus `read_result`.
2. HTTP deployments: create a tokens file with `allsearch hash-token` and set
   `ALLSEARCH_HTTP_TOKENS_FILE`.
3. Pi: replace the legacy extension with the native MCP config in
   `examples/clients/pi.json` (see `integrations/pi/NATIVE.md`).

### Rollback

`pip install git+https://github.com/Windrunner20/allsearch@1244cf5` (the 0.2
release as taken over; also tagged `baseline-1244cf5` locally). 0.3 records are ignored by 0.2 and expire after
an hour; no other state needs migrating.

## 0.2.0

Grok-primary orchestration with Tavily, AnySearch, Firecrawl; opt-in compact
output; 39-case benchmark.
