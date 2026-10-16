"""Full search/fetch/health composition pipeline."""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from allsearch.cache import MemoryCache
from allsearch.config import AllSearchConfig
from allsearch.deadline import Deadline
from allsearch.errors import (
    AllSearchError,
    DeadlineExceededError,
    ProviderUnavailableError,
    UnsafeURLError,
    to_public_error,
)
from allsearch.coverage import evaluate as evaluate_coverage
from allsearch.coverage import unmet
from allsearch.health import HealthRegistry
from allsearch.intent import QueryIntent, analyze
from allsearch.merge import attach_fetched_content, dedupe_key, merge_provider_results
from allsearch.models import (
    CacheMeta,
    Coverage,
    ResultItem,
    ThreadEvidence,
    ProviderResultItem,
    ErrorItem,
    FetchRequest,
    FetchResponse,
    HealthResponse,
    ProviderHealth,
    ProviderSearchResult,
    RouteInfo,
    RouteStage,
    SearchRequest,
    SearchResponse,
)
from allsearch.providers.anysearch import AnySearchProvider
from allsearch.providers.firecrawl import FirecrawlProvider
from allsearch.providers.tavily import TavilyProvider
from allsearch.native import NativeSources, render_thread, terms, thread_ref
from allsearch.providers.xai import XAIProvider, rewrite_query
from allsearch.quality import (
    access_issue,
    content_quality_issue,
    evaluate_primary_sufficiency,
    select_urls_for_scrape,
)
from allsearch.routing import (
    ProviderStage,
    build_execution_plan,
    tavily_required_after_primary,
)
from allsearch.security import canonicalize_url, hostname_of, validate_public_http_url
from allsearch.sites import site_for_url
from allsearch.transport import HttpTransport

# Internal per-page cap for automatic post-merge scraping (not configurable).
AUTO_SCRAPE_MAX_CHARS = 30_000
# Share of the total budget the primary may use before supplements start anyway.
PRIMARY_SHARE = 0.65
MIN_BUDGET_FOR_PRIMARY_CAP = 20.0


@dataclass(slots=True)
class _StageResult:
    """Structured outcome of one provider stage so records commit in plan order.

    ``provider_result`` is carried back (not written to shared state inside the
    coroutine) so completed results survive a concurrent phase timeout and are
    committed deterministically by the caller.
    """

    stage: RouteStage
    provider_result: ProviderSearchResult | None = None
    error: ErrorItem | None = None
    warnings: list[str] = field(default_factory=list)
    output_key: str | None = None


_ANSWER_LINK = re.compile(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)|(?<![(\[])(https?://[^\s<>\"'`)\]]+)")


def _answer_contexts(answer: str) -> dict[str, tuple[str, str]]:
    """canonical URL -> (markdown label, plain text of the answer block naming it)."""
    out: dict[str, tuple[str, str]] = {}
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:\d+[.)]|[-*•])\s)", answer or "")
    for block in blocks:
        plain = re.sub(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", r"\1", block)
        plain = re.sub(r"https?://\S+", "", plain)
        plain = re.sub(r"[*_`#>|]+|\[\w+:\d+\]", " ", plain)
        plain = " ".join(plain.split()).strip(" -–—:：·")
        for match in _ANSWER_LINK.finditer(block):
            url = (match.group(2) or match.group(3) or "").rstrip(".,;:!?*)]}>\u3002\uff0c")
            if not url.startswith(("http://", "https://")):
                continue
            label = " ".join((match.group(1) or "").replace("*", "").split())
            key = canonicalize_url(url)
            if key not in out or (label and not out[key][0]):
                out[key] = (label, plain[:300])
    return out


def _with_answer_links(primary: ProviderSearchResult) -> ProviderSearchResult:
    """Use the answer text to describe and extend Grok's citations.

    Grok cites bare URLs, and often writes the exact post URL only in prose
    ("https://x.com/OpenAI/status/…") while its citation list holds the pages
    it searched. Cited results get a title and snippet from the answer block
    that names them; URLs that appear only in the answer are appended. Both are
    marked in metadata so they can be told apart from engine snippets.
    """
    contexts = _answer_contexts(primary.answer or "")
    if not contexts:
        return primary
    results: list[ProviderResultItem] = []
    known: set[str] = set()
    for item in primary.results:
        key = canonicalize_url(item.url) if item.url else ""
        known.add(key)
        label, context = contexts.get(key, ("", ""))
        update: dict[str, Any] = {}
        if label and (not item.title or item.title.startswith("http")):
            update["title"] = label
        if context and not item.snippet:
            update["snippet"] = context
        if update:
            update["metadata"] = {**(item.metadata or {}), "snippet_source": "answer_context"}
            item = item.model_copy(update=update)
        results.append(item)
    known |= {canonicalize_url(c.get("url") or "") for c in primary.citations}
    added = 0
    for key, (label, context) in contexts.items():
        if key in known or added >= 10:
            continue
        known.add(key)
        added += 1
        results.append(
            ProviderResultItem(
                title=label,
                url=key,
                snippet=context,
                provider=primary.provider,
                metadata={"from_answer": True, "snippet_source": "answer_context"},
            )
        )
    return primary.model_copy(update={"results": results})


@dataclass(slots=True)
class _ScrapeResult:
    """Structured outcome of one post-merge scrape, free of shared side effects."""

    url: str
    stage: RouteStage
    content: str | None = None  # None when rejected/errored (nothing to attach)
    title: str = ""
    error: ErrorItem | None = None
    warnings: list[str] = field(default_factory=list)


def _collect_done(tasks: list[Any]) -> list[Any | None]:
    """Return each task's result in input order, or None if it did not finish
    successfully (cancelled by the deadline, or raised unexpectedly)."""
    out: list[Any | None] = []
    for task in tasks:
        if task.done() and not task.cancelled():
            try:
                out.append(task.result())
            except Exception:
                out.append(None)
        else:
            out.append(None)
    return out


_IDENT = re.compile(
    r"(?<![\w.])(?:v?\d+(?:\.\d+){1,3}|\d{3,}|[A-Za-z]+\d+[A-Za-z\d]*|[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+|[A-Z][A-Za-z0-9]*\.[a-z][A-Za-z0-9]+)(?![\w])"
)


def query_identifiers(query: str) -> list[str]:
    """Exact tokens worth matching in URLs and titles: numbers of 3+ digits,
    versions, alphanumeric codes, snake_case and Type.member names."""
    found = []
    for token in _IDENT.findall(query):
        if re.fullmatch(r"(19|20)\d\d", token):
            continue  # years match too many unrelated pages
        found.append(token)
    return list(dict.fromkeys(found))[:6]


def identifier_hits(identifiers: list[str], text: str) -> int:
    hay = (text or "").lower()
    hits = 0
    for ident in identifiers:
        low = ident.lower().lstrip("v")
        if low.isdigit():
            if re.search(r"(?<!\d)0*" + low + r"(?!\d)", hay):
                hits += 1
        elif low in hay:
            hits += 1
    return hits


def extractive_answer(threads: list[ThreadEvidence], limit: int = 3) -> str:
    """Answer text built only from comments read through platform APIs."""
    picked = []
    for thread in threads:
        for comment in thread.comments:
            if (comment.relevance or 0) > 0:
                picked.append((comment.relevance or 0, thread, comment))
    picked.sort(key=lambda item: item[0], reverse=True)
    if not picked:
        return ""
    lines = ["Most relevant comments read from the thread API (verbatim, not a summary):"]
    for _, thread, comment in picked[:limit]:
        text = " ".join(comment.text.split())
        if len(text) > 400:
            text = text[:399] + "…"
        lines.append(f"- {comment.author} ({comment.created_at or 'undated'}) {comment.url}: \"{text}\" — in \"{thread.title}\"")
    return "\n".join(lines)


def _next_steps(
    intent: QueryIntent,
    coverage: Coverage,
    results: list[ResultItem],
    threads: list[ThreadEvidence],
) -> list[dict[str, Any]]:
    """Concrete follow-ups for the client, derived only from unmet requirements."""
    steps: list[dict[str, Any]] = []
    if any(t.comments for t in threads):
        steps.append({"tool": "read_result", "fields": ["comments"], "why": "comments read through the platform API are stored with ids, authors and timestamps"})
    for thread in threads:
        if thread.status == "found":
            steps.append({"tool": "fetch", "url": thread.url, "why": f"comments not readable via API ({thread.error}); fetch reads the page if it is public"})
    for req in unmet(coverage, "site"):
        site = next((r for r in intent.sites if r.site.name == req.target), None)
        if site is not None:
            steps.append({
                "tool": "search",
                "query": intent.topic or intent.query,
                "include_domains": list(site.domains),
                "why": f"nothing from {site.site.name} yet; restrict the next search to it or report it as not found",
            })
    if unmet(coverage, "recency"):
        steps.append({"tool": "search", "query": intent.query, "why": "no dated result inside the window; try mode=news or report that none was found"})
    return steps[:4]


class Orchestrator:
    def __init__(
        self,
        config: AllSearchConfig,
        *,
        transport: HttpTransport | None = None,
        cache: MemoryCache | None = None,
        health: HealthRegistry | None = None,
        xai: XAIProvider | None = None,
        tavily: TavilyProvider | None = None,
        anysearch: AnySearchProvider | None = None,
        firecrawl: FirecrawlProvider | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or HttpTransport(
            timeout_seconds=config.reliability.timeout_seconds,
            max_response_bytes=config.reliability.max_response_bytes,
        )
        self.cache = cache or MemoryCache(max_entries=config.cache.max_entries)
        self.health_registry = health or HealthRegistry(
            failure_threshold=config.reliability.circuit_failure_threshold,
            open_seconds=config.reliability.circuit_open_seconds,
            probe_ttl_seconds=config.reliability.health_probe_ttl_seconds,
            cache=self.cache,
        )
        self.xai = xai or XAIProvider(config.xai, self.transport, self.health_registry)
        self.tavily = tavily or TavilyProvider(config.tavily, self.transport, self.health_registry)
        self.anysearch = anysearch or AnySearchProvider(
            config.anysearch, self.transport, self.health_registry, self.cache
        )
        self.firecrawl = firecrawl or FirecrawlProvider(config.firecrawl, self.transport, self.health_registry)
        self.native: NativeSources | None = (
            NativeSources(
                self.transport,
                self.health_registry,
                self.cache,
                reddit_client_id=config.reddit_client_id,
                reddit_client_secret=config.reddit_client_secret,
                stackexchange_key=config.stackexchange_key,
            )
            if config.native_sources
            else None
        )
        self._register_health()

    def _register_health(self) -> None:
        self.health_registry.register("xai", self.config.xai.configured())
        self.health_registry.register("tavily", self.config.tavily.configured())
        self.health_registry.register("anysearch", self.config.anysearch.configured())
        self.health_registry.register("firecrawl", self.config.firecrawl.configured())

    async def aclose(self) -> None:
        await self.transport.aclose()

    def _check_url(self, url: str) -> str:
        """Reject non-public destinations before Firecrawl fetches them.

        Scheme, credentials, localhost and literal private IPs are always
        rejected. Hostnames are resolved locally only when Firecrawl itself runs
        on an internal address (see ``fetch_dns_check``); with hosted Firecrawl
        the local resolver is not the one that will be used, and proxy fake-IP
        resolvers (198.18.0.0/15) would otherwise block ordinary public sites.
        """
        return validate_public_http_url(url, resolve_dns=self.config.resolve_fetch_dns())

    def _search_cache_key(self, request: SearchRequest) -> str:
        payload = {
            "q": request.query,
            "mode": request.mode,
            "depth": request.depth,
            "max": request.max_results,
            "inc": request.include_domains or [],
            "exc": request.exclude_domains or [],
            "vertical": request.vertical,
            "fp": self.config.enabled_provider_fingerprint(),
        }
        return self.cache.make_key("search", payload)

    def _fetch_cache_key(self, request: FetchRequest) -> str:
        payload = {
            "url": request.url,
            "focus": request.focus or "",
            "max_chars": request.max_chars,
            "fp": self.config.enabled_provider_fingerprint(),
        }
        return self.cache.make_key("fetch", payload)

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Primary search, then evidence-driven supplements.

        1. Phase 0: Grok (restricted to the requested sites when the query names
           them as places) runs alongside cheap site-native searches and, when a
           CJK query targets English sources, a keyword rewrite.
        2. Coverage: the evidence so far is checked against what the query asked
           for (site, every named source, comments, recency window, both sides
           of a comparison). Citation and domain counts still apply to queries
           without such requirements.
        3. Phase 1: supplements are spent only on unmet requirements: Tavily
           restricted to a missing site, a news search inside the window, a
           search for the missing side of a comparison, or the general Tavily
           supplement under the 0.2 rules.
        4. Threads on sites with a public API are read with their comments;
           comment relevance is ranked against what the user described.
        5. verify/deep: Firecrawl reads the best remaining pages.
        """
        started = time.perf_counter()
        warnings: list[str] = []
        errors: list[ErrorItem] = []
        stages: list[RouteStage] = []
        timed_out = False

        if not request.fresh:
            key = self._search_cache_key(request)
            cached = self.cache.get(key)
            if isinstance(cached, dict):
                resp = SearchResponse.model_validate(cached)
                resp.cache = CacheMeta(
                    hit=True,
                    age_seconds=self.cache.age_seconds(key) or 0.0,
                    ttl_seconds=self.config.cache.search_ttl_seconds,
                    namespace="search",
                )
                return resp

        intent = analyze(request.query, include_domains=request.include_domains)
        available = {
            "xai": self.config.xai.configured() and self.health_registry.is_available("xai"),
            "tavily": self.config.tavily.configured() and self.health_registry.is_available("tavily"),
            "anysearch": self.config.anysearch.configured() and self.health_registry.is_available("anysearch"),
            "firecrawl": self.config.firecrawl.configured() and self.health_registry.is_available("firecrawl"),
        }
        plan = build_execution_plan(request, self.config, provider_available=available)
        deadline = Deadline(plan.total_deadline_seconds)
        site_domains = intent.site_domains
        if site_domains and len(site_domains) <= 5:
            for stage in plan.stages:
                if stage.provider == "xai":
                    stage.include_domains = site_domains
                    stage.reason = "primary_site_restricted"
                    if (
                        intent.comments
                        and intent.topic
                        and self.native is not None
                        and all(req.site.reader and self.native.can_read(req.site.reader) for req in intent.sites)
                    ):
                        # The comment itself is found by reading the thread API;
                        # Grok only needs to locate the threads, which it does far
                        # faster than hunting for one comment (that hunt never
                        # finished within 90 s in the 0.2 runs).
                        names = " ".join(req.site.name for req in intent.sites)
                        stage.query = f"{names} threads about: {intent.topic}"
                        stage.reason = "primary_threads_only"

        group0 = [s for s in plan.stages if s.parallel_group == 0]
        group1 = [s for s in plan.stages if s.parallel_group == 1]
        provider_outputs: dict[str, ProviderSearchResult] = {}
        committed: set[tuple[str, str]] = set()
        rewrite: dict[str, str] = {}

        def commit(result: _StageResult) -> None:
            stages.append(result.stage)
            committed.add((result.stage.provider, result.stage.operation, result.stage.reason))
            if result.provider_result is not None:
                key = result.output_key or result.stage.provider
                provider_outputs[key] = result.provider_result
            if result.error is not None:
                errors.append(result.error)
            if result.warnings:
                warnings.extend(result.warnings)

        def mark_skipped_planned(plan_stages: list[ProviderStage], reason: str) -> None:
            # Idempotent: a stage is recorded at most once.
            for stage in plan_stages:
                key = (stage.provider, stage.operation, stage.reason)
                if key in committed:
                    continue
                stages.append(
                    RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="skipped",
                        reason=reason,
                    )
                )
                committed.add(key)

        def emit_deadline_error() -> None:
            nonlocal timed_out
            timed_out = True
            # Guarantee at most one structured deadline error per search.
            if any(e.code == "deadline_exceeded" for e in errors):
                return
            pub = to_public_error(DeadlineExceededError(), provider="allsearch")
            errors.append(ErrorItem(**pub.to_dict()))

        async def run_stage(stage: ProviderStage) -> _StageResult:
            op_started = time.perf_counter()
            query = stage.query or request.query
            include = stage.include_domains if stage.include_domains is not None else request.include_domains
            try:
                if stage.provider == "xai":
                    result = await self.xai.search(
                        query,
                        max_results=stage.max_results,
                        include_domains=include,
                        exclude_domains=request.exclude_domains,
                        deadline_seconds=deadline.remaining,
                    )
                elif stage.provider == "tavily":
                    result = await self.tavily.search(
                        query,
                        max_results=stage.max_results,
                        include_domains=include,
                        exclude_domains=request.exclude_domains,
                        search_depth=stage.search_depth,
                        topic=stage.topic,
                        time_range=stage.time_range,
                        deadline_seconds=deadline.remaining,
                    )
                elif stage.provider == "anysearch":
                    result = await self.anysearch.search(
                        query,
                        max_results=stage.max_results,
                        domain=stage.vertical,
                        deadline_seconds=deadline.remaining,
                    )
                else:
                    return _StageResult(
                        stage=RouteStage(
                            provider=stage.provider,
                            operation=stage.operation,
                            outcome="skipped",
                            reason="unknown_provider",
                        )
                    )
                selected_model = ""
                usage = None
                if stage.provider == "xai" and isinstance(result.tool_usage, dict):
                    selected_model = str(result.tool_usage.get("model") or "")
                    usage = result.tool_usage.get("usage") or None
                return _StageResult(
                    stage=RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="ok",
                        reason=stage.reason,
                        latency_ms=int((time.perf_counter() - op_started) * 1000),
                        detail=f"model={selected_model}" if selected_model else None,
                        usage=usage,
                    ),
                    provider_result=result,
                    warnings=list(result.raw_warnings),
                    output_key=stage.key,
                )
            except asyncio.CancelledError:
                # Deadline cancellation: never recorded as a provider failure.
                raise
            except TimeoutError:
                # Managed at the phase boundary; never treated as a provider error.
                raise
            except Exception as exc:
                pub = to_public_error(exc, provider=stage.provider)
                return _StageResult(
                    stage=RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="error",
                        reason=stage.reason,
                        latency_ms=int((time.perf_counter() - op_started) * 1000),
                        detail=pub.code,
                    ),
                    error=ErrorItem(**pub.to_dict()),
                )

        async def run_phase(plan_stages: list[ProviderStage], extra: list[Any] | None = None) -> None:
            """Run stages concurrently under the shared deadline.

            Completed results are committed in plan order; tasks cancelled by the
            deadline are marked skipped/deadline_exceeded. Never starts a phase
            whose budget is already exhausted. ``extra`` coroutines (site-native
            discovery) run in the same window and commit after the planned stages.
            """
            extra = extra or []
            if not plan_stages and not extra:
                return
            if deadline.expired:
                for coro in extra:
                    coro.close()
                emit_deadline_error()
                mark_skipped_planned(plan_stages, "deadline_exceeded")
                return
            tasks = [asyncio.ensure_future(run_stage(s)) for s in plan_stages]
            side = [asyncio.ensure_future(coro) for coro in extra]
            phase_timed_out = False
            try:
                async with deadline.timeout:
                    await asyncio.gather(*tasks, *side)
            except TimeoutError:
                phase_timed_out = True
                emit_deadline_error()
            collected = _collect_done(tasks)
            if phase_timed_out:
                # Commit completed and skipped stages in plan order, not completion
                # order, so partial timeout responses remain deterministic.
                for stage, result in zip(plan_stages, collected):
                    if result is None:
                        mark_skipped_planned([stage], "deadline_exceeded")
                    else:
                        commit(result)
            else:
                for result in collected:
                    if result is not None:
                        commit(result)
            for bundle in _collect_done(side):
                for result in bundle or []:
                    commit(result)

        # Phase 0: the primary (xAI/Grok), with site-native discovery and the
        # optional rewrite running beside it. With a realistic budget the
        # primary gets at most PRIMARY_SHARE of it, so one slow Grok call cannot
        # starve the supplements and thread reads that follow.
        discovery = (
            asyncio.ensure_future(self._site_discovery(intent, request, deadline, rewrite))
            if available.get("xai") or self.config.allow_degraded_search
            else None
        )
        cap = None
        if plan.total_deadline_seconds >= MIN_BUDGET_FOR_PRIMARY_CAP:
            cap = Deadline(min(deadline.remaining, plan.total_deadline_seconds * PRIMARY_SHARE))
        primary_capped = False
        if cap is None or deadline.expired:
            await run_phase(group0)
        elif group0:
            tasks = [asyncio.ensure_future(run_stage(s)) for s in group0]
            try:
                async with cap.timeout:
                    await asyncio.gather(*tasks)
            except TimeoutError:
                primary_capped = True
            for stage, result in zip(group0, _collect_done(tasks)):
                if result is not None:
                    commit(result)
                else:
                    stages.append(
                        RouteStage(provider=stage.provider, operation=stage.operation, outcome="skipped", reason="primary_time_cap")
                    )
                    committed.add((stage.provider, stage.operation, stage.reason))
            if primary_capped:
                errors.append(
                    ErrorItem(
                        provider="xai",
                        code="primary_timeout",
                        message=f"primary search stopped after {cap.total_seconds:.0f}s to leave time for supplements",
                        retryable=True,
                    )
                )
        if discovery is not None:
            try:
                async with deadline.timeout:
                    bundle = await discovery
            except TimeoutError:
                bundle = []
                emit_deadline_error()
            for result in bundle or []:
                commit(result)

        primary = provider_outputs.get("xai")
        if primary is not None:
            primary = _with_answer_links(primary)
            provider_outputs["xai"] = primary

        if timed_out:
            # Do not start the next phase; keep partial results only. Group 0 was
            # already marked by run_phase; mark any un-run group-1 stages here.
            mark_skipped_planned(group1, "deadline_exceeded")
            results, citations, evidence = self._merge_results(request, provider_outputs, None, intent)
            return self._finalize(
                request=request,
                plan=plan,
                provider_outputs=provider_outputs,
                sufficiency=None,
                stages=stages,
                warnings=warnings,
                errors=errors,
                degraded=False,
                started=started,
                results=results,
                citations=citations,
                evidence=evidence,
                pages_fetched=0,
                timed_out=True,
                intent=intent,
                threads=[],
            )

        if primary is None and not self.config.allow_degraded_search and not primary_capped:
            # Strict gate: fail before any group-1 stage runs. A primary that was
            # merely slow (capped) is not "unavailable": its supplements still run.
            mark_skipped_planned(group1, "primary_unavailable")
            return self._strict_primary_error(request, plan, stages, warnings, errors, started)

        sufficiency = evaluate_primary_sufficiency(
            primary,
            self.config,
            depth=request.depth,
            include_domains=request.include_domains or (site_domains or None),
            exclude_domains=request.exclude_domains,
            comparison=plan.signals.comparison,
            recency=plan.signals.recency,
            vertical_expected=bool(plan.signals.verticals),
        )
        pre_results, pre_citations, _ = self._merge_results(request, provider_outputs, sufficiency, intent)
        # Comments are met by reading threads, not by searching again, so they
        # do not count toward the decision to run search supplements.
        pre_coverage = evaluate_coverage(intent, pre_results, [c.url for c in pre_citations], [])
        pre_coverage = Coverage(
            status=(
                "not_applicable"
                if all(r.kind == "comments" for r in pre_coverage.requirements)
                else "sufficient"
                if all(r.satisfied for r in pre_coverage.requirements if r.kind != "comments")
                else "insufficient"
            ),
            requirements=[r for r in pre_coverage.requirements if r.kind != "comments"],
        )

        # Phase 1: supplements. Targeted stages replace the general Tavily
        # supplement when the coverage check names what is missing.
        tavily_stage = next((s for s in group1 if s.provider == "tavily"), None)
        targeted = self._targeted_stages(intent, request, pre_coverage, tavily_stage, rewrite) if tavily_stage else []
        # When the query states requirements, the coverage check replaces the
        # 0.2 citation/domain-count rule: three HN threads are enough for
        # "find the HN thread" even though they share one domain.
        primary_sufficient = (
            pre_coverage.status == "sufficient"
            if pre_coverage.status != "not_applicable"
            else sufficiency.sufficient
        )
        if tavily_stage is not None and intent.target_language == "en" and rewrite.get("topic_en"):
            tavily_stage.query = rewrite["topic_en"]
        need_tavily = tavily_required_after_primary(
            depth=request.depth,
            primary_ok=primary is not None,
            primary_sufficient=primary_sufficient,
            signals=plan.signals,
            tavily_planned=tavily_stage is not None,
        )
        runnable: list[ProviderStage] = []
        for stage in group1:
            if stage.provider == "tavily":
                if targeted:
                    continue
                if not need_tavily:
                    stages.append(
                        RouteStage(
                            provider="tavily",
                            operation="search",
                            outcome="skipped",
                            reason="primary_sufficient",
                        )
                    )
                    committed.add(("tavily", "search", "primary_sufficient"))
                    continue
            runnable.append(stage)
        runnable = targeted + runnable
        await run_phase(runnable)

        degraded = primary is None and not primary_capped
        results, citations, evidence = self._merge_results(request, provider_outputs, sufficiency, intent)

        # Forum threads: read through public APIs, comments ranked by relevance.
        threads: list[ThreadEvidence] = []
        if not timed_out and not deadline.expired:
            threads = await self._read_threads(intent, request, results, deadline, rewrite, stages, warnings)
            if deadline.expired and not threads:
                emit_deadline_error()

        # Optional Firecrawl after merge (final domain filter already applied above).
        pages_fetched = 0
        if not timed_out and plan.scrape_after_merge and not deadline.expired:
            read_urls = {canonicalize_url(t.url) for t in threads if t.status in {"read", "comments_read"}}
            candidates = [r for r in results if canonicalize_url(r.url) not in read_urls]
            urls = select_urls_for_scrape(candidates, budget=plan.scrape_budget, prefer_domains=site_domains or None)
            if urls:
                sem = asyncio.Semaphore(self.config.reliability.max_concurrency)

                async def scrape_one(url: str) -> _ScrapeResult:
                    # No shared side effects here: content/title are returned in
                    # the bundle so the caller attaches deterministically in URL
                    # order and completed scrapes survive a phase timeout.
                    async with sem:
                        try:
                            self._check_url(url)
                            fr = await self.firecrawl.scrape(
                                url, deadline_seconds=deadline.remaining
                            )
                            if fr.final_url:
                                self._check_url(fr.final_url)
                            raw = fr.content or ""
                            content = raw[:AUTO_SCRAPE_MAX_CHARS]
                            truncated = len(raw) > AUTO_SCRAPE_MAX_CHARS
                            issue = content_quality_issue(content)
                            if issue is not None:
                                return _ScrapeResult(
                                    url=url,
                                    stage=RouteStage(
                                        provider="firecrawl",
                                        operation="scrape",
                                        outcome="partial",
                                        reason="post_discovery",
                                        detail=issue,
                                    ),
                                    warnings=[f"auto_scrape_rejected:{issue}"],
                                )
                            return _ScrapeResult(
                                url=url,
                                stage=RouteStage(
                                    provider="firecrawl",
                                    operation="scrape",
                                    outcome="ok",
                                    reason="post_discovery",
                                    latency_ms=fr.latency_ms,
                                ),
                                content=content,
                                title=fr.title,
                                warnings=["auto_scrape_truncated"] if truncated else [],
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            pub = to_public_error(exc, provider="firecrawl")
                            return _ScrapeResult(
                                url=url,
                                stage=RouteStage(
                                    provider="firecrawl",
                                    operation="scrape",
                                    outcome="error",
                                    reason="post_discovery",
                                    detail=pub.code,
                                ),
                                error=ErrorItem(**pub.to_dict()),
                            )

                def commit_scrape(result: _ScrapeResult) -> None:
                    stages.append(result.stage)
                    if result.content is not None:
                        attach_fetched_content(
                            results, result.url, result.content, result.title
                        )
                        nonlocal pages_fetched
                        pages_fetched += 1
                    if result.error is not None:
                        errors.append(result.error)
                    if result.warnings:
                        warnings.extend(result.warnings)

                tasks = [asyncio.ensure_future(scrape_one(u)) for u in urls]
                try:
                    async with deadline.timeout:
                        await asyncio.gather(*tasks)
                except TimeoutError:
                    emit_deadline_error()
                # Commit completed scrapes in URL plan order; record unfinished
                # URLs as skipped/deadline_exceeded.
                for result in _collect_done(tasks):
                    if result is None:
                        stages.append(
                            RouteStage(
                                provider="firecrawl",
                                operation="scrape",
                                outcome="skipped",
                                reason="deadline_exceeded",
                            )
                        )
                    else:
                        commit_scrape(result)
            evidence.pages_fetched = pages_fetched

        return self._finalize(
            request=request,
            plan=plan,
            provider_outputs=provider_outputs,
            sufficiency=sufficiency,
            stages=stages,
            warnings=warnings,
            errors=errors,
            degraded=degraded,
            started=started,
            results=results,
            citations=citations,
            evidence=evidence,
            pages_fetched=pages_fetched,
            timed_out=timed_out,
            intent=intent,
            threads=threads,
        )

    # -- evidence-driven helpers ------------------------------------------------

    async def _site_discovery(
        self,
        intent: QueryIntent,
        request: SearchRequest,
        deadline: Deadline,
        rewrite: dict[str, str],
    ) -> list[_StageResult]:
        """Rewrite (when a CJK query targets English sources) and site-native searches."""
        out: list[_StageResult] = []
        wants_rewrite = (
            self.config.rewrite_enabled
            and self.config.xai.configured()
            and self.health_registry.is_available("xai")
            and (intent.needs_rewrite or (intent.comments and intent.language != "en"))
        )
        if wants_rewrite:
            op_started = time.perf_counter()
            try:
                fields, usage = await rewrite_query(
                    self.xai,
                    request.query,
                    model=self.config.rewrite_model or None,
                    deadline_seconds=min(20.0, deadline.remaining),
                )
                rewrite.update({k: v for k, v in fields.items() if v})
                out.append(
                    _StageResult(
                        stage=RouteStage(
                            provider="xai",
                            operation="rewrite",
                            outcome="ok",
                            reason="cross_language" if intent.needs_rewrite else "comment_terms",
                            latency_ms=int((time.perf_counter() - op_started) * 1000),
                            usage=usage or None,
                        )
                    )
                )
            except (asyncio.CancelledError, TimeoutError):
                raise
            except Exception as exc:
                pub = to_public_error(exc, provider="xai")
                out.append(
                    _StageResult(
                        stage=RouteStage(provider="xai", operation="rewrite", outcome="error", reason="cross_language", detail=pub.code),
                        warnings=["rewrite_failed_using_rule_keywords"],
                    )
                )
        if self.native is None or request.include_domains:
            return out

        async def one(req: Any) -> _StageResult | None:
            key = req.site.key
            if not self.native.can_search(key):
                return None
            lang = "zh" if req.domains == ("zh.wikipedia.org",) else ("en" if req.domains == ("en.wikipedia.org",) else intent.language)
            if req.site.language == "en" or (key == "wikipedia" and lang == "en"):
                query = rewrite.get("topic_en") or (intent.topic if intent.language == "en" else "")
            elif req.site.language == "zh" or lang == "zh":
                query = rewrite.get("topic_zh") or intent.topic
            else:
                query = intent.topic
            if not query:
                return None
            op_started = time.perf_counter()
            try:
                items = await self.native.search(key, query, limit=5, deadline=min(15.0, deadline.remaining), lang=lang)
            except (asyncio.CancelledError, TimeoutError):
                raise
            except Exception as exc:
                pub = to_public_error(exc, provider=key)
                return _StageResult(
                    stage=RouteStage(provider=key, operation="search", outcome="error", reason="site_native", detail=pub.code),
                    error=ErrorItem(**pub.to_dict()),
                )
            return _StageResult(
                stage=RouteStage(
                    provider=items[0].provider if items else key,
                    operation="search",
                    outcome="ok",
                    reason="site_native",
                    latency_ms=int((time.perf_counter() - op_started) * 1000),
                    detail=f"{len(items)} results",
                ),
                provider_result=ProviderSearchResult(provider=key, query=query, results=items,
                                                     citations=[{"title": i.title, "url": i.url} for i in items]),
                output_key=f"native:{key}",
            )

        found = await asyncio.gather(*(one(req) for req in intent.sites))
        out.extend(item for item in found if item is not None)
        return out

    def _targeted_stages(
        self,
        intent: QueryIntent,
        request: SearchRequest,
        coverage: Coverage,
        template: ProviderStage,
        rewrite: dict[str, str],
    ) -> list[ProviderStage]:
        """Tavily calls aimed at the requirements the evidence does not yet meet."""
        if request.depth == "fast" or coverage.status != "insufficient":
            return []
        out: list[ProviderStage] = []

        def topic_for(language: str) -> str:
            if language == "en":
                return rewrite.get("topic_en") or (intent.topic if intent.language == "en" else request.query)
            if language == "zh":
                return rewrite.get("topic_zh") or (intent.topic if intent.language == "zh" else request.query)
            return intent.topic or request.query

        missing_sites = {r.target for r in unmet(coverage, "site")}
        for req in intent.sites:
            if req.site.name not in missing_sites or len(out) >= 3:
                continue
            out.append(
                replace(
                    template,
                    reason=f"site:{req.site.key}",
                    query=topic_for(req.site.language),
                    include_domains=list(req.domains),
                    key=f"tavily:site:{req.site.key}",
                )
            )
        if unmet(coverage, "recency") and intent.recency_hours:
            hours = intent.recency_hours
            out.append(
                replace(
                    template,
                    reason="recency_window",
                    topic="news",
                    time_range="day" if hours <= 24 else "week" if hours <= 168 else "month",
                    key="tavily:recency",
                )
            )
        for req in unmet(coverage, "entity")[:2]:
            out.append(replace(template, reason="missing_entity", query=req.target, key=f"tavily:entity:{req.target}"))
        if unmet(coverage, "language") and intent.target_language and not out:
            out.append(
                replace(template, reason="target_language", query=topic_for(intent.target_language), key="tavily:language")
            )
        return out

    async def _read_threads(
        self,
        intent: QueryIntent,
        request: SearchRequest,
        results: list[ResultItem],
        deadline: Deadline,
        rewrite: dict[str, str],
        stages: list[RouteStage],
        warnings: list[str],
    ) -> list[ThreadEvidence]:
        """Read the most relevant threads on sites that have a public API.

        Comments are always read when the query asks for them. For a plain
        "find the thread" query on a readable site, the top thread is read too
        (the APIs are free and the title and body confirm the match).
        """
        if self.native is None:
            return []
        if intent.comments:
            budget = {"fast": 1, "balanced": 2}.get(request.depth, 3)
        elif intent.thread and any(r.site.reader for r in intent.sites):
            budget = {"fast": 0, "balanced": 1}.get(request.depth, 2)
        else:
            return []
        required = intent.sites
        candidates: list[str] = []
        unreadable: list[str] = []
        topic_terms = set(terms(rewrite.get("topic_en") or "") + terms(intent.topic))

        def relevance(item: ResultItem, index: int) -> tuple[float, float, int]:
            have = set(terms(f"{item.title} {item.snippet}"))
            overlap = len(topic_terms & have) / max(1, len(topic_terms))
            meta = item.metadata or {}
            size = meta.get("num_comments") or meta.get("replies") or 0
            prior = min(1.0, math.log1p(size) / 6) if isinstance(size, (int, float)) else 0.0
            return (round(overlap, 2), prior, -index)

        ranked = [item for _, item in sorted(((relevance(r, i), r) for i, r in enumerate(results)), key=lambda x: x[0], reverse=True)]
        for item in ranked:
            ref = thread_ref(item.url)
            if ref is None:
                site = site_for_url(item.url)
                if intent.comments and site is not None and site.is_content_url(item.url) and (
                    not required or any(r.site.key == site.key for r in required)
                ):
                    unreadable.append(item.url)
                continue
            if required and not any(r.site.reader == ref[0] or r.site.key == ref[0] for r in required):
                continue
            if item.url not in candidates:
                candidates.append(item.url)
        focus = rewrite.get("comment_en") or intent.comment_focus or rewrite.get("topic_en") or intent.topic
        if intent.comment_focus and rewrite.get("comment_en"):
            focus = f"{intent.comment_focus} {rewrite['comment_en']}"
        selected = candidates[:budget]
        threads: list[ThreadEvidence] = []
        if selected:
            op_started = time.perf_counter()
            tasks = [
                asyncio.ensure_future(
                    self.native.read_thread(url, focus=focus, deadline=min(15.0, deadline.remaining), keep=8)
                )
                for url in selected
            ]
            try:
                async with deadline.timeout:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                warnings.append("thread_read_deadline")
            for url, task in zip(selected, tasks):
                if task.done() and not task.cancelled() and task.exception() is None:
                    thread = task.result()
                else:
                    thread = ThreadEvidence(platform=(thread_ref(url) or ("unknown", ""))[0], url=url, status="failed", error="deadline_exceeded")
                threads.append(thread)
                stages.append(
                    RouteStage(
                        provider=thread.platform,
                        operation="read_thread",
                        outcome="ok" if thread.status in {"read", "comments_read"} else ("skipped" if thread.status == "found" else "error"),
                        reason="comments" if intent.comments else "thread",
                        latency_ms=int((time.perf_counter() - op_started) * 1000),
                        detail=thread.error or f"{len(thread.comments)} comments",
                    )
                )
        by_key = {dedupe_key(t.url): t for t in threads if t.status in {"read", "comments_read"}}
        for item in results:
            thread = by_key.get(dedupe_key(item.url))
            if thread is None:
                continue
            if thread.title and (not item.title or item.title.startswith("http")):
                item.title = thread.title
            if not item.snippet and thread.body:
                item.snippet = thread.body[:300]
            if thread.platform not in item.matched_providers:
                item.matched_providers = [*item.matched_providers, thread.platform]
        for url in unreadable[: max(0, 2 - len(threads))]:
            site = site_for_url(url)
            threads.append(
                ThreadEvidence(
                    platform=site.key if site else "unknown",
                    url=url,
                    status="found",
                    error=f"no_public_comment_api ({site.access if site else 'unknown'})",
                )
            )
        return threads

    def _merge_results(
        self,
        request: SearchRequest,
        provider_outputs: dict[str, ProviderSearchResult],
        sufficiency: Any,
        intent: QueryIntent | None = None,
    ) -> tuple[list[Any], list[Any], Any]:
        outputs = list(provider_outputs.values())
        if intent is not None and intent.sites:
            # Site-native results go right after the primary in the interleave.
            outputs.sort(key=lambda r: 0 if r.provider == "xai" else 1 if self.native and r.provider in {"hackernews", "v2ex", "stackexchange", "wikipedia", "reddit"} else 2)
        results, citations, evidence = merge_provider_results(
            outputs,
            max_results=request.max_results,
            primary_provider="xai",
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
        )
        identifiers = query_identifiers(request.query)
        if intent is not None and (intent.sites or identifiers):
            # Content pages on the requested sites first, then pages that carry
            # the exact identifiers the query names (RFC 791, PEP 703, 22.0.0,
            # PyObject_Malloc); provider order otherwise kept.
            def rank(item: ResultItem) -> tuple[int, int]:
                site_rank = 2
                host = hostname_of(item.url or "")
                for req in intent.sites:
                    if host and req.site.owns(host):
                        site_rank = 0 if req.site.is_content_url(item.url) else 1
                        break
                return (site_rank if intent.sites else 0, -identifier_hits(identifiers, f"{item.url} {item.title}"))

            results = sorted(results, key=rank)
            for index, item in enumerate(results, start=1):
                item.id = f"r{index}"
            new_ids = {canonicalize_url(item.url): item.id for item in results if item.url}
            for citation in citations:
                if citation.result_id is not None:
                    citation.result_id = new_ids.get(canonicalize_url(citation.url))
            order = {item.id: index for index, item in enumerate(results)}
            citations.sort(key=lambda c: order.get(c.result_id or "", len(order)))
            for index, citation in enumerate(citations, start=1):
                citation.id = f"c{index}"
        if sufficiency is not None:
            evidence.primary_citation_count = sufficiency.citation_count
        return results, citations, evidence

    def _finalize(
        self,
        *,
        request: SearchRequest,
        plan: Any,
        provider_outputs: dict[str, ProviderSearchResult],
        sufficiency: Any,
        stages: list[RouteStage],
        warnings: list[str],
        errors: list[ErrorItem],
        degraded: bool,
        started: float,
        results: list[Any],
        citations: list[Any],
        evidence: Any,
        pages_fetched: int,
        timed_out: bool,
        intent: QueryIntent | None = None,
        threads: list[ThreadEvidence] | None = None,
    ) -> SearchResponse:
        threads = threads or []
        primary = provider_outputs.get("xai")
        answer = ""
        if primary is not None:
            answer = primary.answer or ""
        if threads and intent is not None and intent.comments:
            quoted = extractive_answer(threads)
            if quoted:
                answer = f"{answer}\n\n{quoted}" if answer else quoted
                warnings.append("answer_includes_api_comments")
        elif degraded:
            tav = provider_outputs.get("tavily")
            if tav and tav.answer:
                answer = tav.answer
                warnings.append("degraded_answer_from_tavily")
            else:
                warnings.append("degraded_empty_answer")

        status = "ok"
        if errors and (results or answer):
            status = "partial"
        elif errors and not results and not answer:
            status = "error"
        if degraded:
            status = "partial" if (results or answer) else "error"

        governed = intent is not None and bool(intent.sites or intent.entities or intent.recency_hours or intent.target_language_explicit)
        if sufficiency is not None and not sufficiency.sufficient and primary is not None and not governed:
            # With explicit requirements the coverage block replaces this signal.
            warnings.append("primary_insufficient:" + ",".join(sufficiency.reasons[:4]))

        route = RouteInfo(
            primary="xai",
            depth=request.depth,
            mode=request.mode,
            verticals=plan.signals.verticals,
            stages=stages,
            degraded=degraded,
            reasons=plan.reasons + (list(sufficiency.reasons) if sufficiency is not None else []),
        )
        coverage = None
        next_steps: list[dict[str, Any]] = []
        if intent is not None:
            coverage = evaluate_coverage(intent, results, [c.url for c in citations], threads)
            if coverage.status == "insufficient":
                warnings.append(
                    "coverage_insufficient:" + ",".join(f"{r.kind}:{r.target}" for r in coverage.requirements if not r.satisfied)[:200]
                )
            next_steps = _next_steps(intent, coverage, results, threads)
            evidence.threads_read = sum(1 for t in threads if t.status in {"read", "comments_read"})
            evidence.comments_read = sum(len(t.comments) for t in threads if t.status == "comments_read")
        timing_ms = int((time.perf_counter() - started) * 1000)
        response = SearchResponse(
            status=status,
            query=request.query,
            answer=answer,
            results=results,
            citations=citations,
            route=route,
            evidence=evidence,
            cache=CacheMeta(
                hit=False,
                ttl_seconds=self.config.cache.search_ttl_seconds,
                namespace="search",
            ),
            warnings=warnings,
            errors=errors,
            timing_ms=timing_ms,
            intent=intent.to_dict() if intent is not None else {},
            coverage=coverage,
            threads=threads,
            next_steps=next_steps,
        )
        # Never cache timed-out responses.
        if not timed_out and status in {"ok", "partial"} and (results or answer) and not request.fresh:
            ttl = (
                self.config.cache.news_ttl_seconds
                if plan.signals.recency or request.mode == "news"
                else self.config.cache.search_ttl_seconds
            )
            self.cache.set(self._search_cache_key(request), response.model_dump(), ttl)
        return response

    def _strict_primary_error(
        self,
        request: SearchRequest,
        plan: Any,
        stages: list[RouteStage],
        warnings: list[str],
        errors: list[ErrorItem],
        started: float,
    ) -> SearchResponse:
        if not self.config.xai.configured():
            errors.append(
                ErrorItem(
                    provider="xai",
                    code="primary_unavailable",
                    message="xAI/Grok is not configured",
                    retryable=False,
                )
            )
        elif not any(e.provider == "xai" for e in errors):
            # Primary missing for another reason (e.g. circuit open) and no xAI
            # error recorded yet — emit a structured item so callers see why.
            circuit_open = False
            state = self.health_registry.get("xai")
            if state is not None and getattr(state, "state", "") == "open":
                circuit_open = True
            errors.append(
                ErrorItem(
                    provider="xai",
                    code="circuit_open" if circuit_open else "primary_unavailable",
                    message=(
                        "xAI/Grok circuit is open"
                        if circuit_open
                        else "xAI/Grok primary produced no result"
                    ),
                    retryable=circuit_open,
                )
            )
        timing_ms = int((time.perf_counter() - started) * 1000)
        return SearchResponse(
            status="error",
            query=request.query,
            answer="",
            results=[],
            citations=[],
            route=RouteInfo(
                primary="xai",
                depth=request.depth,
                mode=request.mode,
                verticals=plan.signals.verticals,
                stages=stages,
                degraded=False,
                reasons=plan.reasons + ["primary_unavailable"],
            ),
            cache=CacheMeta(hit=False, ttl_seconds=self.config.cache.search_ttl_seconds),
            warnings=warnings,
            errors=errors,
            timing_ms=timing_ms,
        )

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        started = time.perf_counter()
        stages: list[RouteStage] = []
        errors: list[ErrorItem] = []
        warnings: list[str] = []

        try:
            self._check_url(request.url)
        except UnsafeURLError as exc:
            return FetchResponse(
                status="error",
                url=request.url,
                route=RouteInfo(stages=[RouteStage(provider="security", operation="validate", outcome="error", reason="ssrf")]),
                errors=[ErrorItem(**to_public_error(exc, provider="security").to_dict())],
                timing_ms=int((time.perf_counter() - started) * 1000),
            )

        if not request.fresh:
            key = self._fetch_cache_key(request)
            cached = self.cache.get(key)
            if isinstance(cached, dict):
                resp = FetchResponse.model_validate(cached)
                resp.cache = CacheMeta(
                    hit=True,
                    age_seconds=self.cache.age_seconds(key) or 0.0,
                    ttl_seconds=self.config.cache.fetch_ttl_seconds,
                    namespace="fetch",
                )
                return resp

        ref = thread_ref(request.url)
        if ref is not None and self.native is not None and self.native.can_read(ref[0]):
            op_started = time.perf_counter()
            thread = await self.native.read_thread(
                request.url,
                focus=request.focus or "",
                deadline=self.config.reliability.timeout_seconds,
                keep=20,
            )
            stages.append(
                RouteStage(
                    provider=thread.platform,
                    operation="read_thread",
                    outcome="ok" if thread.status in {"read", "comments_read"} else "error",
                    reason="explicit_fetch",
                    latency_ms=int((time.perf_counter() - op_started) * 1000),
                    detail=thread.error or f"{len(thread.comments)} comments",
                )
            )
            if thread.status in {"read", "comments_read"}:
                content = render_thread(thread)
                truncated = len(content) > request.max_chars
                response = FetchResponse(
                    status="ok",
                    url=request.url,
                    final_url=thread.url,
                    title=thread.title,
                    content=content[: request.max_chars],
                    content_type="text/markdown",
                    provider=thread.platform,
                    truncated=truncated,
                    metadata={"source_api": thread.source_api, "comments_total": thread.comments_total},
                    route=RouteInfo(stages=stages),
                    cache=CacheMeta(hit=False, ttl_seconds=self.config.cache.fetch_ttl_seconds),
                    warnings=warnings,
                    errors=errors,
                    timing_ms=int((time.perf_counter() - started) * 1000),
                    thread=thread,
                )
                if not request.fresh:
                    self.cache.set(self._fetch_cache_key(request), response.model_dump(), self.config.cache.fetch_ttl_seconds)
                return response
            warnings.append(f"thread_api_{thread.status}:{thread.error}")

        if not self.config.firecrawl.configured():
            return FetchResponse(
                status="error",
                url=request.url,
                errors=[
                    ErrorItem(
                        provider="firecrawl",
                        code="fetch_unavailable",
                        message="Firecrawl is not configured",
                        retryable=False,
                    )
                ],
                route=RouteInfo(
                    stages=[
                        RouteStage(
                            provider="firecrawl",
                            operation="scrape",
                            outcome="skipped",
                            reason="not_configured",
                        )
                    ]
                ),
                timing_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            result = await self.firecrawl.scrape(
                request.url,
                deadline_seconds=self.config.reliability.timeout_seconds,
            )
            if result.final_url:
                # Redirect destination must also pass SSRF checks; short /
                # anti-bot content is still returned for explicit fetches.
                self._check_url(result.final_url)
            content = result.content or ""
            access = access_issue(content)
            if access is not None:
                # Report the wall instead of passing its text off as the page.
                errors.append(
                    ErrorItem(
                        provider="firecrawl",
                        code=access,
                        message=f"the page returned a {access.replace('_', ' ')} instead of its content",
                        retryable=False,
                    )
                )
            truncated = False
            if len(content) > request.max_chars:
                content = content[: request.max_chars]
                truncated = True
            stages.append(
                RouteStage(
                    provider="firecrawl",
                    operation="scrape",
                    outcome="ok",
                    reason="explicit_fetch",
                    latency_ms=result.latency_ms,
                )
            )
            response = FetchResponse(
                status="ok" if content and access is None else "partial",
                url=request.url,
                final_url=result.final_url or request.url,
                title=result.title,
                content=content,
                content_type=result.content_type,
                provider="firecrawl",
                truncated=truncated,
                metadata=result.metadata,
                route=RouteInfo(stages=stages),
                cache=CacheMeta(hit=False, ttl_seconds=self.config.cache.fetch_ttl_seconds),
                warnings=warnings,
                errors=errors,
                timing_ms=int((time.perf_counter() - started) * 1000),
            )
            if response.status in {"ok", "partial"} and content and not request.fresh:
                self.cache.set(
                    self._fetch_cache_key(request),
                    response.model_dump(),
                    self.config.cache.fetch_ttl_seconds,
                )
            return response
        except Exception as exc:
            pub = to_public_error(exc, provider="firecrawl")
            return FetchResponse(
                status="error",
                url=request.url,
                errors=[ErrorItem(**pub.to_dict())],
                route=RouteInfo(
                    stages=[
                        RouteStage(
                            provider="firecrawl",
                            operation="scrape",
                            outcome="error",
                            reason="explicit_fetch",
                            detail=pub.code,
                        )
                    ]
                ),
                timing_ms=int((time.perf_counter() - started) * 1000),
            )

    async def health(self, *, probe: bool = False) -> HealthResponse:
        started = time.perf_counter()
        warnings: list[str] = []
        self._register_health()

        if probe:
            async def probe_xai() -> dict[str, Any]:
                if not self.config.xai.configured():
                    return {"ok": False, "reason": "not_configured"}
                # Minimal dry check: configuration only for offline safety unless real call desired.
                # Active probe would spend credits; keep lightweight metadata probe.
                return {"ok": True, "reason": "configured"}

            for name, fn in (
                ("xai", probe_xai),
            ):
                try:
                    await self.health_registry.probe_once(name, fn)
                except Exception as exc:
                    warnings.append(f"probe_{name}_failed:{exc.__class__.__name__}")

        providers = [
            ProviderHealth(**state.to_dict())
            for state in self.health_registry.all_states()
        ]
        configured_primary = self.config.xai.configured()
        any_configured = any(
            [
                self.config.xai.configured(),
                self.config.tavily.configured(),
                self.config.anysearch.configured(),
                self.config.firecrawl.configured(),
            ]
        )
        if configured_primary:
            xai_state = self.health_registry.get("xai")
            state_name = xai_state.state if xai_state else "idle"
            if state_name in {"degraded", "half_open", "open"}:
                status = "degraded"
                if state_name == "open":
                    warnings.append("primary_circuit_open")
            else:
                status = "ok"
        elif any_configured and self.config.allow_degraded_search:
            status = "degraded"
            warnings.append("primary_missing_degraded_allowed")
        elif any_configured:
            status = "degraded"
            warnings.append("primary_missing")
        else:
            status = "unavailable"

        return HealthResponse(
            status=status,
            version=__import__("allsearch").__version__,
            transport=self.config.transport,
            model=self.config.xai.model,
            allow_degraded_search=self.config.allow_degraded_search,
            providers=providers,
            cache=self.cache.snapshot(),
            warnings=warnings,
            timing_ms=int((time.perf_counter() - started) * 1000),
        )
