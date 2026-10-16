"""Plan and score AllSearch benchmark cases without calling providers.

``plan`` only uses the routing rules. ``score`` grades a saved observation.
``live`` is refused unless ALLSEARCH_RUN_BENCHMARK=1, and this module's
default CLI never sets that variable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from allsearch.config import AllSearchConfig
from allsearch.models import SearchRequest
from allsearch.routing import build_execution_plan, classify_query, tavily_required_after_primary

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.json"
DIMENSIONS = ("discovery", "reading", "comments", "cross_language", "synthesis")
ACCESS_MARKERS = {
    "paywall": ("subscribe to continue", "subscription required", "paywall"),
    "anti_bot": ("captcha", "access denied", "enable javascript", "are you a robot"),
    "login": ("sign in to continue", "login required", "请登录", "登录后"),
}


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    data = json.loads((path or CASES_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("benchmark cases must be a list")
    return data


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _hosts_of(observation: dict[str, Any]) -> list[str]:
    hosts: list[str] = []
    for item in observation.get("results") or []:
        if isinstance(item, dict) and item.get("url"):
            hosts.append(_host(str(item["url"])))
    for item in observation.get("citations") or []:
        if isinstance(item, dict) and item.get("url"):
            hosts.append(_host(str(item["url"])))
    return [host for host in hosts if host]


def _host_hit(host: str, expected: str) -> bool:
    return host == expected or host.endswith("." + expected)


def plan_case(case: dict[str, Any], config: AllSearchConfig, *, depth: str) -> dict[str, Any]:
    request = SearchRequest(
        query=provider_query(case) or str(case.get("url") or case["id"]),
        depth=depth,  # type: ignore[arg-type]
        mode=case.get("mode") or "auto",
    )
    signals = classify_query(request, config.anysearch.verticals)
    plan = build_execution_plan(request, config)
    tavily_planned = any(stage.provider == "tavily" for stage in plan.stages)
    skipped_if_sufficient = not tavily_required_after_primary(
        depth=request.depth,
        primary_ok=True,
        primary_sufficient=True,
        signals=signals,
        tavily_planned=tavily_planned,
    )
    calls_if_sufficient = ["xai"]
    if not skipped_if_sufficient and tavily_planned:
        calls_if_sufficient.append("tavily")
    if any(stage.provider == "anysearch" for stage in plan.stages):
        calls_if_sufficient.append("anysearch")
    calls_if_thin = list(dict.fromkeys([*calls_if_sufficient, *(["tavily"] if tavily_planned else [])]))
    scrape = plan.scrape_budget if plan.scrape_after_merge else 0
    return {
        "id": case["id"],
        "channel": case["channel"],
        "depth": depth,
        "kind": case.get("kind", "search"),
        "access": case.get("access", "public"),
        "recency": signals.recency,
        "comparison": signals.comparison,
        "docs": signals.docs,
        "verticals": list(signals.verticals),
        "stages": [stage.provider for stage in plan.stages],
        "tavily_skipped_if_primary_sufficient": skipped_if_sufficient and case.get("kind", "search") == "search",
        "scrape_pages": scrape,
        "calls_if_primary_sufficient": calls_if_sufficient if case.get("kind") != "fetch" else ["firecrawl"],
        "calls_if_primary_thin": calls_if_thin if case.get("kind") != "fetch" else ["firecrawl"],
    }


def current_stop_decision(
    config: AllSearchConfig, query: str, urls: list[str], *, depth: str = "balanced"
) -> dict[str, Any]:
    """Read-only diagnosis of today's stop rule. This does not change routing."""
    from allsearch.models import ProviderResultItem, ProviderSearchResult
    from allsearch.quality import evaluate_primary_sufficiency

    request = SearchRequest(query=query, depth=depth)  # type: ignore[arg-type]
    signals = classify_query(request, config.anysearch.verticals)
    primary = ProviderSearchResult(
        provider="xai",
        query=query,
        answer="primary answer with enough prose",
        results=[
            ProviderResultItem(title=url, url=url, snippet="unrelated snippet", provider="xai")
            for url in urls
        ],
        citations=[{"title": url, "url": url} for url in urls],
    )
    sufficient = evaluate_primary_sufficiency(
        primary,
        config,
        depth=depth,
        comparison=signals.comparison,
        recency=signals.recency,
    )
    plan = build_execution_plan(request, config)
    tavily_planned = any(stage.provider == "tavily" for stage in plan.stages)
    skipped = not tavily_required_after_primary(
        depth=depth,  # type: ignore[arg-type]
        primary_ok=True,
        primary_sufficient=sufficient.sufficient,
        signals=signals,
        tavily_planned=tavily_planned,
    )
    return {
        "recency": signals.recency,
        "comparison": signals.comparison,
        "sufficient": sufficient.sufficient,
        "reasons": sufficient.reasons,
        "supplement_skipped": skipped,
    }


def budget(plans: list[dict[str, Any]]) -> dict[str, Any]:
    search_plans = [item for item in plans if item["kind"] != "fetch"]
    fetch_plans = [item for item in plans if item["kind"] == "fetch"]

    def count(key: str, provider: str) -> int:
        return sum(provider in item[key] for item in search_plans)

    return {
        "cases": len(plans),
        "search_cases": len(search_plans),
        "fetch_cases": len(fetch_plans),
        "xai_min": count("calls_if_primary_sufficient", "xai"),
        "xai_max": count("calls_if_primary_thin", "xai"),
        "tavily_min": count("calls_if_primary_sufficient", "tavily"),
        "tavily_max": count("calls_if_primary_thin", "tavily"),
        "anysearch_min": count("calls_if_primary_sufficient", "anysearch"),
        "firecrawl_pages_max": sum(item["scrape_pages"] for item in search_plans) + len(fetch_plans),
        "skipped_supplement_if_primary_looks_full": sum(
            1 for item in search_plans if item["tavily_skipped_if_primary_sufficient"]
        ),
    }


def _records(observation: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [item for item in observation.get(key) or [] if isinstance(item, dict)]


def _target_url_match(url: str, target: dict[str, Any]) -> bool:
    hosts = [str(host).lower() for host in target.get("hosts") or [] if str(host).strip()]
    if target.get("host"):
        hosts.append(str(target["host"]).lower())
    if hosts and not any(_host_hit(_host(url), host) for host in hosts):
        return False
    path_regex = str(target.get("path_regex") or "")
    if path_regex and re.search(path_regex, url, re.I) is None:
        return False
    needle = str(target.get("path_contains") or "")
    if needle:
        return needle.lower() in url.lower()
    if path_regex:
        return True
    path = urlparse(url).path.strip("/")
    return bool(path)


def _matched_records(observation: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in ("results", "citations", "fetched"):
        for item in _records(observation, key):
            if item.get("url") and _target_url_match(str(item["url"]), target):
                found.append(item)
    return found


def _text_has(items: list[dict[str, Any]], evidence: list[str], fields: tuple[str, ...]) -> bool:
    if not evidence:
        return True
    blob = "\n".join(
        "\n".join(str(item.get(field) or "") for field in fields) for item in items
    ).lower()
    return all(str(token).lower() in blob for token in evidence)


def _discovery_status(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    if case.get("dynamic_url"):
        pages = []
        for item in _records(observation, "results") + _records(observation, "citations"):
            url = str(item.get("url") or "")
            path = urlparse(url).path.strip("/")
            if url.startswith("http") and path and item.get("published_at"):
                pages.append(url)
        return {"status": "pass" if pages else "fail", "matched_targets": pages}
    targets = [item for item in case.get("targets") or [] if isinstance(item, dict)]
    if not targets:
        return {"status": "fail", "reason": "no_target_page"}
    matched = []
    for target in targets:
        rows = _matched_records(observation, target)
        evidence = [str(token) for token in target.get("discovery_evidence") or []]
        if rows and _text_has(rows, evidence, ("title", "snippet")):
            matched.append(target.get("id") or target.get("url"))
    require_all = bool(case.get("require_all_targets", True))
    if require_all and len(matched) == len(targets):
        status = "pass"
    elif matched:
        status = "partial"
    else:
        status = "fail"
    return {"status": status, "matched_targets": matched, "required": len(targets)}


def _reading_status(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    fetched = _records(observation, "fetched")
    if not fetched:
        return {"status": "not_attempted"}
    targets = [item for item in case.get("targets") or [] if isinstance(item, dict)] or [{}]
    ok = True
    for target in targets:
        rows = [item for item in fetched if item.get("url") and _target_url_match(str(item["url"]), target)]
        evidence = [str(token) for token in (target.get("reading_evidence") or target.get("discovery_evidence") or [])]
        if not rows or not _text_has(rows, evidence, ("content",)):
            ok = False
    return {"status": "pass" if ok else "fail"}


def _comment_is_exact(target: dict[str, Any]) -> bool:
    if target.get("match") == "content":
        return False
    if target.get("match") == "exact_id":
        return True
    return str(target.get("comment_id") or "").isdigit()


def _comment_status(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    if "comments" not in observation:
        return {"status": "not_attempted"}
    found = _records(observation, "comments")
    required = [item for item in case.get("comment_targets") or [] if isinstance(item, dict)]
    if not required:
        return {"status": "fail", "reason": "comment_case_has_no_target_comment"}
    page_targets = [item for item in case.get("targets") or [] if isinstance(item, dict)]
    matched = []
    for index, target in enumerate(required):
        evidence = [str(token) for token in target.get("evidence") or []]
        label = str(target.get("comment_id") or index)
        for item in found:
            text = str(item.get("text") or "")
            if not _text_has([{"text": text}], evidence, ("text",)):
                continue
            if _comment_is_exact(target):
                if str(item.get("comment_id")) == str(target.get("comment_id")):
                    matched.append(label)
                    break
                continue
            source = str(item.get("url") or "")
            if source and any(_target_url_match(source, page) for page in page_targets):
                matched.append(label)
                break
    require_all = bool(case.get("require_all_comments", True))
    if require_all and len(matched) == len(required):
        status = "pass"
    elif matched:
        status = "partial"
    else:
        status = "fail"
    return {"status": status, "matched_comments": matched}


def _cross_language_status(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    wanted = [host.lower() for host in case.get("cross_language_hosts") or []]
    if not wanted:
        return {"status": "not_applicable"}
    found = []
    for target in case.get("targets") or []:
        if not isinstance(target, dict):
            continue
        if str(target.get("host") or "").lower() not in wanted:
            continue
        if _matched_records(observation, target):
            found.append(target.get("host"))
    return {"status": "pass" if len(found) == len(wanted) else "fail", "matched_hosts": found}


def _synthesis_status(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    answer = str(observation.get("answer") or "")
    facts = [str(fact) for fact in case.get("key_facts") or [] if str(fact).strip()]
    if not answer.strip():
        return {"status": "fail", "reason": "empty_answer"}
    missing = [fact for fact in facts if fact.lower() not in answer.lower()]
    return {"status": "pass" if not missing else "fail", "missing": missing}


def _freshness_status(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    if observation.get("window_has_story") is False:
        return {"status": "not_answerable", "reason": "no_story_in_window"}
    spec = case.get("freshness") or {}
    window_hours = float(spec.get("window_hours") or 0)
    executed = observation.get("executed_at")
    stamps = [
        item.get("published_at")
        for item in _records(observation, "results")
        if item.get("published_at")
    ]
    if not executed or not stamps:
        return {"status": "unverifiable"}
    from datetime import datetime, timezone

    def _parse(value: str) -> datetime:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    start = _parse(str(executed))
    fresh = []
    for stamp in stamps:
        age = (start - _parse(str(stamp))).total_seconds() / 3600
        if 0 <= age <= window_hours:
            fresh.append(str(stamp))
    return {"status": "pass" if fresh else "fail", "fresh_published_at": fresh}


def observed_access_failure(observation: dict[str, Any]) -> str | None:
    """Use only markers in the observation. The case's expected access is not evidence."""
    parts: list[str] = []
    for key in ("results", "fetched", "errors", "comments"):
        for item in _records(observation, key):
            parts.extend(str(item.get(field) or "") for field in ("title", "snippet", "content", "text", "message", "code"))
    combined = "\n".join(parts).lower()
    for label, markers in ACCESS_MARKERS.items():
        if any(marker in combined for marker in markers):
            return label
    return None


def score_observation(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Component scores stay separate. A required step that was not attempted fails the task."""
    dimensions = set(case.get("dimensions") or [])
    scores: dict[str, dict[str, Any]] = {}
    if "discovery" in dimensions:
        scores["discovery"] = _discovery_status(case, observation)
    if "reading" in dimensions:
        scores["reading"] = _reading_status(case, observation)
    if "comments" in dimensions:
        scores["comments"] = _comment_status(case, observation)
    if "cross_language" in dimensions:
        scores["cross_language"] = _cross_language_status(case, observation)
    if "synthesis" in dimensions:
        scores["synthesis"] = _synthesis_status(case, observation)
    if "freshness" in dimensions or case.get("freshness"):
        scores["freshness"] = _freshness_status(case, observation)
    required = list(case.get("task_requires") or [])
    reasons = []
    for name in required:
        status = (scores.get(name) or {"status": "not_attempted"})["status"]
        if status != "pass":
            reasons.append(f"{name}:{status}")
    observed = observed_access_failure(observation)
    if any(reason.endswith(":not_answerable") for reason in reasons):
        task_status = "not_answerable"
    elif observed and any(reason.startswith(("reading:", "comments:")) for reason in reasons):
        task_status = "blocked"
    elif reasons:
        task_status = "fail"
    else:
        task_status = "pass"
    return {
        "id": case["id"],
        "scores": scores,
        "task": {"status": task_status, "reasons": reasons},
        "expected_access": case.get("expected_access") or case.get("access"),
        "observed_failure": observed,
        "algorithm_miss": task_status == "fail" and observed is None,
    }


def provider_query(case: dict[str, Any]) -> str:
    """Text that may be sent to a provider. Answer keys stay out of this string."""
    return str(case.get("query") or "")


def compare_protocol() -> dict[str, Any]:
    return {
        "arms": ["allsearch-balanced", "baseline"],
        "same_query": True,
        "same_deadline_seconds": 45,
        "allsearch_depth": "balanced",
        "baseline": "One search per case from Pi Web Access or another single search API. Same query text, no rewriting, 45 second cap. Save the same observation JSON.",
        "do_not_mix": "verify and deep are extra arms. Do not average them into the balanced comparison.",
        "score": "Compare each dimension separately. Do not publish one combined score.",
    }


def _print(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


PILOT_IDS = (
    "history-rfc791",
    "compare-react-vue",
    "v2ex-macos",
    "hn-comment-dissent",
    "news-fresh-query",
)


def pilot_plan(cases: list[dict[str, Any]], config: AllSearchConfig) -> dict[str, Any]:
    selected = [case for case in cases if case["id"] in PILOT_IDS]
    plans = [plan_case(case, config, depth="balanced") for case in selected]
    totals = budget(plans)
    return {
        "ids": [case["id"] for case in selected],
        "deadline_seconds_per_case": 45,
        "serial_time_ceiling_seconds": 45 * len(selected),
        "stop": "Stop the run on the second 401 or 429 from one provider, or when a case exceeds 45 seconds.",
        "search_calls": totals,
        "on_demand_reads": "After search, fetch at most 2 already discovered URLs for cases whose task requires reading or comments. Do not search again.",
        "firecrawl_read_ceiling": 2 * sum(
            1 for case in selected if {"reading", "comments"} & set(case.get("task_requires") or [])
        ),
        "cost": "Call counts are the ceiling. Do not convert them to dollars without the account's current price.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AllSearch offline search benchmark")
    parser.add_argument("command", choices=["plan", "budget", "score", "protocol", "pilot", "live"])
    parser.add_argument("--depth", default="balanced")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--observations", type=Path)
    return _run(parser.parse_args(argv))


def _run(args: argparse.Namespace) -> int:
    if args.command == "live":
        print(
            "Refusing live benchmark. It would call Grok, Tavily, or Firecrawl. "
            "Run plan/budget first and ask before any paid execution.",
            file=sys.stderr,
        )
        return 2
    if args.command == "protocol":
        _print(compare_protocol())
        return 0
    cases = load_cases(args.cases)
    if args.command == "pilot":
        from allsearch.config import load_config

        _print(pilot_plan(cases, load_config()))
        return 0
    if args.command == "score":
        if args.observations is None:
            print("score requires --observations", file=sys.stderr)
            return 2
        observed = {
            item["id"]: item
            for item in json.loads(args.observations.read_text(encoding="utf-8"))
            if isinstance(item, dict) and item.get("id")
        }
        _print({"results": [score_observation(case, observed.get(case["id"], {})) for case in cases]})
        return 0
    from allsearch.config import load_config

    config = load_config()
    plans = [plan_case(case, config, depth=args.depth) for case in cases]
    if args.command == "budget":
        _print(budget(plans))
        return 0
    _print({"budget": budget(plans), "plans": plans})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
