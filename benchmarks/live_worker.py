"""One arm of a live tool benchmark. Imports whichever ``allsearch`` is first
on PYTHONPATH, so the same worker can drive the baseline tag (from a git
worktree) and the candidate. Run through ``benchmarks.live_ab``.

Only the case query (or the task URL for fetch cases) is sent to providers.
Reading policy, identical for both arms: for cases whose task requires reading
or comments, fetch at most two URLs, taking server-suggested ``next_steps``
fetches first and then results in rank order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import re

from allsearch import __version__
from allsearch.config import load_config
from allsearch.models import FetchRequest, SearchRequest
from allsearch.orchestrator import Orchestrator
from benchmarks.harness import load_cases, provider_query


_SECRET = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"
    r"|(?<![A-Za-z0-9])(sk-|xai-|tvly-|fc-)(?:[A-Za-z0-9]{1,12}-)?[A-Za-z0-9_]{16,}[A-Za-z0-9_\-]*"
)


def redact_text(value: str) -> str:
    """Independent of the arm's own redactor (0.2's rewrote rfc-editor.org)."""
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2) or "") + "***", value)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value


def _usage(stages: list[dict[str, Any]]) -> dict[str, Any]:
    calls: dict[str, int] = {}
    ticks = 0
    tavily_credits = 0
    for stage in stages:
        if stage.get("outcome") not in {"ok", "partial", "error"}:
            continue
        name = f"{stage.get('provider')}.{stage.get('operation')}"
        calls[name] = calls.get(name, 0) + 1
        usage = stage.get("usage") or {}
        if isinstance(usage, dict) and isinstance(usage.get("cost_in_usd_ticks"), (int, float)):
            ticks += int(usage["cost_in_usd_ticks"])
        if stage.get("provider") == "tavily" and stage.get("outcome") == "ok":
            tavily_credits += 2 if "verify" in str(stage.get("reason")) or "deep" in str(stage.get("reason")) else 1
    return {"calls": calls, "xai_cost_ticks": ticks, "tavily_credits_estimate": tavily_credits}


def _comments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    threads = list(payload.get("threads") or [])
    if isinstance(payload.get("thread"), dict):
        threads.append(payload["thread"])
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("status") != "comments_read":
            continue
        for comment in thread.get("comments") or []:
            out.append({**comment, "thread_url": thread.get("url"), "source": "api"})
    return out


async def run_case(app: Orchestrator, case: dict[str, Any], depth: str) -> dict[str, Any]:
    started = time.perf_counter()
    observation: dict[str, Any] = {
        "id": case["id"],
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "depth": depth,
        "allsearch_version": __version__,
        "provider_query": provider_query(case),
    }
    stages: list[dict[str, Any]] = []
    try:
        if case.get("kind") == "fetch":
            fetched = (await app.fetch(FetchRequest(url=str(case["url"]), fresh=True))).model_dump()
            stages += (fetched.get("route") or {}).get("stages") or []
            observation.update(
                fetched=[fetched],
                results=[{"url": fetched.get("final_url") or fetched.get("url"), "title": fetched.get("title"), "snippet": ""}],
                answer="",
                errors=fetched.get("errors") or [],
                comments=_comments(fetched),
            )
        else:
            payload = (await app.search(SearchRequest(query=provider_query(case), depth=depth, fresh=True, max_results=8))).model_dump()
            stages += (payload.get("route") or {}).get("stages") or []
            observation.update(
                answer=payload.get("answer") or "",
                results=payload.get("results") or [],
                citations=payload.get("citations") or [],
                errors=payload.get("errors") or [],
                route=payload.get("route") or {},
                coverage=payload.get("coverage"),
                threads=[{k: v for k, v in t.items() if k != "comments"} | {"comments_read": len(t.get("comments") or [])} for t in payload.get("threads") or []],
                next_steps=payload.get("next_steps") or [],
                comments=_comments(payload),
            )
            needs = {"reading", "comments"} & set(case.get("task_requires") or [])
            if needs:
                urls: list[str] = [s["url"] for s in payload.get("next_steps") or [] if s.get("tool") == "fetch" and s.get("url")]
                urls += [r["url"] for r in payload.get("results") or [] if r.get("url")]
                fetched_pages = []
                for url in dict.fromkeys(urls):
                    if len(fetched_pages) >= 2:
                        break
                    page = (await app.fetch(FetchRequest(url=url, fresh=True))).model_dump()
                    stages += (page.get("route") or {}).get("stages") or []
                    fetched_pages.append(page)
                    observation["comments"] += _comments(page)
                observation["fetched"] = fetched_pages
        codes = {e.get("code") for e in observation.get("errors") or [] if isinstance(e, dict)}
        observation["timed_out"] = bool(codes & {"deadline_exceeded"}) and not observation.get("answer") and not observation.get("results")
    except Exception as exc:  # noqa: BLE001 - recorded, run continues
        observation["errors"] = [{"provider": "runner", "code": "runner_error", "message": redact_text(repr(exc))[:300]}]
    observation["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    observation["usage"] = _usage(stages)
    return _redact(observation)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--depth", default="balanced")
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--ids", default="")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args(argv)
    cases = load_cases(args.cases) if args.cases else load_cases()
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    args.out.mkdir(parents=True, exist_ok=True)
    app = Orchestrator(load_config())
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def one(case: dict[str, Any]) -> None:
        dest = args.out / f"{case['id']}.json"
        if dest.exists():
            return
        async with sem:
            observation = await run_case(app, case, args.depth)
        dest.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{case['id']} {observation['elapsed_ms']}ms {observation['usage']['calls']}", flush=True)

    try:
        await asyncio.gather(*(one(case) for case in cases))
    finally:
        await app.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
