"""Agent round: a real MCP client (Pi) answers each case through AllSearch.

    python -m benchmarks.live_agent --arms baseline,candidate [--ids a,b] [--concurrency 3]

The prompt is the 2026-10-15 prompt (only AllSearch tools, cite original
links, quote comments) plus the case query or fetch URL. Each arm gets its own
temporary Pi agent directory whose mcp.json starts that arm's server: the
candidate from this checkout, the baseline from a git worktree of the baseline
tag with ALLSEARCH_COMPACT_OUTPUT=true, as in the historical Pi setup.

Unlike the 2026-10-15 runner, the observation is built from what the tools
returned to the agent (Pi's tool_execution_end structuredContent), never from
the agent's final prose: result URLs/titles/snippets from search summaries,
page text from fetch previews and read_result pages, comments only when they
arrive as id'd comment records. The final answer is used for synthesis only.
Also recorded: every tool call with its arguments, duplicates, wall time, and
whether the 240 s cap was hit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.harness import CASES_PATH, load_cases, provider_query, score_observation

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs"
PROMPT = (
    "你只能使用 AllSearch MCP 的 search、fetch、read_result、health。"
    "不要使用其他 MCP，不要用 bash，不要读本地 benchmark 文件。"
    "最终答案必须给出可核对的原始链接。如果读到评论，请引用评论原句。\n问题："
)
TIMEOUT = 240
# Pinned for both arms. The user's Pi default (settings.json) is not used:
# it changes over time and loads the user's own Pi packages.
MODEL = os.environ.get("ALLSEARCH_BENCH_PI_MODEL", "huu-gpt/gpt-5.6-terra")
THINKING = os.environ.get("ALLSEARCH_BENCH_PI_THINKING", "medium")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def _agent_dir(base: Path, arm: str, src: Path) -> Path:
    from dotenv import dotenv_values

    agent = base / f"pi-{arm}"
    agent.mkdir(mode=0o700)
    home = Path.home() / ".pi" / "agent"
    for name in ("auth.json", "models.json", "models-store.json"):
        if (home / name).exists():
            (agent / name).symlink_to(home / name)
    env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None and k.startswith("ALLSEARCH_")}
    env.update(
        {
            "PYTHONPATH": str(src),
            "ALLSEARCH_LOG_LEVEL": "WARNING",
            "ALLSEARCH_RECORD_DIR": str(base / f"records-{arm}"),
            "ALLSEARCH_COMPACT_OUTPUT": "true",
        }
    )
    config = {
        "mcpServers": {
            "allsearch": {
                "type": "stdio",
                "exposure": "direct",
                "command": sys.executable,
                "args": ["-m", "allsearch", "--transport", "stdio", "--log-level", "WARNING"],
                "timeout": 180,
                "env": env,
            }
        }
    }
    path = agent / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    os.chmod(path, 0o600)
    return agent


def _parse(stdout: str) -> tuple[list[dict[str, Any]], str]:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    answer = ""
    model_error = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "tool_execution_start":
            cid = str(event.get("toolCallId"))
            calls[cid] = {"tool": event.get("toolName"), "args": event.get("args") or {}}
            order.append(cid)
        elif kind == "tool_execution_end":
            cid = str(event.get("toolCallId"))
            result = event.get("result") or {}
            structured = result.get("structuredContent") if isinstance(result, dict) else None
            if isinstance(structured, dict) and "structuredContent" in structured and "content" in structured:
                # Pi nests the whole MCP CallToolResult: {content, structuredContent, isError}.
                structured = structured.get("structuredContent")
            if structured is None and isinstance(result, dict):
                for block in result.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        try:
                            structured = json.loads(block.get("text") or "")
                        except json.JSONDecodeError:
                            structured = None
            calls.setdefault(cid, {"tool": event.get("toolName"), "args": {}})
            calls[cid].update(result=structured, is_error=bool(event.get("isError")), ms=event.get("durationMs"))
        elif kind == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            if (event.get("message") or {}).get("stopReason") == "error":
                model_error = str((event.get("message") or {}).get("errorMessage") or "error")[:200]
            content = (event.get("message") or {}).get("content")
            if isinstance(content, str):
                answer = content
            elif isinstance(content, list):
                answer = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    _parse.model_error = model_error  # type: ignore[attr-defined]
    return [calls[cid] for cid in order], answer


def observation_from_calls(case_id: str, calls: list[dict[str, Any]], answer: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    fetched: dict[str, dict[str, Any]] = {}
    comments: list[dict[str, Any]] = []
    rid_url: dict[str, str] = {}

    def add_comments(items: Any, thread_url: str) -> None:
        for c in items or []:
            if isinstance(c, dict) and (c.get("id") or c.get("comment_id")):
                comments.append({**c, "thread_url": c.get("thread_url") or thread_url, "source": "api"})

    for call in calls:
        data = call.get("result")
        if not isinstance(data, dict):
            continue
        tool = str(call.get("tool") or "").rsplit("__", 1)[-1]
        if tool == "search":
            for item in (data.get("results") or []) + (data.get("citations") or []):
                if isinstance(item, dict) and item.get("url"):
                    results.append({"url": item["url"], "title": item.get("title") or "", "snippet": item.get("snippet") or "",
                                    "published_at": item.get("published_at")})
            for thread in data.get("threads") or []:
                add_comments(thread.get("top_comments"), thread.get("url") or "")
        elif tool == "fetch":
            url = data.get("final_url") or data.get("url") or (call.get("args") or {}).get("url") or ""
            if data.get("result_id"):
                rid_url[data["result_id"]] = url
            page = fetched.setdefault(url, {"url": url, "content": "", "errors": data.get("errors") or []})
            page["content"] += data.get("content_preview") or ""
            thread = data.get("thread") or {}
            add_comments(thread.get("top_comments"), url)
        elif tool == "read_result":
            page_data = data.get("data") or {}
            url = rid_url.get(str(data.get("result_id")), "")
            if page_data.get("content") and url:
                fetched.setdefault(url, {"url": url, "content": "", "errors": []})["content"] += page_data["content"]
            for item in page_data.get("results") or []:
                if isinstance(item, dict) and item.get("url"):
                    results.append({"url": item["url"], "title": item.get("title") or "", "snippet": item.get("snippet") or "",
                                    "published_at": item.get("published_at")})
            for item in page_data.get("citations") or []:
                if isinstance(item, dict) and item.get("url"):
                    results.append({"url": item["url"], "title": item.get("title") or "", "snippet": ""})
            add_comments(page_data.get("comments"), url)
            for thread in page_data.get("threads") or []:
                add_comments(thread.get("comments"), thread.get("url") or "")
    return {
        "id": case_id,
        "answer": answer,
        "results": results,
        "fetched": list(fetched.values()),
        "comments": comments,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }


def efficiency(calls: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    seen: dict[str, int] = {}
    for call in calls:
        tool = str(call.get("tool") or "").rsplit("__", 1)[-1]
        counts[tool] = counts.get(tool, 0) + 1
        key = json.dumps([tool, call.get("args")], sort_keys=True, ensure_ascii=False)
        seen[key] = seen.get(key, 0) + 1
    stale = [
        int(((call.get("result") or {}).get("session") or {}).get("calls_without_new_evidence") or 0)
        for call in calls
        if isinstance(call.get("result"), dict)
    ]
    return {
        "tool_calls": counts,
        "total_calls": sum(counts.values()),
        "duplicate_calls": sum(n - 1 for n in seen.values() if n > 1),
        "max_calls_without_new_evidence": max(stale) if stale else None,
        "errors": sum(1 for call in calls if call.get("is_error")),
    }


def run_case(agent_dir: Path, case: dict[str, Any], out: Path) -> dict[str, Any]:
    query = provider_query(case)
    if case.get("kind") == "fetch":
        query = "请读取这个 URL 并说明页面是否在讲 venv 模块：" + str(case.get("url") or "")
    env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent_dir))
    started = time.perf_counter()
    timed_out = False
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(
                ["pi", "--mode", "json", "--no-session", "--no-builtin-tools", "--tools", "mcp__allsearch__*",
                 "--model", MODEL, "--thinking", THINKING, "-p", PROMPT + query],
                cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT,
            )
            stdout, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            code, timed_out = 124, True
    calls, answer = _parse(stdout)
    model_error = getattr(_parse, "model_error", "")
    observation = observation_from_calls(case["id"], calls, answer)
    observation["timed_out"] = timed_out
    score = score_observation(case, observation)
    if not answer and model_error and not timed_out:
        # The agent's model failed (e.g. HTTP 503); not a search outcome.
        score["task"] = {"status": "agent_error", "reasons": [model_error]}
    row = {
        "id": case["id"],
        "query": query,
        "returncode": code,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "efficiency": efficiency(calls),
        "calls": [{k: v for k, v in c.items() if k != "result"} for c in calls],
        # Raw tool results as the agent received them, bounded per call.
        "tool_results": [json.dumps(c.get("result"), ensure_ascii=False)[:20000] for c in calls],
        "answer": answer[:6000],
        "observation": observation,
        "score": score,
    }
    (out / f"{case['id']}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out.name} {case['id']} {score['task']['status']} {row['efficiency']['tool_calls']} {row['elapsed_ms'] // 1000}s", flush=True)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", default="baseline,candidate")
    parser.add_argument("--baseline-ref", default="baseline-1244cf5")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--ids", default="")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if shutil.which("pi") is None:
        print("pi is not installed", file=sys.stderr)
        return 2
    cases = load_cases(args.cases)
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or RUNS / f"agent-ab-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    arms = [a for a in args.arms.split(",") if a]
    meta = {"started_at": stamp, "arms": arms, "baseline_ref": args.baseline_ref,
            "baseline_sha": _git("rev-parse", args.baseline_ref), "candidate_sha": _git("rev-parse", "HEAD"),
            "cases_file": str(args.cases), "case_count": len(cases), "timeout_s": TIMEOUT, "client": "pi",
            "model": MODEL, "thinking": THINKING}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="allsearch-agent-") as tmp:
        base = Path(tmp)
        worktree = base / "wt"
        if "baseline" in arms:
            _git("worktree", "add", "--detach", str(worktree), args.baseline_ref)
        try:
            jobs = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency * len(arms)) as pool:
                for arm in arms:
                    src = worktree / "src" if arm == "baseline" else ROOT / "src"
                    agent = _agent_dir(base, arm, src)
                    (out / arm).mkdir(exist_ok=True)
                    for case in cases:
                        if not (out / arm / f"{case['id']}.json").exists():
                            jobs.append(pool.submit(run_case, agent, case, out / arm))
                for job in jobs:
                    job.result()
        finally:
            if "baseline" in arms:
                _git("worktree", "remove", "--force", str(worktree))
    summary = summarize(out, arms, cases)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def summarize(out: Path, arms: list[str], cases: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in arms:
        rows = [json.loads((out / arm / f"{c['id']}.json").read_text(encoding="utf-8")) for c in cases if (out / arm / f"{c['id']}.json").exists()]
        tasks: dict[str, int] = {}
        tools: dict[str, int] = {}
        for row in rows:
            tasks[row["score"]["task"]["status"]] = tasks.get(row["score"]["task"]["status"], 0) + 1
            for tool, n in row["efficiency"]["tool_calls"].items():
                tools[tool] = tools.get(tool, 0) + n
        summary[arm] = {
            "cases": len(rows),
            "task": tasks,
            "tool_calls": tools,
            "duplicate_calls": sum(r["efficiency"]["duplicate_calls"] for r in rows),
            "timeouts_240s": sum(1 for r in rows if r["returncode"] == 124),
            "elapsed_s_total": round(sum(r["elapsed_ms"] for r in rows) / 1000),
        }
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
