"""Validated environment configuration and redaction."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import load_dotenv

from allsearch.errors import ConfigError, redact_text

TransportName = Literal["stdio", "sse", "streamable-http"]

DEFAULT_VERTICALS = (
    "general,resource,social_media,finance,academic,legal,health,business,"
    "security,ip,code,energy,environment,agriculture,travel,film,gaming"
)


def _env_str(*names: str, default: str = "") -> str:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name} must be <= {max_value}")
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        value = default
    else:
        try:
            value = float(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}")
    return value


def _normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ConfigError("base URL must not be empty")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"invalid base URL: {url}")
    if parsed.username or parsed.password:
        raise ConfigError("base URL must not contain credentials")
    return url


def _normalize_path(path: str) -> str:
    path = path.strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _csv_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _safe_repr_secret(value: str | None) -> str:
    if not value:
        return ""
    return "***"


@dataclass(slots=True, frozen=True)
class XAIFallbackEndpoint:
    """A secondary, independently-credentialed xAI-compatible endpoint used when the
    primary Responses endpoint fails. Typically an OpenAI-compatible chat gateway."""

    api_key: str
    base_url: str
    chat_path: str
    model: str
    # Whether the fallback gateway speaks OpenAI chat completions. Only
    # ``openai`` is supported; the ``responses`` protocol is rejected at load.
    protocol: Literal["openai"] = "openai"
    # Optional per-endpoint reasoning effort. Defaults to None because many
    # OpenAI-compatible gateways mis-handle reasoning_effort and return empty content.
    reasoning_effort: Literal["low", "medium", "high"] | None = None

    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"XAIFallbackEndpoint(api_key={_safe_repr_secret(self.api_key)!r}, "
            f"base_url={self.base_url!r}, chat_path={self.chat_path!r}, model={self.model!r}, "
            f"protocol={self.protocol!r}, reasoning_effort={self.reasoning_effort!r})"
        )


@dataclass(slots=True, frozen=True)
class XAIConfig:
    api_key: str | None
    base_url: str
    responses_path: str
    model: str
    fallback_models: tuple[str, ...]
    reasoning_effort: Literal["low", "medium", "high"] | None
    allowed_models: tuple[str, ...]
    max_tool_calls: int
    # Endpoint-level fallback (independent base_url + api_key + model).
    fallback_endpoint: XAIFallbackEndpoint | None = None

    def configured(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        fb = "yes" if self.fallback_endpoint and self.fallback_endpoint.configured() else "no"
        return (
            f"XAIConfig(api_key={_safe_repr_secret(self.api_key)!r}, base_url={self.base_url!r}, "
            f"responses_path={self.responses_path!r}, model={self.model!r}, fallback_endpoint={fb})"
        )


@dataclass(slots=True, frozen=True)
class TavilyConfig:
    api_key: str | None
    base_url: str
    search_path: str
    default_depth: Literal["basic", "advanced"]
    max_results: int
    # Additional keys forming a rotation pool (excludes api_key, which is always index 0).
    extra_api_keys: tuple[str, ...] = ()

    def configured(self) -> bool:
        return bool(self.all_keys())

    def all_keys(self) -> tuple[str, ...]:
        """Return the full deduped key pool, primary key first."""
        keys: list[str] = []
        if self.api_key:
            keys.append(self.api_key)
        for k in self.extra_api_keys:
            if k and k not in keys:
                keys.append(k)
        return tuple(keys)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"TavilyConfig(api_key={_safe_repr_secret(self.api_key)!r}, "
            f"extra_keys={len(self.extra_api_keys)}, base_url={self.base_url!r}, "
            f"search_path={self.search_path!r})"
        )


@dataclass(slots=True, frozen=True)
class AnySearchConfig:
    api_key: str | None
    endpoint: str
    enabled: bool
    verticals: tuple[str, ...]
    directory_ttl_seconds: int
    max_results: int

    def configured(self) -> bool:
        return self.enabled

    def authenticated(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AnySearchConfig(api_key={_safe_repr_secret(self.api_key)!r}, endpoint={self.endpoint!r}, "
            f"enabled={self.enabled})"
        )


@dataclass(slots=True, frozen=True)
class FirecrawlConfig:
    api_key: str | None
    base_url: str
    scrape_path: str
    only_main_content: bool
    max_pages: int

    def configured(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FirecrawlConfig(api_key={_safe_repr_secret(self.api_key)!r}, base_url={self.base_url!r}, "
            f"scrape_path={self.scrape_path!r})"
        )


@dataclass(slots=True, frozen=True)
class CacheConfig:
    search_ttl_seconds: int
    news_ttl_seconds: int
    fetch_ttl_seconds: int
    max_entries: int
    backend: str = "memory"


@dataclass(slots=True, frozen=True)
class ReliabilityConfig:
    timeout_seconds: float
    total_budget_seconds: float
    max_concurrency: int
    max_response_bytes: int
    health_probe_ttl_seconds: int
    circuit_failure_threshold: int
    circuit_open_seconds: int
    min_primary_citations: int
    min_unique_domains: int
    auto_scrape_top_n: int


@dataclass(slots=True, frozen=True)
class AllSearchConfig:
    server_name: str
    log_level: str
    transport: TransportName
    mcp_host: str
    mcp_port: int
    mcp_path: str
    mcp_stateless_http: bool
    allow_degraded_search: bool
    reliability: ReliabilityConfig
    cache: CacheConfig
    xai: XAIConfig
    tavily: TavilyConfig
    anysearch: AnySearchConfig
    firecrawl: FirecrawlConfig
    route_policy_version: str = "1"

    def enabled_provider_fingerprint(self) -> str:
        flags = [
            f"xai={int(self.xai.configured())}",
            f"tavily={int(self.tavily.configured())}",
            f"anysearch={int(self.anysearch.configured())}",
            f"firecrawl={int(self.firecrawl.configured())}",
            f"model={self.xai.model}",
            f"policy={self.route_policy_version}",
        ]
        return "|".join(flags)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["xai"]["api_key"] = bool(self.xai.api_key)
        fallback = self.xai.fallback_endpoint
        if fallback is not None and isinstance(data["xai"].get("fallback_endpoint"), dict):
            data["xai"]["fallback_endpoint"]["api_key"] = bool(fallback.api_key)
        data["tavily"]["api_key"] = bool(self.tavily.api_key)
        data["tavily"]["extra_api_keys"] = len(self.tavily.extra_api_keys)
        data["anysearch"]["api_key"] = bool(self.anysearch.api_key)
        data["firecrawl"]["api_key"] = bool(self.firecrawl.api_key)
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AllSearchConfig(server_name={self.server_name!r}, transport={self.transport!r}, "
            f"xai={self.xai!r}, tavily={self.tavily!r}, anysearch={self.anysearch!r}, "
            f"firecrawl={self.firecrawl!r})"
        )


def load_dotenv_if_present(path: str | Path | None = None) -> None:
    """Load .env without overriding process/OpenClaw-injected environment."""
    if path is not None:
        load_dotenv(path, override=False)
        return
    # Prefer CWD, then package-adjacent parent
    load_dotenv(override=False)


def load_config(*, env_file: str | Path | None = None) -> AllSearchConfig:
    load_dotenv_if_present(env_file)

    transport_raw = _env_str("ALLSEARCH_TRANSPORT", default="stdio").lower()
    if transport_raw not in {"stdio", "sse", "streamable-http"}:
        raise ConfigError(f"ALLSEARCH_TRANSPORT invalid: {transport_raw}")

    tavily_depth = _env_str("ALLSEARCH_TAVILY_DEFAULT_DEPTH", default="basic").lower()
    if tavily_depth not in {"basic", "advanced"}:
        raise ConfigError("ALLSEARCH_TAVILY_DEFAULT_DEPTH must be basic|advanced")

    model = _env_str("ALLSEARCH_XAI_MODEL", default="grok-4.5")
    tavily_primary_key = _env_str("ALLSEARCH_TAVILY_API_KEY") or None
    # Pool keys: tokens separated by commas or newlines; whitespace/quotes trimmed.
    # Supports ALLSEARCH_TAVILY_API_KEYS (preferred, plural) and legacy ALLSEARCH_TAVILY_EXTRA_API_KEYS.
    tavily_pool_raw = _env_str(
        "ALLSEARCH_TAVILY_API_KEYS", "ALLSEARCH_TAVILY_EXTRA_API_KEYS", default=""
    )
    tavily_extra_keys: tuple[str, ...] = ()
    if tavily_pool_raw:
        tokens = [t.strip().strip("'\"") for t in tavily_pool_raw.replace("\n", ",").split(",")]
        seen: set[str] = set()
        for tok in tokens:
            if tok and tok not in seen and tok != tavily_primary_key:
                seen.add(tok)
                tavily_extra_keys += (tok,)
    fallback_models_raw = _env_str("ALLSEARCH_XAI_FALLBACK_MODELS", default="grok-4.3")
    if fallback_models_raw.strip().lower() in {"none", "off"}:
        fallback_models: tuple[str, ...] = ()
    else:
        fallback_models = tuple(
            candidate
            for candidate in _csv_list(fallback_models_raw)
            if candidate != model
        )
    reasoning_effort_raw = _env_str("ALLSEARCH_XAI_REASONING_EFFORT", default="low").lower()
    if reasoning_effort_raw in {"", "none", "off"}:
        reasoning_effort = None
    elif reasoning_effort_raw in {"low", "medium", "high"}:
        reasoning_effort = reasoning_effort_raw
    else:
        raise ConfigError("ALLSEARCH_XAI_REASONING_EFFORT must be low|medium|high|none")
    allowed_models = tuple(
        _csv_list(
            _env_str(
                "ALLSEARCH_XAI_ALLOWED_MODELS",
                default="grok-4.3,grok-3-mini-fast,grok-4.5,grok-composer-2.5-fast,grok-4.20-fast,grok-4.20-0309-non-reasoning,grok-4.20-0309-reasoning",
            )
        )
    )
    # Do not hard-fail on unknown model IDs; account aliases vary. Warn via allowed list only if set.
    if allowed_models and model not in allowed_models:
        # keep configurable; still accept operator choice
        pass

    verticals = tuple(
        _csv_list(_env_str("ALLSEARCH_ANYSEARCH_VERTICALS", default=DEFAULT_VERTICALS))
    )

    # Optional endpoint-level fallback: a second xAI-compatible gateway used when the
    # primary Responses endpoint fails. OpenAI-compatible chat gateways use /chat/completions.
    xai_fb_endpoint = _env_str("ALLSEARCH_XAI_FALLBACK_BASE_URL", default="")
    xai_fb_key = _env_str("ALLSEARCH_XAI_FALLBACK_API_KEY", default="")
    xai_fallback_endpoint: XAIFallbackEndpoint | None = None
    if xai_fb_endpoint and xai_fb_key:
        xai_fb_protocol_raw = _env_str(
            "ALLSEARCH_XAI_FALLBACK_PROTOCOL", default="openai"
        ).lower()
        if xai_fb_protocol_raw != "openai":
            raise ConfigError(
                "ALLSEARCH_XAI_FALLBACK_PROTOCOL must be openai; "
                "the responses protocol is not supported"
            )
        xai_fb_reasoning_raw = _env_str(
            "ALLSEARCH_XAI_FALLBACK_REASONING_EFFORT", default=""
        ).lower()
        if xai_fb_reasoning_raw in {"", "none", "off"}:
            xai_fb_reasoning: Literal["low", "medium", "high"] | None = None
        elif xai_fb_reasoning_raw in {"low", "medium", "high"}:
            xai_fb_reasoning = xai_fb_reasoning_raw  # type: ignore[assignment]
        else:
            raise ConfigError(
                "ALLSEARCH_XAI_FALLBACK_REASONING_EFFORT must be low|medium|high|none"
            )
        xai_fallback_endpoint = XAIFallbackEndpoint(
            api_key=xai_fb_key,
            base_url=_normalize_base_url(xai_fb_endpoint),
            chat_path=_normalize_path(
                _env_str("ALLSEARCH_XAI_FALLBACK_CHAT_PATH", default="/chat/completions")
            ),
            model=_env_str("ALLSEARCH_XAI_FALLBACK_MODEL", default=model),
            protocol=xai_fb_protocol_raw,  # type: ignore[arg-type]
            reasoning_effort=xai_fb_reasoning,  # type: ignore[arg-type]
        )

    return AllSearchConfig(
        server_name=_env_str("ALLSEARCH_SERVER_NAME", default="AllSearch"),
        log_level=_env_str("ALLSEARCH_LOG_LEVEL", default="INFO").upper(),
        transport=transport_raw,  # type: ignore[arg-type]
        mcp_host=_env_str("ALLSEARCH_MCP_HOST", default="127.0.0.1"),
        mcp_port=_env_int("ALLSEARCH_MCP_PORT", 8000, min_value=1, max_value=65535),
        mcp_path=_normalize_path(_env_str("ALLSEARCH_MCP_PATH", default="/mcp")),
        mcp_stateless_http=_env_bool("ALLSEARCH_MCP_STATELESS_HTTP", False),
        allow_degraded_search=_env_bool("ALLSEARCH_ALLOW_DEGRADED_SEARCH", False),
        reliability=ReliabilityConfig(
            timeout_seconds=_env_float("ALLSEARCH_TIMEOUT_SECONDS", 30.0, min_value=1.0),
            total_budget_seconds=_env_float("ALLSEARCH_TOTAL_BUDGET_SECONDS", 45.0, min_value=1.0),
            max_concurrency=_env_int("ALLSEARCH_MAX_CONCURRENCY", 4, min_value=1, max_value=32),
            max_response_bytes=_env_int(
                "ALLSEARCH_MAX_RESPONSE_BYTES", 33_554_432, min_value=1024
            ),
            health_probe_ttl_seconds=_env_int(
                "ALLSEARCH_HEALTH_PROBE_TTL_SECONDS", 300, min_value=0
            ),
            circuit_failure_threshold=_env_int(
                "ALLSEARCH_CIRCUIT_FAILURE_THRESHOLD", 3, min_value=1
            ),
            circuit_open_seconds=_env_int("ALLSEARCH_CIRCUIT_OPEN_SECONDS", 60, min_value=1),
            min_primary_citations=_env_int("ALLSEARCH_MIN_PRIMARY_CITATIONS", 3, min_value=0),
            min_unique_domains=_env_int("ALLSEARCH_MIN_UNIQUE_DOMAINS", 2, min_value=0),
            auto_scrape_top_n=_env_int("ALLSEARCH_AUTO_SCRAPE_TOP_N", 0, min_value=0, max_value=10),
        ),
        cache=CacheConfig(
            search_ttl_seconds=_env_int("ALLSEARCH_SEARCH_CACHE_TTL_SECONDS", 120, min_value=0),
            news_ttl_seconds=_env_int("ALLSEARCH_NEWS_CACHE_TTL_SECONDS", 30, min_value=0),
            fetch_ttl_seconds=_env_int("ALLSEARCH_FETCH_CACHE_TTL_SECONDS", 900, min_value=0),
            max_entries=_env_int("ALLSEARCH_CACHE_MAX_ENTRIES", 512, min_value=1),
            backend=_env_str("ALLSEARCH_CACHE_BACKEND", default="memory"),
        ),
        xai=XAIConfig(
            api_key=_env_str("ALLSEARCH_XAI_API_KEY") or None,
            base_url=_normalize_base_url(
                _env_str("ALLSEARCH_XAI_BASE_URL", default="https://api.x.ai/v1")
            ),
            responses_path=_normalize_path(
                _env_str("ALLSEARCH_XAI_RESPONSES_PATH", default="/responses")
            ),
            model=model,
            fallback_models=fallback_models,
            reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
            allowed_models=allowed_models,
            max_tool_calls=_env_int("ALLSEARCH_XAI_MAX_TOOL_CALLS", 4, min_value=1, max_value=20),
            fallback_endpoint=xai_fallback_endpoint,
        ),
        tavily=TavilyConfig(
            api_key=tavily_primary_key,
            extra_api_keys=tavily_extra_keys,
            base_url=_normalize_base_url(
                _env_str("ALLSEARCH_TAVILY_BASE_URL", default="https://api.tavily.com")
            ),
            search_path=_normalize_path(
                _env_str("ALLSEARCH_TAVILY_SEARCH_PATH", default="/search")
            ),
            default_depth=tavily_depth,  # type: ignore[arg-type]
            max_results=_env_int("ALLSEARCH_TAVILY_MAX_RESULTS", 8, min_value=1, max_value=20),
        ),
        anysearch=AnySearchConfig(
            api_key=_env_str("ALLSEARCH_ANYSEARCH_API_KEY") or None,
            endpoint=_normalize_base_url(
                _env_str("ALLSEARCH_ANYSEARCH_ENDPOINT", default="https://api.anysearch.com/mcp")
            ),
            enabled=_env_bool("ALLSEARCH_ANYSEARCH_ENABLED", True),
            verticals=verticals,
            directory_ttl_seconds=_env_int(
                "ALLSEARCH_ANYSEARCH_DIRECTORY_TTL_SECONDS", 86_400, min_value=0
            ),
            max_results=_env_int("ALLSEARCH_ANYSEARCH_MAX_RESULTS", 8, min_value=1, max_value=10),
        ),
        firecrawl=FirecrawlConfig(
            api_key=_env_str("ALLSEARCH_FIRECRAWL_API_KEY") or None,
            base_url=_normalize_base_url(
                _env_str("ALLSEARCH_FIRECRAWL_BASE_URL", default="https://api.firecrawl.dev")
            ),
            scrape_path=_normalize_path(
                _env_str("ALLSEARCH_FIRECRAWL_SCRAPE_PATH", default="/v2/scrape")
            ),
            only_main_content=_env_bool("ALLSEARCH_FIRECRAWL_ONLY_MAIN_CONTENT", True),
            max_pages=_env_int("ALLSEARCH_FIRECRAWL_MAX_PAGES", 3, min_value=0, max_value=10),
        ),
    )


def redact_for_log(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(s in key for s in ("key", "token", "secret", "password", "authorization")):
                out[k] = "***" if v else v
            else:
                out[k] = redact_for_log(v)
        return out
    if isinstance(value, list):
        return [redact_for_log(v) for v in value]
    return value
