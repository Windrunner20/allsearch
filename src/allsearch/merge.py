"""URL dedupe, ranking, provenance, citations, and evidence metrics."""

from __future__ import annotations

from allsearch.models import (
    CitationItem,
    EvidenceMetrics,
    ProviderResultItem,
    ProviderSearchResult,
    ResultItem,
)
from allsearch.security import canonicalize_url, hostname_of


def _quality_score(item: ProviderResultItem | ResultItem) -> tuple[int, int, int]:
    title_len = len((item.title or "").strip())
    snippet_len = len((item.snippet or "").strip())
    content_len = len((item.content or "").strip()) if item.content else 0
    return (content_len, snippet_len, title_len)


def merge_provider_results(
    provider_results: list[ProviderSearchResult],
    *,
    max_results: int,
    primary_provider: str = "xai",
) -> tuple[list[ResultItem], list[CitationItem], EvidenceMetrics]:
    """Merge with primary-first rank, URL canonicalization, and provenance."""
    # Preserve provider order with primary first
    ordered = sorted(
        provider_results,
        key=lambda r: (0 if r.provider == primary_provider else 1, provider_results.index(r)),
    )

    variants: dict[str, list[ProviderResultItem]] = {}
    providers_by_key: dict[str, list[str]] = {}
    sequences: list[list[str]] = []

    for pr in ordered:
        seq: list[str] = []
        for item in pr.results:
            key = canonicalize_url(item.url) if item.url else f"no-url:{pr.provider}:{item.title}:{item.snippet[:40]}"
            if not key:
                continue
            seq.append(key)
            variants.setdefault(key, []).append(item)
            plist = providers_by_key.setdefault(key, [])
            prov = item.provider or pr.provider
            if prov not in plist:
                plist.append(prov)
        sequences.append(seq)

    merged_keys: list[str] = []
    indexes = [0 for _ in sequences]
    seen: set[str] = set()
    while len(merged_keys) < max_results and sequences:
        progressed = False
        for seq_i, sequence in enumerate(sequences):
            if len(merged_keys) >= max_results:
                break
            while indexes[seq_i] < len(sequence):
                key = sequence[indexes[seq_i]]
                indexes[seq_i] += 1
                if key in seen:
                    continue
                seen.add(key)
                merged_keys.append(key)
                progressed = True
                break
        if not progressed:
            break

    results: list[ResultItem] = []
    cross = 0
    for i, key in enumerate(merged_keys):
        items = variants.get(key) or []
        if not items:
            continue
        best = max(items, key=_quality_score)
        matched = providers_by_key.get(key) or [best.provider]
        if len(matched) > 1:
            cross += 1
        # Prefer primary as provider label when present
        provider = primary_provider if primary_provider in matched else matched[0]
        results.append(
            ResultItem(
                id=f"r{i + 1}",
                title=best.title,
                url=best.url,
                snippet=best.snippet,
                content=best.content,
                published_at=best.published_at,
                source_type=best.source_type,
                provider=provider,
                matched_providers=matched,
                score=best.score,
                vertical=best.vertical,
                metadata=dict(best.metadata or {}),
            )
        )

    # Citations: align to results, then leftover unique URLs from providers
    citations: list[CitationItem] = []
    cited_urls: set[str] = set()
    for item in results:
        if not item.url:
            continue
        canon = canonicalize_url(item.url)
        if canon in cited_urls:
            continue
        cited_urls.add(canon)
        citations.append(
            CitationItem(
                id=f"c{len(citations) + 1}",
                title=item.title,
                url=item.url,
                result_id=item.id,
                providers=list(item.matched_providers),
            )
        )

    for pr in ordered:
        for cit in pr.citations:
            url = cit.get("url") or ""
            if not url:
                continue
            canon = canonicalize_url(url)
            if canon in cited_urls:
                continue
            cited_urls.add(canon)
            citations.append(
                CitationItem(
                    id=f"c{len(citations) + 1}",
                    title=cit.get("title") or url,
                    url=url,
                    result_id=None,
                    providers=[pr.provider],
                )
            )

    domains = {hostname_of(r.url) for r in results if r.url and hostname_of(r.url)}
    primary_cites = 0
    for pr in ordered:
        if pr.provider == primary_provider:
            primary_cites = len([c for c in pr.citations if c.get("url")])
            break

    evidence = EvidenceMetrics(
        unique_urls=len({canonicalize_url(r.url) for r in results if r.url}),
        unique_domains=len(domains),
        cross_provider_matches=cross,
        pages_fetched=0,  # populated by orchestrator only for actual Firecrawl fetches
        primary_citation_count=primary_cites,
    )
    return results, citations, evidence


def attach_fetched_content(results: list[ResultItem], url: str, content: str, title: str = "") -> None:
    canon = canonicalize_url(url)
    for item in results:
        if canonicalize_url(item.url) == canon:
            item.content = content
            if title and not item.title:
                item.title = title
            if "firecrawl" not in item.matched_providers:
                item.matched_providers = list(item.matched_providers) + ["firecrawl"]
            return
