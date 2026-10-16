"""URL dedupe, ranking, provenance, citations, and evidence metrics."""

from __future__ import annotations

import re

from allsearch.models import (
    CitationItem,
    EvidenceMetrics,
    ProviderResultItem,
    ProviderSearchResult,
    ResultItem,
)
from allsearch.security import canonicalize_url, hostname_of, matches_domain_filter


def dedupe_key(url: str) -> str:
    """Key under which two URLs are the same page.

    Forum threads collapse by (platform, id), so mirrors and variants such as
    hk.v2ex.com/t/1, v2ex.com/t/1?p=2 and www.v2ex.com/t/1 merge. Other URLs
    use the canonical form with a leading ``www.`` removed.
    """
    from allsearch.native import thread_ref

    ref = thread_ref(url)
    if ref is not None:
        return f"thread:{ref[0]}:{ref[1]}"
    canonical = canonicalize_url(url)
    return re.sub(r"^(https?://)www\.", r"\1", canonical)


def _public_url(url: str) -> str:
    """Emit the dedupe key when it is a real HTTP URL.

    Tracking parameters and fragments are removed for the citation the model
    sees, and forum mirrors map to the official thread URL. Malformed URLs stay
    untouched so distinct failures do not collapse.
    """
    from allsearch.native import canonical_thread_url

    canonical = canonicalize_url(canonical_thread_url(url))
    if canonical.startswith(("http://", "https://")):
        return canonical
    return url


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
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> tuple[list[ResultItem], list[CitationItem], EvidenceMetrics]:
    """Merge with primary-first rank, URL canonicalization, and provenance.

    When domain filters are supplied they are applied before variant/citation
    construction, so the final structured results, citations, and evidence all
    respect include/exclude. Items without a URL are dropped when
    ``include_domains`` is set and kept otherwise.
    """
    if include_domains or exclude_domains:
        provider_results = [
            pr.model_copy(
                update={
                    "results": [
                        i
                        for i in pr.results
                        if matches_domain_filter(
                            hostname_of(i.url or ""),
                            include_domains=include_domains,
                            exclude_domains=exclude_domains,
                        )
                    ],
                    "citations": [
                        c
                        for c in pr.citations
                        if matches_domain_filter(
                            hostname_of(c.get("url") or ""),
                            include_domains=include_domains,
                            exclude_domains=exclude_domains,
                        )
                    ],
                }
            )
            for pr in provider_results
        ]
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
            key = dedupe_key(item.url) if item.url else f"no-url:{pr.provider}:{item.title}:{item.snippet[:40]}"
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
                url=_public_url(best.url),
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
        canon = dedupe_key(item.url)
        if canon in cited_urls:
            continue
        cited_urls.add(canon)
        citations.append(
            CitationItem(
                id=f"c{len(citations) + 1}",
                title=item.title,
                url=_public_url(item.url),
                result_id=item.id,
                providers=list(item.matched_providers),
            )
        )

    for pr in ordered:
        for cit in pr.citations:
            url = cit.get("url") or ""
            if not url:
                continue
            canon = dedupe_key(url)
            if canon in cited_urls:
                continue
            cited_urls.add(canon)
            citations.append(
                CitationItem(
                    id=f"c{len(citations) + 1}",
                    title=cit.get("title") or url,
                    url=_public_url(url),
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
        unique_urls=len({dedupe_key(r.url) for r in results if r.url}),
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
