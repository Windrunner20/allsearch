"""Pure query classification and staged execution-plan construction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from allsearch.config import AllSearchConfig
from allsearch.models import SearchDepth, SearchMode, SearchRequest

VerticalName = str

NEWS_RE = re.compile(
    r"\b(today|latest|breaking|headline|news|yesterday|this week|当前|最新|今日|新闻)\b",
    re.I,
)
DOCS_RE = re.compile(
    r"\b(docs?|documentation|api reference|readme|tutorial|how to|指南|文档)\b",
    re.I,
)
COMPARE_RE = re.compile(
    r"\b(vs\.?|versus|compare|comparison|difference|对比|比较)\b",
    re.I,
)

VERTICAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("security", re.compile(r"\bCVE-\d{4}-\d+\b", re.I)),
    ("academic", re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+|PMID[:\s]?\d+|arXiv:\d{4}\.\d+)\b", re.I)),
    ("finance", re.compile(r"\b(\$[A-Z]{1,5}|NYSE:|NASDAQ:|ticker)\b")),
    ("legal", re.compile(r"\b(\d+\s+U\.?S\.?C\.?|v\.\s+[A-Z]|\bcase\b|\bcourt\b)\b", re.I)),
    ("code", re.compile(r"\b(npm|pypi|crates\.io|github\.com/[\w.-]+/[\w.-]+|pip install|go module)\b", re.I)),
    ("travel", re.compile(r"\b([A-Z]{3}\s*[-–]\s*[A-Z]{3}|flight\s+\w+\d+|IATA)\b", re.I)),
    ("health", re.compile(r"\b(clinical trial|FDA|ICD-10|symptom|dosage)\b", re.I)),
    ("ip", re.compile(r"\b(patent|USPTO|trademark)\b", re.I)),
]


@dataclass(slots=True)
class QuerySignals:
    recency: bool = False
    docs: bool = False
    comparison: bool = False
    verticals: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderStage:
    provider: str
    operation: str
    reason: str
    parallel_group: int = 0  # same group runs concurrently
    search_depth: Literal["basic", "advanced"] | None = None
    topic: Literal["general", "news"] = "general"
    vertical: str | None = None
    max_results: int = 8
    max_pages: int = 0


@dataclass(slots=True)
class ExecutionPlan:
    depth: SearchDepth
    mode: SearchMode
    signals: QuerySignals
    stages: list[ProviderStage]
    reasons: list[str]
    total_deadline_seconds: float
    max_results: int
    scrape_after_merge: bool
    scrape_budget: int


def classify_query(request: SearchRequest, allowed_verticals: tuple[str, ...] | list[str]) -> QuerySignals:
    q = request.query
    signals = QuerySignals(
        recency=bool(NEWS_RE.search(q)) or request.mode == "news",
        docs=bool(DOCS_RE.search(q)) or request.mode in {"docs", "research"},
        comparison=bool(COMPARE_RE.search(q)),
    )
    allowed = {v.lower() for v in allowed_verticals}

    if request.vertical:
        v = request.vertical.strip().lower()
        if v in allowed or not allowed:
            signals.verticals = [v]
        else:
            signals.verticals = [v]
        return signals

    found: list[str] = []
    for name, pattern in VERTICAL_PATTERNS:
        if name not in allowed and allowed:
            continue
        if pattern.search(q):
            found.append(name)
    # keyword soft matches for explicit mode vertical
    if request.mode == "vertical" and not found:
        for name in allowed:
            if name and name in q.lower():
                found.append(name)
                break
    signals.verticals = found[:3]
    return signals


def build_execution_plan(
    request: SearchRequest,
    config: AllSearchConfig,
    *,
    provider_available: dict[str, bool] | None = None,
) -> ExecutionPlan:
    """Build deterministic plan. provider_available gates stages without I/O."""
    available = provider_available or {
        "xai": config.xai.configured(),
        "tavily": config.tavily.configured(),
        "anysearch": config.anysearch.configured(),
        "firecrawl": config.firecrawl.configured(),
    }
    signals = classify_query(request, config.anysearch.verticals)
    depth = request.depth
    mode = request.mode
    reasons: list[str] = []
    stages: list[ProviderStage] = []

    max_results = request.max_results
    tavily_depth: Literal["basic", "advanced"] = (
        "advanced" if depth in {"verify", "deep"} else config.tavily.default_depth
    )
    topic: Literal["general", "news"] = "news" if (signals.recency or mode == "news") else "general"

    # Group 0: ONLY the primary (xAI/Grok) runs alone first — honors the
    # "Grok preferred" pipeline. All supplements run in group 1 (after primary),
    # concurrent among themselves. Firecrawl remains a post-merge stage.
    if available.get("xai"):
        stages.append(
            ProviderStage(
                provider="xai",
                operation="search",
                reason="primary",
                parallel_group=0,
                max_results=max_results,
            )
        )
        reasons.append("xai_primary")
    else:
        reasons.append("xai_unavailable")

    high_conf_vertical = bool(signals.verticals) and (
        request.vertical is not None or depth in {"verify", "deep"} or mode == "vertical"
    )
    # strong vertical identifiers drive AnySearch in group 1
    strong_vertical = any(p.search(request.query) for _, p in VERTICAL_PATTERNS)

    def add_anysearch(group: int, reason: str) -> None:
        if not available.get("anysearch") or not signals.verticals:
            return
        for vertical in signals.verticals[:1]:
            stages.append(
                ProviderStage(
                    provider="anysearch",
                    operation="search",
                    reason=reason,
                    parallel_group=group,
                    vertical=vertical,
                    max_results=min(max_results, config.anysearch.max_results),
                )
            )
            reasons.append(f"anysearch_{reason}:{vertical}")

    def add_tavily(group: int, reason: str) -> None:
        if not available.get("tavily"):
            return
        stages.append(
            ProviderStage(
                provider="tavily",
                operation="search",
                reason=reason,
                parallel_group=group,
                search_depth=tavily_depth,
                topic=topic,
                max_results=min(max_results, config.tavily.max_results),
            )
        )
        reasons.append(f"tavily_{reason}")

    scrape_after = False
    scrape_budget = 0

    if depth == "fast":
        if request.vertical or strong_vertical:
            add_anysearch(1, "strong_vertical")
        add_tavily(1, "conditional_primary_insufficient")
        scrape_after = False
        scrape_budget = 0
        reasons.append("depth_fast")
    elif depth == "balanced":
        if high_conf_vertical or strong_vertical or request.vertical:
            add_anysearch(1, "high_confidence_vertical")
        add_tavily(1, "conditional_supplement")
        scrape_after = False
        scrape_budget = config.reliability.auto_scrape_top_n
        reasons.append("depth_balanced")
    elif depth == "verify":
        add_tavily(1, "verify_supplement")
        if signals.verticals:
            add_anysearch(1, "verify_vertical")
        scrape_after = available.get("firecrawl", False)
        scrape_budget = min(2, config.firecrawl.max_pages)
        reasons.append("depth_verify")
    else:  # deep
        add_tavily(1, "deep_supplement")
        if signals.verticals:
            add_anysearch(1, "deep_vertical")
        scrape_after = available.get("firecrawl", False)
        scrape_budget = min(config.firecrawl.max_pages, max(config.reliability.auto_scrape_top_n, 3))
        reasons.append("depth_deep")

    if signals.recency:
        reasons.append("recency_signal")
    if signals.docs:
        reasons.append("docs_signal")
    if signals.comparison:
        reasons.append("comparison_signal")

    return ExecutionPlan(
        depth=depth,
        mode=mode,
        signals=signals,
        stages=stages,
        reasons=reasons,
        total_deadline_seconds=config.reliability.total_budget_seconds,
        max_results=max_results,
        scrape_after_merge=scrape_after and scrape_budget > 0,
        scrape_budget=scrape_budget,
    )


def tavily_required_after_primary(
    *,
    depth: SearchDepth,
    primary_ok: bool,
    primary_sufficient: bool,
    signals: QuerySignals,
    tavily_planned: bool,
) -> bool:
    if not tavily_planned:
        return False
    if depth in {"verify", "deep"}:
        return True
    if not primary_ok:
        return True
    if not primary_sufficient:
        return True
    if signals.recency and depth in {"balanced", "verify"}:
        return True
    return False
