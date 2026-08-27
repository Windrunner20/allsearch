"""Full search/fetch/health composition pipeline."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
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
from allsearch.health import HealthRegistry
from allsearch.merge import attach_fetched_content, merge_provider_results
from allsearch.models import (
    CacheMeta,
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
from allsearch.providers.xai import XAIProvider
from allsearch.quality import (
    content_quality_issue,
    evaluate_primary_sufficiency,
    select_urls_for_scrape,
)
from allsearch.routing import (
    ProviderStage,
    build_execution_plan,
    tavily_required_after_primary,
)
from allsearch.security import validate_public_http_url
from allsearch.transport import HttpTransport

# Internal per-page cap for automatic post-merge scraping (not configurable).
AUTO_SCRAPE_MAX_CHARS = 30_000


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
        self._register_health()

    def _register_health(self) -> None:
        self.health_registry.register("xai", self.config.xai.configured())
        self.health_registry.register("tavily", self.config.tavily.configured())
        self.health_registry.register("anysearch", self.config.anysearch.configured())
        self.health_registry.register("firecrawl", self.config.firecrawl.configured())

    async def aclose(self) -> None:
        await self.transport.aclose()

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

        available = {
            "xai": self.config.xai.configured() and self.health_registry.is_available("xai"),
            "tavily": self.config.tavily.configured() and self.health_registry.is_available("tavily"),
            "anysearch": self.config.anysearch.configured() and self.health_registry.is_available("anysearch"),
            "firecrawl": self.config.firecrawl.configured() and self.health_registry.is_available("firecrawl"),
        }
        plan = build_execution_plan(request, self.config, provider_available=available)
        deadline = Deadline(plan.total_deadline_seconds)

        # Split stages by parallel group.
        group0 = [s for s in plan.stages if s.parallel_group == 0]
        group1 = [s for s in plan.stages if s.parallel_group == 1]
        provider_outputs: dict[str, ProviderSearchResult] = {}
        committed: set[tuple[str, str]] = set()

        def commit(result: _StageResult) -> None:
            stages.append(result.stage)
            committed.add((result.stage.provider, result.stage.operation))
            if result.provider_result is not None:
                provider_outputs[result.stage.provider] = result.provider_result
            if result.error is not None:
                errors.append(result.error)
            if result.warnings:
                warnings.extend(result.warnings)

        def mark_skipped_planned(plan_stages: list[ProviderStage], reason: str) -> None:
            # Idempotent: a stage is recorded at most once.
            for stage in plan_stages:
                key = (stage.provider, stage.operation)
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
            try:
                if stage.provider == "xai":
                    result = await self.xai.search(
                        request.query,
                        max_results=stage.max_results,
                        include_domains=request.include_domains,
                        exclude_domains=request.exclude_domains,
                        deadline_seconds=deadline.remaining,
                    )
                elif stage.provider == "tavily":
                    result = await self.tavily.search(
                        request.query,
                        max_results=stage.max_results,
                        include_domains=request.include_domains,
                        exclude_domains=request.exclude_domains,
                        search_depth=stage.search_depth,
                        topic=stage.topic,
                        deadline_seconds=deadline.remaining,
                    )
                elif stage.provider == "anysearch":
                    result = await self.anysearch.search(
                        request.query,
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
                if stage.provider == "xai" and isinstance(result.tool_usage, dict):
                    selected_model = str(result.tool_usage.get("model") or "")
                return _StageResult(
                    stage=RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="ok",
                        reason=stage.reason,
                        latency_ms=int((time.perf_counter() - op_started) * 1000),
                        detail=f"model={selected_model}" if selected_model else None,
                    ),
                    provider_result=result,
                    warnings=list(result.raw_warnings),
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

        async def run_phase(plan_stages: list[ProviderStage]) -> None:
            """Run stages concurrently under the shared deadline.

            Completed results are committed in plan order; tasks cancelled by the
            deadline are marked skipped/deadline_exceeded. Never starts a phase
            whose budget is already exhausted.
            """
            if not plan_stages:
                return
            if deadline.expired:
                emit_deadline_error()
                mark_skipped_planned(plan_stages, "deadline_exceeded")
                return
            tasks = [asyncio.ensure_future(run_stage(s)) for s in plan_stages]
            phase_timed_out = False
            try:
                async with deadline.timeout:
                    await asyncio.gather(*tasks)
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

        # Phase 0: the primary (xAI/Grok) runs alone first under the shared deadline.
        await run_phase(group0)

        primary = provider_outputs.get("xai")

        if timed_out:
            # Do not start the next phase; keep partial results only. Group 0 was
            # already marked by run_phase; mark any un-run group-1 stages here.
            mark_skipped_planned(group1, "deadline_exceeded")
            results, citations, evidence = self._merge_results(request, provider_outputs, None)
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
            )

        if primary is None and not self.config.allow_degraded_search:
            # Strict gate: fail before any group-1 stage runs.
            mark_skipped_planned(group1, "primary_unavailable")
            return self._strict_primary_error(request, plan, stages, warnings, errors, started)

        sufficiency = evaluate_primary_sufficiency(
            primary,
            self.config,
            depth=request.depth,
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
            comparison=plan.signals.comparison,
            recency=plan.signals.recency,
            vertical_expected=bool(plan.signals.verticals),
        )

        # Phase 1: group-1 supplements run concurrently; records commit in plan order.
        tavily_stage = next((s for s in group1 if s.provider == "tavily"), None)
        need_tavily = tavily_required_after_primary(
            depth=request.depth,
            primary_ok=primary is not None,
            primary_sufficient=sufficiency.sufficient,
            signals=plan.signals,
            tavily_planned=tavily_stage is not None,
        )
        runnable: list[ProviderStage] = []
        for stage in group1:
            if stage.provider == "tavily" and not need_tavily:
                stages.append(
                    RouteStage(
                        provider="tavily",
                        operation="search",
                        outcome="skipped",
                        reason="primary_sufficient",
                    )
                )
                committed.add(("tavily", "search"))
                continue
            runnable.append(stage)
        await run_phase(runnable)

        degraded = primary is None
        results, citations, evidence = self._merge_results(request, provider_outputs, sufficiency)

        # Optional Firecrawl after merge (final domain filter already applied above).
        pages_fetched = 0
        if not timed_out and plan.scrape_after_merge and not deadline.expired:
            urls = select_urls_for_scrape(results, budget=plan.scrape_budget)
            if urls:
                sem = asyncio.Semaphore(self.config.reliability.max_concurrency)

                async def scrape_one(url: str) -> _ScrapeResult:
                    # No shared side effects here: content/title are returned in
                    # the bundle so the caller attaches deterministically in URL
                    # order and completed scrapes survive a phase timeout.
                    async with sem:
                        try:
                            validate_public_http_url(url, resolve_dns=True)
                            fr = await self.firecrawl.scrape(
                                url, deadline_seconds=deadline.remaining
                            )
                            if fr.final_url:
                                validate_public_http_url(fr.final_url, resolve_dns=True)
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
        )

    def _merge_results(
        self,
        request: SearchRequest,
        provider_outputs: dict[str, ProviderSearchResult],
        sufficiency: Any,
    ) -> tuple[list[Any], list[Any], Any]:
        results, citations, evidence = merge_provider_results(
            list(provider_outputs.values()),
            max_results=request.max_results,
            primary_provider="xai",
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
        )
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
    ) -> SearchResponse:
        primary = provider_outputs.get("xai")
        answer = ""
        if primary is not None:
            answer = primary.answer or ""
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

        if sufficiency is not None and not sufficiency.sufficient and primary is not None:
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
            validate_public_http_url(request.url, resolve_dns=True)
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
                validate_public_http_url(result.final_url, resolve_dns=True)
            content = result.content or ""
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
                status="ok" if content else "partial",
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
