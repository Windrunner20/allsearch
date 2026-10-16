# Holdout cases

Written on 2026-10-16, before any search-algorithm change in the 0.3 cycle, and
committed separately so the commit date is auditable.

Rules:

- Do not run these while developing or tuning routing, intent, or thread code.
- Run them once against the baseline tag and once against the candidate, on the
  same day, and report both.
- Do not add site names, thread ids, or URLs from this file to `src/`.

The grader is the same `benchmarks/harness.py` used for `cases.json`.
