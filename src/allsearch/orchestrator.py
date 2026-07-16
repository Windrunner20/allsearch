"""Full search/fetch/health composition pipeline."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from allsearch.cache import MemoryCache
from allsearch.config import AllSearchConfig
from allsearch.errors import (
    AllSearchError,
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
from allsearch.quality import evaluate_primary_sufficiency, select_urls_for_scrape
from allsearch.routing import build_execution_plan, tavily_required_after_primary
from allsearch.security import validate_public_http_url
from allsearch.transport import HttpTransport


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
        deadline = plan.total_deadline_seconds
        budget_start = time.perf_counter()

        def remaining() -> float:
            return max(0.0, deadline - (time.perf_counter() - budget_start))

        # Split stages by parallel group
        group0 = [s for s in plan.stages if s.parallel_group == 0]
        group1 = [s for s in plan.stages if s.parallel_group == 1]

        provider_outputs: dict[str, ProviderSearchResult] = {}

        async def run_stage(stage) -> None:
            op_started = time.perf_counter()
            try:
                if stage.provider == "xai":
                    result = await self.xai.search(
                        request.query,
                        max_results=stage.max_results,
                        include_domains=request.include_domains,
                        exclude_domains=request.exclude_domains,
                        deadline_seconds=remaining(),
                    )
                elif stage.provider == "tavily":
                    result = await self.tavily.search(
                        request.query,
                        max_results=stage.max_results,
                        include_domains=request.include_domains,
                        exclude_domains=request.exclude_domains,
                        search_depth=stage.search_depth,
                        topic=stage.topic,
                        deadline_seconds=remaining(),
                    )
                elif stage.provider == "anysearch":
                    result = await self.anysearch.search(
                        request.query,
                        max_results=stage.max_results,
                        domain=stage.vertical,
                        deadline_seconds=remaining(),
                    )
                else:
                    stages.append(
                        RouteStage(
                            provider=stage.provider,
                            operation=stage.operation,
                            outcome="skipped",
                            reason="unknown_provider",
                        )
                    )
                    return
                provider_outputs[stage.provider] = result
                warnings.extend(result.raw_warnings)
                selected_model = ""
                if stage.provider == "xai" and isinstance(result.tool_usage, dict):
                    selected_model = str(result.tool_usage.get("model") or "")
                stages.append(
                    RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="ok",
                        reason=stage.reason,
                        latency_ms=int((time.perf_counter() - op_started) * 1000),
                        detail=f"model={selected_model}" if selected_model else None,
                    )
                )
            except Exception as exc:
                pub = to_public_error(exc, provider=stage.provider)
                errors.append(ErrorItem(**pub.to_dict()))
                stages.append(
                    RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="error",
                        reason=stage.reason,
                        latency_ms=int((time.perf_counter() - op_started) * 1000),
                        detail=pub.code,
                    )
                )

        # Execute group 0 concurrently
        if group0 and remaining() > 0:
            await asyncio.gather(*(run_stage(s) for s in group0))

        primary = provider_outputs.get("xai")
        sufficiency = evaluate_primary_sufficiency(
            primary,
            self.config,
            depth=request.depth,
            include_domains=request.include_domains,
            comparison=plan.signals.comparison,
            recency=plan.signals.recency,
            vertical_expected=bool(plan.signals.verticals),
        )

        # Conditional group 1 (typically Tavily)
        tavily_stage = next((s for s in group1 if s.provider == "tavily"), None)
        need_tavily = tavily_required_after_primary(
            depth=request.depth,
            primary_ok=primary is not None,
            primary_sufficient=sufficiency.sufficient,
            signals=plan.signals,
            tavily_planned=tavily_stage is not None,
        )
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
                continue
            if remaining() <= 0:
                stages.append(
                    RouteStage(
                        provider=stage.provider,
                        operation=stage.operation,
                        outcome="skipped",
                        reason="deadline_exhausted",
                    )
                )
                continue
            await run_stage(stage)

        # Primary unavailable handling
        degraded = False
        answer = ""
        if primary is not None:
            answer = primary.answer or ""
        elif not self.config.allow_degraded_search:
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
                # Primary missing for another reason (e.g. circuit open) and no
                # xAI error recorded yet — emit a structured item so callers see
                # why the response is empty.
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
        else:
            degraded = True
            # Prefer Tavily answer
            tav = provider_outputs.get("tavily")
            if tav and tav.answer:
                answer = tav.answer
                warnings.append("degraded_answer_from_tavily")
            else:
                warnings.append("degraded_empty_answer")

        merged_providers = list(provider_outputs.values())
        results, citations, evidence = merge_provider_results(
            merged_providers,
            max_results=request.max_results,
            primary_provider="xai",
        )
        evidence.primary_citation_count = sufficiency.citation_count

        # Optional Firecrawl after merge
        pages_fetched = 0
        if plan.scrape_after_merge and available.get("firecrawl") and remaining() > 0:
            urls = select_urls_for_scrape(results, budget=plan.scrape_budget)
            sem = asyncio.Semaphore(self.config.reliability.max_concurrency)

            async def scrape_one(u: str) -> None:
                nonlocal pages_fetched
                async with sem:
                    try:
                        validate_public_http_url(u, resolve_dns=False)
                        fr = await self.firecrawl.scrape(u, deadline_seconds=remaining())
                        attach_fetched_content(results, u, fr.content, fr.title)
                        pages_fetched += 1
                        stages.append(
                            RouteStage(
                                provider="firecrawl",
                                operation="scrape",
                                outcome="ok",
                                reason="post_discovery",
                                latency_ms=fr.latency_ms,
                            )
                        )
                    except Exception as exc:
                        pub = to_public_error(exc, provider="firecrawl")
                        errors.append(ErrorItem(**pub.to_dict()))
                        stages.append(
                            RouteStage(
                                provider="firecrawl",
                                operation="scrape",
                                outcome="error",
                                reason="post_discovery",
                                detail=pub.code,
                            )
                        )

            if urls:
                await asyncio.gather(*(scrape_one(u) for u in urls))
            evidence.pages_fetched = pages_fetched

        status = "ok"
        if errors and (results or answer):
            status = "partial"
        elif errors and not results and not answer:
            status = "error"
        if degraded:
            status = "partial" if (results or answer) else "error"

        if not sufficiency.sufficient and primary is not None:
            warnings.append("primary_insufficient:" + ",".join(sufficiency.reasons[:4]))

        route = RouteInfo(
            primary="xai",
            depth=request.depth,
            mode=request.mode,
            verticals=plan.signals.verticals,
            stages=stages,
            degraded=degraded,
            reasons=plan.reasons + sufficiency.reasons,
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
            cache=CacheMeta(hit=False, ttl_seconds=self.config.cache.search_ttl_seconds, namespace="search"),
            warnings=warnings,
            errors=errors,
            timing_ms=timing_ms,
        )

        if status in {"ok", "partial"} and (results or answer) and not request.fresh:
            ttl = (
                self.config.cache.news_ttl_seconds
                if plan.signals.recency or request.mode == "news"
                else self.config.cache.search_ttl_seconds
            )
            self.cache.set(self._search_cache_key(request), response.model_dump(), ttl)

        return response

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
