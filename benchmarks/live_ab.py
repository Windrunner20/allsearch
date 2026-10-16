"""Live A/B: baseline tag vs working tree, same cases, same time window.

    python -m benchmarks.live_ab --baseline-ref baseline-1244cf5 --depth balanced \
        [--cases benchmarks/cases.json] [--ids a,b] [--concurrency 2]

Both arms run at the same time from separate processes: the baseline from a
temporary git worktree of ``--baseline-ref`` (its ``src`` first on PYTHONPATH),
the candidate from this checkout. Both use the same .env, depth, deadline and
reading policy (benchmarks/live_worker.py), and are scored by grader v2.

This calls paid providers. Calls, latency and provider-reported cost are
written to benchmarks/runs/ab-<stamp>/summary.json and COMPARE.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.harness import CASES_PATH, load_cases, score_observation

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs"
XAI_TICKS_PER_USD = 10_000_000_000  # xAI reports cost_in_usd_ticks; 1 USD = 1e10 ticks


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def _arm(name: str, src: Path, out: Path, args: argparse.Namespace, cases_path: Path) -> subprocess.Popen[str]:
    from dotenv import dotenv_values

    env = dict(os.environ)
    # Identical provider configuration for both arms; 0.2 cannot find .env from a worktree.
    for key, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            env.setdefault(key, value)
    env["PYTHONPATH"] = os.pathsep.join([str(src), str(ROOT)])
    log = open(out.parent / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115 - closed by the OS on exit
    return subprocess.Popen(
        [sys.executable, "-m", "benchmarks.live_worker", "--out", str(out), "--depth", args.depth,
         "--cases", str(cases_path), "--ids", args.ids, "--concurrency", str(args.concurrency)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )


def score_dir(directory: Path, cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for case in cases:
        path = directory / f"{case['id']}.json"
        if not path.exists():
            continue
        observation = json.loads(path.read_text(encoding="utf-8"))
        out[case["id"]] = {"observation": observation, "score": score_observation(case, observation)}
    return out


def summarize(arms: dict[str, dict[str, dict[str, Any]]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, rows in arms.items():
        statuses: dict[str, int] = {}
        dims: dict[str, dict[str, int]] = {}
        calls: dict[str, int] = {}
        ticks = 0
        elapsed = []
        for row in rows.values():
            status = row["score"]["task"]["status"]
            statuses[status] = statuses.get(status, 0) + 1
            for dim, value in row["score"]["scores"].items():
                dims.setdefault(dim, {})
                dims[dim][value["status"]] = dims[dim].get(value["status"], 0) + 1
            usage = row["observation"].get("usage") or {}
            for key, count in (usage.get("calls") or {}).items():
                calls[key] = calls.get(key, 0) + count
            ticks += int(usage.get("xai_cost_ticks") or 0)
            elapsed.append(int(row["observation"].get("elapsed_ms") or 0))
        elapsed.sort()
        summary[name] = {
            "cases": len(rows),
            "task": statuses,
            "dimensions": dims,
            "calls": dict(sorted(calls.items())),
            "xai_cost_ticks": ticks,
            "xai_cost_usd_reported": round(ticks / XAI_TICKS_PER_USD, 4),
            "elapsed_ms_total": sum(elapsed),
            "elapsed_ms_median": elapsed[len(elapsed) // 2] if elapsed else 0,
        }
    names = list(arms)
    if len(names) == 2:
        a, b = names
        changes = []
        for case in cases:
            ra, rb = arms[a].get(case["id"]), arms[b].get(case["id"])
            if not ra or not rb:
                continue
            sa, sb = ra["score"]["task"]["status"], rb["score"]["task"]["status"]
            if sa != sb:
                changes.append({"id": case["id"], a: sa, b: sb, f"{b}_reasons": rb["score"]["task"]["reasons"]})
        summary["changes"] = changes
    return summary


def render(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    arms = [k for k in summary if k not in {"changes"}]
    lines = [f"# Live A/B ({meta['depth']}, {meta['started_at']})", ""]
    lines += [f"- baseline: `{meta['baseline_ref']}` ({meta['baseline_sha'][:8]})", f"- candidate: working tree at `{meta['candidate_sha'][:8]}`" + (" + uncommitted changes" if meta["dirty"] else ""), f"- cases: {meta['cases_file']} ({meta['case_count']})", ""]
    lines += ["| arm | pass | fail | timeout | blocked | other | Grok cost (reported) | median s | total s |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for arm in arms:
        s = summary[arm]
        t = s["task"]
        other = sum(v for k, v in t.items() if k not in {"pass", "fail", "timeout", "blocked"})
        lines.append(
            f"| {arm} | {t.get('pass', 0)} | {t.get('fail', 0)} | {t.get('timeout', 0)} | {t.get('blocked', 0)} | {other} | "
            f"${s['xai_cost_usd_reported']} | {s['elapsed_ms_median'] / 1000:.1f} | {s['elapsed_ms_total'] / 1000:.0f} |"
        )
    lines += ["", "## Calls", ""]
    for arm in arms:
        lines.append(f"- {arm}: " + ", ".join(f"{k} {v}" for k, v in summary[arm]["calls"].items()))
    lines += ["", "## Per-dimension", ""]
    dims = sorted({d for arm in arms for d in summary[arm]["dimensions"]})
    lines += ["| dimension | " + " | ".join(arms) + " |", "| --- | " + " | ".join("---" for _ in arms) + " |"]
    for dim in dims:
        cells = []
        for arm in arms:
            counts = summary[arm]["dimensions"].get(dim, {})
            cells.append(", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        lines.append(f"| {dim} | " + " | ".join(cells) + " |")
    if summary.get("changes"):
        lines += ["", "## Cases whose task verdict changed", "", "| case | " + " | ".join(arms) + " | candidate reasons |", "| --- | --- | --- | --- |"]
        for row in summary["changes"]:
            lines.append(f"| {row['id']} | {row[arms[0]]} | {row[arms[1]]} | {', '.join(row[f'{arms[1]}_reasons'])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-ref", default="baseline-1244cf5")
    parser.add_argument("--depth", default="balanced")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--ids", default="")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--score-only", type=Path, default=None, help="re-score an existing run directory")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    if args.score_only:
        out = args.score_only
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = args.out or RUNS / f"ab-{args.depth}-{stamp}"
        out.mkdir(parents=True, exist_ok=True)
        meta = {
            "depth": args.depth,
            "started_at": stamp,
            "baseline_ref": args.baseline_ref,
            "baseline_sha": _git("rev-parse", args.baseline_ref),
            "candidate_sha": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "--", "src")),
            "cases_file": str(args.cases.relative_to(ROOT)) if args.cases.is_relative_to(ROOT) else str(args.cases),
            "case_count": len(cases),
            "grader": "v2",
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="allsearch-baseline-") as tmp:
            worktree = Path(tmp) / "wt"
            _git("worktree", "add", "--detach", str(worktree), args.baseline_ref)
            try:
                procs = [
                    _arm("baseline", worktree / "src", out / "baseline", args, args.cases),
                    _arm("candidate", ROOT / "src", out / "candidate", args, args.cases),
                ]
                codes = [proc.wait() for proc in procs]
            finally:
                _git("worktree", "remove", "--force", str(worktree))
        meta["exit_codes"] = codes
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    arms = {name: score_dir(out / name, cases) for name in ("baseline", "candidate") if (out / name).exists()}
    summary = summarize(arms, cases)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = render(summary, meta)
    (out / "COMPARE.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
