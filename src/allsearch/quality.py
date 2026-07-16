"""Primary sufficiency, scrape selection, and content-quality checks."""

from __future__ import annotations

from dataclasses import dataclass

from allsearch.config import AllSearchConfig
from allsearch.models import ProviderSearchResult, ResultItem
from allsearch.security import hostname_of


@dataclass(slots=True)
class SufficiencyResult:
    sufficient: bool
    reasons: list[str]
    citation_count: int
    unique_domains: int


def evaluate_primary_sufficiency(
    primary: ProviderSearchResult | None,
    config: AllSearchConfig,
    *,
    depth: str,
    include_domains: list[str] | None = None,
    comparison: bool = False,
    recency: bool = False,
    vertical_expected: bool = False,
) -> SufficiencyResult:
    reasons: list[str] = []
    if primary is None:
        return SufficiencyResult(False, ["primary_missing"], 0, 0)

    citations = [c for c in primary.citations if c.get("url")]
    urls = [c.get("url", "") for c in citations]
    if not urls:
        urls = [r.url for r in primary.results if r.url]
    citation_count = len({u for u in urls if u})
    domains = {hostname_of(u) for u in urls if hostname_of(u)}
    unique_domains = len(domains)

    if not (primary.answer or "").strip() and citation_count == 0:
        reasons.append("empty_answer_and_citations")

    min_citations = config.reliability.min_primary_citations
    min_domains = config.reliability.min_unique_domains
    if depth == "fast":
        min_citations = max(1, min_citations // 2)
        min_domains = max(1, min_domains // 2)

    if citation_count < min_citations:
        reasons.append(f"citations_below_threshold:{citation_count}<{min_citations}")
    if unique_domains < min_domains:
        reasons.append(f"domains_below_threshold:{unique_domains}<{min_domains}")

    if include_domains:
        allowed = {d.lower().lstrip(".") for d in include_domains}
        if domains and not any(any(host == a or host.endswith("." + a) for a in allowed) for host in domains):
            reasons.append("domain_filter_not_satisfied")

    if comparison and unique_domains < 2:
        reasons.append("comparison_needs_multiple_domains")

    if recency and citation_count == 0:
        reasons.append("recency_without_sources")

    if vertical_expected and citation_count == 0 and not primary.results:
        reasons.append("vertical_without_structured_evidence")

    # long prose without evidence is insufficient for balanced+
    if depth in {"balanced", "verify", "deep"} and citation_count == 0 and len((primary.answer or "")) > 200:
        reasons.append("long_answer_without_citations")

    sufficient = not reasons
    return SufficiencyResult(sufficient, reasons, citation_count, unique_domains)


def content_quality_issue(content: str) -> str | None:
    text = (content or "").strip()
    if not text:
        return "empty_content"
    lowered = text.lower()
    if len(text) < 40:
        return "content_too_short"
    markers = ("captcha", "access denied", "enable javascript", "cf-browser-verification", "are you a robot")
    if any(m in lowered for m in markers):
        return "anti_bot_shell"
    return None


def select_urls_for_scrape(
    results: list[ResultItem],
    *,
    budget: int,
    prefer_domains: list[str] | None = None,
) -> list[str]:
    if budget <= 0:
        return []
    prefer = {d.lower().lstrip(".") for d in (prefer_domains or [])}
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for idx, item in enumerate(results):
        url = item.url
        if not url or url in seen:
            continue
        if not url.startswith("http"):
            continue
        seen.add(url)
        host = hostname_of(url)
        score = 0
        if item.content:
            score -= 5  # already has content
        if not (item.snippet or "").strip():
            score += 3
        if host in prefer or any(host.endswith("." + d) for d in prefer):
            score += 4
        if item.provider == "xai":
            score += 1
        if len(item.matched_providers) > 1:
            score += 2
        scored.append((score, -idx, url))
    scored.sort(reverse=True)
    return [u for _, _, u in scored[:budget]]
