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

Offline suite: 342 passed, 1 skipped (Python 3.14, commit 5f8a78d).

## Live results so far (grader v2, balanced, 39 cases, both arms at once)

| Run | baseline-1244cf5 | candidate |
| --- | --- | --- |
| `ab-balanced-20261016T040427Z` | 9 | 17 (commit 673dd56) |
| `ab-balanced-20261016T041942Z` | 10 | 25 (commit 8bcad06) |

## Not verified here

- Docker build (user not in docker group) → CI `package` job.
- Windows / macOS → CI matrix (branch not pushed yet).
- Reddit OAuth reading (no credentials; mocked tests only).
- Claude Desktop, Cursor, VS Code (not installable here).

## Known issues / open questions

- wiki-en-tcp passed in baseline, failed in candidate in round 2 (investigating).
- Holdout case `ho-v2ex-orbstack` is not independent: a similar OrbStack/V2EX
  query was used as a Claude Code smoke test on 2026-10-16.
- Baseline does not record Grok usage, so baseline cost is estimated only.
  xAI `cost_in_usd_ticks` is converted at 1e10 ticks per USD (xAI docs).

## Next steps (in order)

1. Root-cause wiki-en-tcp; minimal fix + regression test.
2. Check A/B parity (budget, providers, grader); one more fair balanced A/B.
3. Holdout run (baseline vs candidate), OrbStack case flagged.
4. Attribute the gain: score round data under grader v1 rules vs v2.
5. Pi agent A/B with `benchmarks/live_agent.py`.
6. README, CHANGELOG, migration/rollback, compatibility matrix, report.
7. Push branch, read CI.

## Resume checklist

```bash
git switch dev/universal-mcp && git log --oneline -5
uv venv --python 3.14 .venv && uv pip install -e '.[dev]'
.venv/bin/pytest -q
```
