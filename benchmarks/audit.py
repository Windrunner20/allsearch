"""Re-score the 2026-10-15 runs without touching them.

    python -m benchmarks.audit [--out benchmarks/runs/audit-YYYYMMDD]

For every stored observation this prints three verdicts:

* ``v1_stored``: the score written into the file at run time.
* ``v1_replay``: grader v1 (``grader_v1.py`` + ``cases.v1.json``) run again on
  the stored, redacted observation. A mismatch means the stored data no longer
  supports the stored score (the 0.2 redactor rewrote ``rfc-editor.org``).
* ``v2``: grader v2 (``harness.py`` + ``cases.json``) on the same data, after
  two corrections that v1 runners did not make:
  - tool rounds: the "comments" were page bodies (no comment id), marked
    ``source: page_body``;
  - agent round: each "result" carried the agent's own final answer as its
    snippet; the snippet is dropped so discovery evidence cannot come from the
    model's text, and a 240 s kill (returncode 124) is a timeout. An agent case
    that only the answer text could have passed is ``pass_unverified``.

Flags explain each difference. Nothing under benchmarks/runs/<run> is modified.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from benchmarks import grader_v1
from benchmarks.harness import CASES_PATH, load_cases, score_observation

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
ROUNDS = {
    "balanced": "tool-balanced-20261015T074021Z",
    "verify": "tool-verify-20261015T080025Z",
    "deep": "tool-deep-20261015T082424Z",
    "agent": "agent-20261015T0839Z",
}
REDACTION_MARKERS = ("rfc-***", "sk-***", "fc-***", "xai-***", "tvly-***")


def _load_round(name: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((RUNS / ROUNDS[name]).glob("*.json")):
        if path.name == "summary.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id"):
            out[data["id"]] = data
    return out


def _agent_observation(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the observation the v1 agent runner scored (see live_agent.py at 1244cf5)."""
    import re

    answer = str(row.get("answer") or "")
    urls = re.findall(r"https?://[^\s)>\"']+", answer)
    return {
        "id": row["id"],
        "answer": answer,
        "results": [{"url": url, "title": "", "snippet": answer[:800]} for url in urls[:8]],
        "executed_at": "2026-10-15T08:40:00Z",
    }


def _v2_observation(kind: str, observation: dict[str, Any], row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    obs = copy.deepcopy(observation)
    flags: list[str] = []
    if kind == "agent":
        for item in obs.get("results") or []:
            item["snippet"] = ""
        if obs.get("results"):
            flags.append("agent_urls_only_from_final_answer")
        if row.get("returncode") == 124:
            obs["timed_out"] = True
            flags.append("agent_timeout_240s")
        return obs, flags
    comments = obs.get("comments")
    if isinstance(comments, list) and comments:
        for item in comments:
            if isinstance(item, dict) and not item.get("id") and not item.get("comment_id"):
                item["source"] = "page_body"
        flags.append("comments_were_page_bodies")
    stages = (obs.get("route") or {}).get("stages") or []
    if any(stage.get("reason") == "deadline_exceeded" for stage in stages) and not obs.get("answer"):
        obs["timed_out"] = True
        flags.append("search_deadline_exceeded")
    return obs, flags


def audit() -> dict[str, Any]:
    v1_cases = {case["id"]: case for case in grader_v1.load_cases(ROOT / "cases.v1.json")}
    v2_cases = {case["id"]: case for case in load_cases(CASES_PATH)}
    rows: list[dict[str, Any]] = []
    for kind in ROUNDS:
        for case_id, row in _load_round(kind).items():
            observation = _agent_observation(row) if kind == "agent" else row
            stored = str(((row.get("score") or {}).get("task") or {}).get("status") or "")
            replay = grader_v1.score_observation(v1_cases[case_id], observation)["task"]["status"]
            obs2, flags = _v2_observation(kind, observation, row)
            scored = score_observation(v2_cases[case_id], obs2)
            verdict = scored["task"]["status"]
            if kind == "agent" and verdict != "pass":
                # Would the answer's own words have carried it? Then the URL is a
                # claim the transcript cannot back, not a demonstrated failure.
                claimed = dict(obs2, results=observation.get("results") or [])
                claimed.pop("timed_out", None)
                if score_observation(v2_cases[case_id], claimed)["task"]["status"] == "pass":
                    verdict = "pass_unverified"
            blob = json.dumps(observation, ensure_ascii=False)
            if any(marker in blob for marker in REDACTION_MARKERS):
                flags.append("stored_urls_damaged_by_redaction")
            if replay != stored:
                flags.append("v1_not_reproducible_from_stored_data")
                verdict = "unverifiable_stored_data"
            if v2_cases[case_id].get("grader_changes"):
                flags.append("case_targets_changed_in_v2")
            if stored == "blocked" and scored.get("observed_failure") is None:
                flags.append("v1_blocked_came_from_search_snippet")
            rows.append(
                {
                    "round": kind,
                    "id": case_id,
                    "v1_stored": stored,
                    "v1_replay": replay,
                    "v2": verdict,
                    "v2_reasons": scored["task"]["reasons"],
                    "flags": flags,
                }
            )
    summary: dict[str, Any] = {}
    for kind in ROUNDS:
        subset = [row for row in rows if row["round"] == kind]
        summary[kind] = {
            "cases": len(subset),
            "v1_stored": dict(Counter(row["v1_stored"] for row in subset)),
            "v2": dict(Counter(row["v2"] for row in subset)),
        }
    tool_rows = [row for row in rows if row["round"] in {"balanced", "verify", "deep"}]
    for grader in ("v1_stored", "v2"):
        solved = {row["id"] for row in tool_rows if row[grader] == "pass"}
        summary[f"tool_cumulative_{grader}"] = {
            "solved_by_any_depth": len(solved),
            "of": len({row["id"] for row in tool_rows}),
        }
    return {"summary": summary, "rows": rows}


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Benchmark audit (generated by `python -m benchmarks.audit`)",
        "",
        "Original run files are unchanged. See `benchmarks/audit.py` for the rules.",
        "",
        "| round | cases | v1 as stored | v2 |",
        "| --- | --- | --- | --- |",
    ]
    for kind in ROUNDS:
        item = result["summary"][kind]
        fmt = lambda counts: ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))  # noqa: E731
        lines.append(f"| {kind} | {item['cases']} | {fmt(item['v1_stored'])} | {fmt(item['v2'])} |")
    lines += [
        "",
        "Cumulative tool rounds (a case counts once if any depth passed it): "
        f"v1 {result['summary']['tool_cumulative_v1_stored']['solved_by_any_depth']}, "
        f"v2 {result['summary']['tool_cumulative_v2']['solved_by_any_depth']} "
        f"of {result['summary']['tool_cumulative_v2']['of']}.",
        "",
        "## Cases whose verdict changed, or whose stored data is suspect",
        "",
        "| round | case | v1 stored | v1 replay | v2 | flags |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["rows"]:
        changed = row["v1_stored"] != row["v2"] or row["v1_stored"] != row["v1_replay"]
        if changed or "stored_urls_damaged_by_redaction" in row["flags"]:
            lines.append(
                f"| {row['round']} | {row['id']} | {row['v1_stored']} | {row['v1_replay']} | "
                f"{row['v2']} | {', '.join(row['flags'])} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    result = audit()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "AUDIT.md").write_text(render_markdown(result), encoding="utf-8")
    print(render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
