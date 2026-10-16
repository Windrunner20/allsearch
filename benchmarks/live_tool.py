"""Cold-cache tool benchmark. Sends only the case query or the task URL."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allsearch.config import load_config
from allsearch.errors import redact_text
from allsearch.models import FetchRequest, SearchRequest
from allsearch.orchestrator import Orchestrator
from allsearch.security import validate_public_http_url
from benchmarks.harness import load_cases, provider_query, score_observation

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "benchmarks" / "runs"
SECRET_RE = re.compile(r"(?i)(sk-|xai-|tvly-|fc-|bearer\s+)[A-Za-z0-9._\-]{8,}")


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_RE.sub(r"\1***", redact_text(value))
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value


def _calls(stages: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stage in stages:
        if stage.get("outcome") in {"ok", "partial", "error"}:
            name = str(stage.get("provider") or "unknown")
            counts[name] = counts.get(name, 0) + 1
    return counts


def _auth_dead(payload: dict[str, Any]) -> set[str]:
    dead: set[str] = set()
    for error in payload.get("errors") or []:
        if isinstance(error, dict) and error.get("code") == "auth_error" and error.get("provider"):
            dead.add(str(error["provider"]))
    return dead


async def _fetch_ranked(app: Orchestrator, urls: list[str], limit: int = 2) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for url in urls:
        if len(pages) >= limit:
            break
        try:
            validate_public_http_url(url)
        except Exception:
            continue
        result = await app.fetch(FetchRequest(url=url, fresh=True))
        pages.append(result.model_dump())
    return pages


def _comments_from_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for page in pages:
        text = str(page.get("content") or "").strip()
        url = str(page.get("final_url") or page.get("url") or "")
        if text and url:
            comments.append({"url": url, "text": text[:4000]})
    return comments


async def run_tool_round(
    depth: str = "balanced",
    only_ids: set[str] | None = None,
    resume: Path | None = None,
) -> Path:
    if resume is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = RUNS / f"tool-{depth}-{stamp}"
        out.mkdir(parents=True, exist_ok=True)
        done: set[str] = set()
    else:
        out = resume
        done = set()
        for path in out.glob("*.json"):
            if path.name == "summary.json":
                continue
            try:
                done.add(str(json.loads(path.read_text(encoding="utf-8")).get("id") or ""))
            except json.JSONDecodeError:
                continue
    cases = [
        case
        for case in load_cases()
        if (only_ids is None or case["id"] in only_ids) and case["id"] not in done
    ]
    app = Orchestrator(load_config())
    disabled: set[str] = set()
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        for index, case in enumerate(cases, start=len(done) + 1):
            if "xai" in disabled and case.get("kind") != "fetch":
                rows.append({"id": case["id"], "skipped": "xai_auth_stopped"})
                continue
            t0 = time.perf_counter()
            observation: dict[str, Any] = {
                "id": case["id"],
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "depth": depth,
                "provider_query": provider_query(case),
            }
            try:
                if case.get("kind") == "fetch":
                    fetched = await app.fetch(FetchRequest(url=str(case["url"]), fresh=True))
                    payload = fetched.model_dump()
                    observation["fetched"] = [payload]
                    observation["results"] = [{"url": payload.get("final_url") or payload.get("url"), "title": payload.get("title"), "snippet": ""}]
                    observation["answer"] = ""
                    observation["errors"] = payload.get("errors") or []
                    observation["route"] = payload.get("route") or {}
                else:
                    result = await app.search(
                        SearchRequest(query=provider_query(case), depth=depth, fresh=True, max_results=8)
                    )
                    payload = result.model_dump()
                    observation["answer"] = payload.get("answer") or ""
                    observation["results"] = payload.get("results") or []
                    observation["citations"] = payload.get("citations") or []
                    observation["errors"] = payload.get("errors") or []
                    observation["route"] = payload.get("route") or {}
                    observation["cache"] = payload.get("cache") or {}
                    needs_page = {"reading", "comments"} & set(case.get("task_requires") or [])
                    if needs_page:
                        urls = [str(item.get("url") or "") for item in observation["results"] if item.get("url")]
                        observation["fetched"] = await _fetch_ranked(app, urls, 2)
                    if "comments" in set(case.get("task_requires") or []):
                        observation["comments"] = _comments_from_pages(observation.get("fetched") or [])
                dead = _auth_dead(payload)
                disabled.update(dead)
            except Exception as exc:
                observation["errors"] = [{"provider": "allsearch", "code": "runner_error", "message": redact_text(str(exc))}]
                payload = {}
            observation["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            stages = (observation.get("route") or {}).get("stages") or []
            observation["api_calls"] = _calls(stages)
            if observation.get("fetched"):
                for page in observation["fetched"]:
                    for stage in (page.get("route") or {}).get("stages") or []:
                        if stage.get("outcome") in {"ok", "partial", "error"}:
                            name = str(stage.get("provider") or "firecrawl")
                            observation["api_calls"][name] = observation["api_calls"].get(name, 0) + 1
            observation["score"] = score_observation(case, observation)
            rows.append(_redact(observation))
            (out / f"{index:02d}-{case['id']}.json").write_text(
                json.dumps(rows[-1], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"{index}/{len(cases)} {case['id']} task={observation['score']['task']['status']} calls={observation['api_calls']}", flush=True)
    finally:
        await app.aclose()
    summary = {
        "depth": depth,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "disabled_after_auth": sorted(disabled),
        "rows": rows,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys

    depth = sys.argv[1] if len(sys.argv) > 1 else "balanced"
    only = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
    print(asyncio.run(run_tool_round(depth, only_ids=only)))
