# Development progress and resume notes (0.3.0 cycle)

No credentials belong in this file. Keys live only in the untracked `.env`.

## Where things stand

Branch `dev/universal-mcp`, based on tag `baseline-1244cf5` (= GitHub `main`
at takeover). Phase: **verification and release preparation**. No new
features or large refactors in this phase.

## Done and verified

| Area | Evidence |
| --- | --- |
| Compact output default, bounded summaries, read_result paging | unit + fuzz tests (`tests/unit/test_output_bounds.py`) |
| Per-principal result ids, atomic record store | `tests/unit/test_records_owner.py` |
| HTTP bearer auth, rate limit, body limit, anonymous-off-loopback refusal | `tests/unit/test_http_security.py`, `tests/integration/test_http_transport.py` (real server process, two principals) |
| Session loop counters and budgets | `tests/unit/test_session.py` |
| Intent, coverage-based stop rule, site-native HN/V2EX/Stack Exchange/Wikipedia | `tests/unit/test_intent_coverage.py`, `test_native.py`, `test_evidence_pipeline.py`; live API smoke |
| Firecrawl per-URL failures do not open its circuit | `tests/contract/test_firecrawl_provider.py` |
| Grader v2 + audit of 2026-10-15 runs | `benchmarks/audit.py`, `benchmarks/runs/audit-20261016/` |
| Clients | MCP SDK stdio + HTTP, MCP Inspector CLI, Claude Code (stdio + HTTP), Pi native MCP |

Offline suite: 350 passed, 1 skipped on Python 3.14 and 3.11 (commit 834194e).

## Live results (grader v2; raw data in benchmarks/runs, analysis in docs/REPORT-0.3.md)

| Run | baseline | candidate |
| --- | --- | --- |
| tool round 2 / 3 / 4 (39 cases, balanced) | 10 / 11 / 15 | 25 / 24 / 24 |
| same, original v1 grader + v1 cases | 8 / 8 / 10 | 19 / 19 / 19 |
| holdout (9 cases; 8 without the OrbStack case) | 4 (4) | 9 (8) |
| Pi agent A/B (39, gpt-5.6-terra, 240 s) | 15 pass, 13 timeouts, 189 searches | 21 pass, 5 timeouts, 127 searches |

## Not verified here

- Docker build (user not in docker group) → CI `package` job.
- Windows / macOS → CI matrix (branch not pushed yet).
- Reddit OAuth reading (no credentials; mocked tests only).
- Claude Desktop, Cursor, VS Code (not installable here).

## Known issues / open questions

- wiki-en-tcp (round 2 candidate fail) — **resolved**. Trigger: provider
  latency, not the grader: the site-restricted Grok call stalled past the
  58.5 s primary cap (same call took 9 s in round 1; baseline has its own
  56–58 s outliers; restricted-call median 15 s vs 15–16 s unrestricted, so
  restriction is not systematically slower). Two candidate defects turned the
  stall into a failure: no fallback answer when the primary is capped, and the
  native Wikipedia query kept "English" (no HTTP article in the top 5).
  Both fixed; regression test in `tests/unit/test_evidence_pipeline.py`.
- Holdout case `ho-v2ex-orbstack` is not independent: a similar OrbStack/V2EX
  query was used as a Claude Code smoke test on 2026-10-16.
- Baseline does not record Grok usage, so baseline cost is estimated only.
  xAI `cost_in_usd_ticks` is converted at 1e10 ticks per USD (xAI docs).

## Next steps (in order)

1. Push `dev/universal-mcp` — **blocked (2026-10-16)**: the HTTPS credential
   available here is an OAuth app token without the `workflow` scope, and
   GitHub rejects the push because the branch changes
   `.github/workflows/tests.yml` (the OS × Python CI matrix). Nothing was
   pushed. Options: grant `workflow` scope / push from an account that has
   it, or push a rewritten branch without the workflow change.
2. Read CI: Linux/macOS/Windows × 3.11–3.13, wheel and Docker build.
3. Fix whatever CI reports; then decide on merging to `main` and tagging v0.3.0.
4. Optional: one interleaved agent A/B after the post-run fixes (741d4be,
   include_domains fix); Reddit OAuth check if credentials are provided.

## Resume checklist

```bash
git switch dev/universal-mcp && git log --oneline -5
uv venv --python 3.14 .venv && uv pip install -e '.[dev]'
.venv/bin/pytest -q
```
