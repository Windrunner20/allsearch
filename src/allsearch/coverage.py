"""Check evidence against what the query asked for.

Each requirement from ``intent.analyze`` becomes a ``Requirement`` that is
either satisfied by concrete evidence (a URL on the requested site's content
pages, a comment read through a platform API, a dated result inside the
window, …) or not, with a short reason. The orchestrator uses the unmet ones
to decide which targeted searches are worth their cost; clients receive the
same list so they can tell "found nothing" from "found the wrong kind of thing".

Citation counts and domain counts are deliberately not requirements: three
citations from three unrelated sites do not answer "find the V2EX thread".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

from allsearch.intent import QueryIntent, SiteRequirement, language_of
from allsearch.models import Coverage, Requirement, ResultItem, ThreadEvidence
from allsearch.native import terms


def _site_hits(req: SiteRequirement, urls: list[str]) -> tuple[list[str], list[str]]:
    """(content-page URLs, any URLs) on the required site."""
    on_site = [u for u in urls if any(_owns(d, u) for d in req.domains)]
    content = [u for u in on_site if req.site.is_content_url(u)]
    return content, on_site


def _owns(domain: str, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _entity_present(entity: str, blobs: list[str]) -> bool:
    wanted = set(terms(entity))
    if not wanted:
        return True
    for blob in blobs:
        have = set(terms(blob))
        if len(wanted & have) >= max(1, round(len(wanted) * 0.6)):
            return True
    return False


def _blob(item: ResultItem) -> str:
    url = unquote(item.url or "")
    url_words = re.sub(r"[/_.\-?=&#:]+", " ", url)
    return f"{item.title} {item.snippet} {url_words}"


def evaluate(
    intent: QueryIntent,
    results: list[ResultItem],
    citation_urls: list[str],
    threads: list[ThreadEvidence],
    *,
    now: datetime | None = None,
) -> Coverage:
    urls = list(dict.fromkeys([r.url for r in results if r.url] + [u for u in citation_urls if u]))
    reqs: list[Requirement] = []

    site_status: list[bool] = []
    for req in intent.sites:
        content, on_site = _site_hits(req, urls)
        ok = bool(content) if req.content_page else bool(on_site)
        site_status.append(ok)
        if ok:
            detail = f"{len(content)} content page(s), e.g. {content[0]}" if content else f"{len(on_site)} page(s)"
        elif on_site:
            detail = f"only non-content pages on {req.site.name} (home, profile or list pages)"
        else:
            detail = f"no result from {', '.join(req.domains)}"
        if req.site.access != "public" and not ok:
            detail += f"; {req.site.name} usually requires {req.site.access.replace('_', '-')}"
        reqs.append(Requirement(kind="site", target=req.site.name, satisfied=ok, detail=detail))
    if len(intent.sites) >= 2:
        needed = "all" if intent.all_sites else "any"
        met = all(site_status) if intent.all_sites else any(site_status)
        reqs.append(
            Requirement(
                kind="sources",
                target=needed,
                satisfied=met,
                detail=f"{sum(site_status)} of {len(site_status)} requested sites covered",
            )
        )

    if intent.comments:
        read = [t for t in threads if t.status == "comments_read" and t.comments]
        relevant = [t for t in read if any((c.relevance or 0) > 0 for c in t.comments)] if intent.comment_focus else read
        ok = bool(relevant)
        if ok:
            total = sum(len(t.comments) for t in relevant)
            detail = f"{total} comment(s) read via API from {len(relevant)} thread(s)"
        elif read:
            detail = "comments read, none matched the described comment"
        elif threads:
            reasons = sorted({t.error or t.status for t in threads})
            detail = "thread found but comments not read: " + ", ".join(reasons)
        else:
            readable = [r for r in intent.sites if r.site.reader]
            detail = (
                "no thread from a site with a public comment API was found"
                if readable or not intent.sites
                else f"no public comment API for {', '.join(r.site.name for r in intent.sites)}"
            )
        reqs.append(Requirement(kind="comments", target=intent.comment_focus or "comments", satisfied=ok, detail=detail))

    if intent.recency_hours:
        current = now or datetime.now(UTC)
        window = timedelta(hours=intent.recency_hours)
        dated = [(r, _parse_time(r.published_at)) for r in results]
        fresh = [r for r, t in dated if t is not None and timedelta(0) <= current - t <= window]
        undated = sum(1 for _, t in dated if t is None)
        detail = (
            f"{len(fresh)} result(s) published in the last {intent.recency_hours}h"
            if fresh
            else f"no dated result in the last {intent.recency_hours}h ({undated} result(s) have no date)"
        )
        reqs.append(Requirement(kind="recency", target=f"{intent.recency_hours}h", satisfied=bool(fresh), detail=detail))

    if intent.target_language and intent.target_language_explicit:
        lang = intent.target_language
        matching = [r for r in results if r.title and language_of(r.title) == lang]
        detail = f"{len(matching)} result(s) with {lang} titles" if matching else f"no result with a {lang} title"
        reqs.append(Requirement(kind="language", target=lang, satisfied=bool(matching), detail=detail))

    if len(intent.entities) >= 2:
        blobs = [_blob(r) for r in results]
        for entity in intent.entities:
            ok = _entity_present(entity, blobs)
            reqs.append(
                Requirement(
                    kind="entity",
                    target=entity,
                    satisfied=ok,
                    detail="covered by a result title, snippet or URL" if ok else "no result is about this side",
                )
            )

    if not reqs:
        return Coverage(status="not_applicable", requirements=[])
    any_of = len(intent.sites) >= 2 and not intent.all_sites
    # In "any of" queries the per-site rows are informational; "sources" decides.
    sufficient = all(r.satisfied for r in reqs if not (any_of and r.kind == "site"))
    return Coverage(status="sufficient" if sufficient else "insufficient", requirements=reqs)


def unmet(coverage: Coverage, kind: str) -> list[Requirement]:
    return [r for r in coverage.requirements if r.kind == kind and not r.satisfied]
