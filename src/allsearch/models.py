"""Versioned public and normalized internal schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# 1.1 is additive over 1.0: coverage, threads, next_steps, intent, fetch.thread.
SCHEMA_VERSION = "1.1"

SearchMode = Literal["auto", "web", "news", "docs", "research", "vertical"]
SearchDepth = Literal["fast", "balanced", "verify", "deep"]
ResponseStatus = Literal["ok", "partial", "error"]
HealthStatus = Literal["ok", "degraded", "unavailable"]
SourceType = Literal["web", "news", "document", "social", "vertical"]
ProviderName = Literal["xai", "tavily", "anysearch", "firecrawl"]
StageOutcome = Literal["ok", "partial", "error", "skipped"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: SearchMode = "auto"
    depth: SearchDepth = "balanced"
    max_results: int = Field(default=8, ge=1, le=20)
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    vertical: str | None = None
    fresh: bool = False

    @field_validator("include_domains", "exclude_domains", mode="before")
    @classmethod
    def _coerce_domains(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = [value]
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        return cleaned or None

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        q = value.strip()
        if not q:
            raise ValueError("query must not be empty")
        return q


class FetchRequest(BaseModel):
    url: str = Field(min_length=1)
    focus: str | None = None
    max_chars: int = Field(default=30_000, ge=100, le=200_000)
    fresh: bool = False


class ResultItem(BaseModel):
    id: str
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str | None = None
    published_at: str | None = None
    source_type: SourceType = "web"
    provider: str = ""
    matched_providers: list[str] = Field(default_factory=list)
    score: float | None = None
    vertical: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CitationItem(BaseModel):
    id: str
    title: str = ""
    url: str = ""
    result_id: str | None = None
    providers: list[str] = Field(default_factory=list)


class RouteStage(BaseModel):
    provider: str
    operation: str
    outcome: StageOutcome
    reason: str = ""
    latency_ms: int | None = None
    detail: str | None = None
    # Provider-reported usage (tokens, cost ticks) when the response carries it.
    usage: dict[str, Any] | None = None


class RouteInfo(BaseModel):
    primary: str = "xai"
    depth: SearchDepth = "balanced"
    mode: SearchMode = "auto"
    verticals: list[str] = Field(default_factory=list)
    stages: list[RouteStage] = Field(default_factory=list)
    degraded: bool = False
    reasons: list[str] = Field(default_factory=list)


class EvidenceMetrics(BaseModel):
    unique_urls: int = 0
    unique_domains: int = 0
    cross_provider_matches: int = 0
    pages_fetched: int = 0
    primary_citation_count: int = 0
    threads_read: int = 0
    comments_read: int = 0


class CacheMeta(BaseModel):
    hit: bool = False
    age_seconds: float = 0.0
    ttl_seconds: int = 0
    namespace: str | None = None


class ErrorItem(BaseModel):
    provider: str | None = None
    code: str
    message: str
    retryable: bool = False


class Comment(BaseModel):
    """One comment or reply read from a platform API, never from a search snippet."""

    id: str
    author: str = ""
    created_at: str | None = None
    text: str = ""
    url: str = ""
    parent_id: str | None = None
    depth: int = 0
    points: int | None = None
    relevance: float | None = None


ThreadStatus = Literal["found", "read", "comments_read", "failed", "blocked"]


class ThreadEvidence(BaseModel):
    """A forum thread and what was actually read from it.

    ``found``: only the URL is known. ``read``: the opening post was read.
    ``comments_read``: comments were read through the platform API.
    ``failed`` / ``blocked``: the read was attempted and did not succeed.
    """

    platform: str
    url: str
    thread_id: str = ""
    title: str = ""
    author: str = ""
    created_at: str | None = None
    body: str = ""
    status: ThreadStatus = "found"
    comments_total: int | None = None
    comments: list[Comment] = Field(default_factory=list)
    source_api: str = ""
    error: str | None = None


class Requirement(BaseModel):
    kind: str
    target: str = ""
    satisfied: bool = False
    detail: str = ""


class Coverage(BaseModel):
    """Whether the evidence meets what the query asked for, requirement by requirement."""

    status: Literal["sufficient", "insufficient", "not_applicable"] = "not_applicable"
    requirements: list[Requirement] = Field(default_factory=list)


class SearchResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    status: ResponseStatus
    query: str
    answer: str = ""
    results: list[ResultItem] = Field(default_factory=list)
    citations: list[CitationItem] = Field(default_factory=list)
    route: RouteInfo = Field(default_factory=RouteInfo)
    evidence: EvidenceMetrics = Field(default_factory=EvidenceMetrics)
    cache: CacheMeta = Field(default_factory=CacheMeta)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorItem] = Field(default_factory=list)
    timing_ms: int = 0
    intent: dict[str, Any] = Field(default_factory=dict)
    coverage: Coverage | None = None
    threads: list[ThreadEvidence] = Field(default_factory=list)
    next_steps: list[dict[str, Any]] = Field(default_factory=list)


class FetchResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    status: ResponseStatus
    url: str
    final_url: str = ""
    title: str = ""
    content: str = ""
    content_type: str = "text/markdown"
    provider: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    route: RouteInfo = Field(default_factory=RouteInfo)
    cache: CacheMeta = Field(default_factory=CacheMeta)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorItem] = Field(default_factory=list)
    timing_ms: int = 0
    thread: ThreadEvidence | None = None


class ProviderHealth(BaseModel):
    name: str
    configured: bool
    state: str = "unknown"
    last_success_at: float | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    latency_ms_ewma: float | None = None
    probe_at: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    status: HealthStatus
    version: str
    transport: str = "stdio"
    model: str | None = None
    allow_degraded_search: bool = False
    providers: list[ProviderHealth] = Field(default_factory=list)
    cache: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    timing_ms: int = 0


# Internal normalized provider outputs


class ProviderResultItem(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str | None = None
    published_at: str | None = None
    source_type: SourceType = "web"
    provider: str
    score: float | None = None
    vertical: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderSearchResult(BaseModel):
    provider: str
    query: str
    answer: str = ""
    results: list[ProviderResultItem] = Field(default_factory=list)
    citations: list[dict[str, str]] = Field(default_factory=list)
    tool_usage: dict[str, Any] = Field(default_factory=dict)
    raw_warnings: list[str] = Field(default_factory=list)
    latency_ms: int = 0


class ProviderFetchResult(BaseModel):
    provider: str
    url: str
    final_url: str = ""
    title: str = ""
    content: str = ""
    content_type: str = "text/markdown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
