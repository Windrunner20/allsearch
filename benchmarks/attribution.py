"""How much of an A/B difference survives each grader?

    python -m benchmarks.attribution benchmarks/runs/ab-balanced-<stamp> [...]

Scores the same stored observations three ways:

* ``v1``:            grader_v1.py + cases.v1.json (the 2026-10-15 rules)
* ``v2_rules_v1_targets``: harness.py rules + cases.v1.json targets
* ``v2``:            harness.py + cases.json (current)

If the candidate's lead holds under ``v1``, it does not come from the
grader changes made during development. Note that v1 counts page bodies as
comments and reads access markers from snippets; both arms are affected alike.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from benchmarks import grader_v1, harness

ROOT = Path(__file__).resolve().parent


def score_run(run: Path) -> dict[str, Any]:
    v1_cases = {c["id"]: c for c in grader_v1.load_cases(ROOT / "cases.v1.json")}
    v2_cases = {c["id"]: c for c in harness.load_cases()}
    out: dict[str, Any] = {}
    per_case: dict[str, dict[str, str]] = {}
    for arm in ("baseline", "candidate"):
        counts = {"v1": 0, "v2_rules_v1_targets": 0, "v2": 0}
        for path in sorted((run / arm).glob("*.json")):
            obs = json.loads(path.read_text(encoding="utf-8"))
            cid = obs["id"]
            verdicts = {
                "v1": grader_v1.score_observation(v1_cases[cid], obs)["task"]["status"],
                "v2_rules_v1_targets": harness.score_observation(v1_cases[cid], obs)["task"]["status"],
                "v2": harness.score_observation(v2_cases[cid], obs)["task"]["status"],
            }
            for key, verdict in verdicts.items():
                counts[key] += verdict == "pass"
            per_case.setdefault(cid, {}).update({f"{arm}:{k}": v for k, v in verdicts.items()})
        out[arm] = counts
    out["cases_changed_by_grader"] = {
        cid: v for cid, v in per_case.items()
        if len({v[f"candidate:{k}"] == "pass" for k in ("v1", "v2")}) > 1 or len({v[f"baseline:{k}"] == "pass" for k in ("v1", "v2")}) > 1
    }
    return out


def main(argv: list[str]) -> int:
    for run in argv:
        result = score_run(Path(run))
        print(f"## {Path(run).name}")
        print("| grader | baseline pass | candidate pass |\n| --- | --- | --- |")
        for key in ("v1", "v2_rules_v1_targets", "v2"):
            print(f"| {key} | {result['baseline'][key]} | {result['candidate'][key]} |")
        print("\ncases whose pass/fail differs between v1 and v2 for either arm:")
        for cid, v in result["cases_changed_by_grader"].items():
            print(f"- {cid}: baseline v1={v['baseline:v1']} v2={v['baseline:v2']}; candidate v1={v['candidate:v1']} v2={v['candidate:v2']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
