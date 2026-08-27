"""xAI Grok Responses API web_search primary adapter.

Supports two transport shapes:
  * Primary: xAI Responses API (``/responses``) with the ``web_search`` tool.
  * Fallback: an optional OpenAI-compatible chat-completions gateway that also
    supports the ``web_search`` tool, used when the primary endpoint fails.
"""

from __future__ import annotations

import time
from typing import Any

from allsearch.config import XAIConfig, XAIFallbackEndpoint
from allsearch.errors import (
    AuthError,
    ProviderContractError,
    ProviderUnavailableError,
    TransportError,
)
from allsearch.health import HealthRegistry
from allsearch.models import ProviderResultItem, ProviderSearchResult
from allsearch.providers.base import citation_from_item
from allsearch.transport import HttpTransport


def _is_fallbackable(exc: BaseException) -> bool:
    """Can a secondary endpoint/model realistically resolve this failure?"""
    if isinstance(exc, AuthError):
        # Different endpoint => different credential; worth retrying there.
        return True
    if isinstance(exc, ProviderContractError):
        return True
    if isinstance(exc, TransportError):
        if exc.retryable:
            return True
        status = getattr(exc, "status_code", None)
        # 402 Payment Required (balance exhausted) and other 4xx quota/billing
        # signals should also try the fallback endpoint, whose billing is separate.
        if status in {402, 429}:
            return True
        return False
    return bool(getattr(exc, "retryable", False))


class XAIProvider:
    name = "xai"

    def __init__(
        self,
        config: XAIConfig,
        transport: HttpTransport,
        health: HealthRegistry | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.health = health

    def configured(self) -> bool:
        return self.config.configured()

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------
    def build_payload(
        self,
        query: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": "web_search"}
        filters: dict[str, Any] = {}
        if include_domains:
            filters["allowed_domains"] = include_domains
        if exclude_domains:
            filters["excluded_domains"] = exclude_domains
        if filters:
            tool["filters"] = filters

        augmented = f"{query}\n\nReturn up to {max_results} relevant results with concise sourcing."
        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "input": [{"role": "user", "content": augmented}],
            "tools": [tool],
            "store": False,
            "max_tool_calls": self.config.max_tool_calls,
        }
        if reasoning_effort:
            # Responses APIs use the nested reasoning object. Some compatibility
            # gateways accept a top-level reasoning_effort field but ignore it.
            payload["reasoning"] = {"effort": reasoning_effort}
        return payload

    @staticmethod
    def build_chat_payload(
        query: str,
        *,
        model: str,
        reasoning_effort: str | None = None,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build an OpenAI chat-completions payload with the web_search tool."""
        tool: dict[str, Any] = {"type": "web_search"}
        filters: dict[str, Any] = {}
        if include_domains:
            filters["allowed_domains"] = include_domains
        if exclude_domains:
            filters["excluded_domains"] = exclude_domains
        if filters:
            tool["filters"] = filters

        augmented = f"{query}\n\nReturn up to {max_results} relevant results with concise sourcing."
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": augmented}],
            "tools": [tool],
            "stream": False,
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        return payload

    # ------------------------------------------------------------------
    # Response parsers
    # ------------------------------------------------------------------
    @staticmethod
    def extract_output_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"]
        parts: list[str] = []
        for item in payload.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, dict) and isinstance(text.get("value"), str):
                    parts.append(text["value"])
        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def extract_chat_text(payload: dict[str, Any]) -> str:
        """Extract assistant text from an OpenAI chat-completions response."""
        choices = payload.get("choices") or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content:
                    return content
        return ""

    @staticmethod
    def extract_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()

        def _add(item: Any) -> None:
            if not isinstance(item, dict):
                return
            cit = citation_from_item(item)
            if cit is None:
                # xAI Responses citations may use several field aliases for URL/title.
                url = str(
                    item.get("url")
                    or item.get("href")
                    or item.get("target_url")
                    or item.get("source_url")
                    or ""
                ).strip()
                title = str(
                    item.get("title")
                    or item.get("text")
                    or item.get("source_title")
                    or item.get("display_text")
                    or url
                )
                if not url:
                    return
                cit = {"title": title, "url": url}
            if cit["url"] in seen:
                return
            seen.add(cit["url"])
            normalized.append(cit)

        raw = payload.get("citations") or []
        if isinstance(raw, list):
            for item in raw:
                _add(item)
        if normalized:
            return normalized

        for output_item in payload.get("output", []) or []:
            if not isinstance(output_item, dict):
                continue
            for content_item in output_item.get("content") or []:
                if not isinstance(content_item, dict):
                    continue
                for annotation in content_item.get("annotations") or []:
                    _add(annotation)
        return normalized

    @staticmethod
    def extract_chat_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
        """Extract citations from an OpenAI chat-completions web_search response.

        Sources, in priority order:
          1. Top-level ``search_sources`` array (gateway-provided web results).
          2. ``message.annotations[].url_citation`` (OpenAI-style annotations).
          3. Markdown links / bare URLs in the message content (fallback parser).
        """
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()

        def _add(url: Any, title: Any) -> None:
            url = str(url or "").strip()
            if not url or url in seen:
                return
            seen.add(url)
            normalized.append({"title": str(title or url), "url": url})

        # 1. Top-level search_sources
        sources = payload.get("search_sources")
        if isinstance(sources, list):
            for item in sources:
                if isinstance(item, dict):
                    _add(item.get("url"), item.get("title"))
                elif isinstance(item, str):
                    _add(item, item)

        # 2. Message annotations (OpenAI url_citation)
        if not normalized:
            for choice in payload.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                for ann in message.get("annotations") or []:
                    if not isinstance(ann, dict):
                        continue
                    uc = ann.get("url_citation")
                    if isinstance(uc, dict):
                        _add(uc.get("url"), uc.get("title"))

        # 3. Fallback: parse markdown links and bare URLs out of the content text.
        # Some OpenAI-compatible gateways omit structured citation fields and only
        # embed sources inline in the answer text.
        if not normalized:
            import re

            for choice in payload.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                # Markdown links: [title](url)
                for m in re.finditer(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", content):
                    _add(m.group(2), m.group(1))
                # Bare http(s) URLs not already captured by markdown form above.
                url_chars = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
                for m in re.finditer(rf"(?<!\()(https?://[{url_chars}]+)", content):
                    url = m.group(1)
                    # Strip trailing punctuation that is commonly appended in prose.
                    url = url.rstrip(".,;:!?)]}")
                    _add(url, url)
        return normalized

    # ------------------------------------------------------------------
    # Search orchestration
    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        deadline_seconds: float | None = None,
        **_: Any,
    ) -> ProviderSearchResult:
        if not self.configured():
            raise ProviderUnavailableError("xAI API key not configured", provider=self.name, code="primary_unavailable")
        if self.health:
            self.health.ensure_closed_or_raise(self.name)

        fb = self.config.fallback_endpoint
        fb_available = fb is not None and fb.configured()

        started = time.perf_counter()
        # Try the primary Responses endpoint first (with its own model fallbacks).
        last_exc: BaseException | None = None
        try:
            return await self._search_responses(
                query,
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                deadline_seconds=deadline_seconds,
                started=started,
            )
        except AuthError as exc:
            last_exc = exc
            if not (fb_available and _is_fallbackable(exc)):
                if self.health:
                    self.health.record_failure(self.name, "auth_error", retryable=False)
                raise
        except Exception as exc:
            last_exc = exc
            if not (fb_available and _is_fallbackable(exc)):
                if self.health:
                    code = getattr(exc, "code", "error")
                    retryable = bool(getattr(exc, "retryable", True))
                    self.health.record_failure(self.name, str(code), retryable=retryable)
                raise

        # Endpoint-level fallback via the OpenAI-compatible gateway.
        assert fb is not None  # for type checkers; guaranteed by fb_available
        if fb.protocol != "openai":
            # Defensive: only OpenAI chat-completions fallback is supported.
            from allsearch.errors import ConfigError

            raise ConfigError(
                "xAI fallback only supports protocol=openai, got "
                f"{fb.protocol!r}"
            )
        remaining = None
        if deadline_seconds is not None:
            remaining = max(0.0, deadline_seconds - (time.perf_counter() - started))
        try:
            result = await self._search_chat_fallback(
                query,
                fb,
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                deadline_seconds=remaining,
                started=started,
            )
            return result
        except AuthError:
            if self.health:
                self.health.record_failure(self.name, "auth_error", retryable=False)
            raise
        except Exception as exc:
            if self.health:
                code = getattr(exc, "code", "error")
                retryable = bool(getattr(exc, "retryable", True))
                self.health.record_failure(self.name, str(code), retryable=retryable)
            # Surface the original primary failure if both endpoints failed, since
            # the primary error is usually more diagnostic for callers.
            raise last_exc if last_exc is not None else exc

    async def _search_responses(
        self,
        query: str,
        *,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        deadline_seconds: float | None = None,
        started: float,
    ) -> ProviderSearchResult:
        models = [self.config.model, *self.config.fallback_models]
        # Stable de-duplication in case the operator repeats the primary.
        models = list(dict.fromkeys(model for model in models if model))
        url = f"{self.config.base_url.rstrip('/')}{self.config.responses_path}"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        attempted_models: list[str] = []
        last_exc: BaseException | None = None

        for index, model in enumerate(models):
            attempted_models.append(model)
            payload = self.build_payload(
                query,
                model=model,
                # The configured reasoning profile belongs to the primary model.
                # Fallback models use their native default for compatibility.
                reasoning_effort=self.config.reasoning_effort if index == 0 else None,
                max_results=max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
            remaining = None
            if deadline_seconds is not None:
                remaining = max(0.0, deadline_seconds - (time.perf_counter() - started))
            try:
                response = await self.transport.request_json(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    deadline_seconds=remaining,
                    provider=self.name,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                text = self.extract_output_text(response)
                citations = self.extract_citations(response)
                results = [
                    ProviderResultItem(
                        title=c.get("title", ""),
                        url=c.get("url", ""),
                        snippet="",
                        provider=self.name,
                        source_type="web",
                    )
                    for c in citations
                    if c.get("url")
                ]
                if self.health:
                    self.health.record_success(self.name, latency_ms)
                native_tool_usage = (
                    response.get("server_side_tool_usage")
                    or response.get("tool_usage")
                    or {}
                )
                return ProviderSearchResult(
                    provider=self.name,
                    query=query,
                    answer=text,
                    results=results,
                    citations=citations,
                    tool_usage={
                        "model": model,
                        "primary_model": self.config.model,
                        "fallback_used": index > 0,
                        "endpoint": "primary",
                        "reasoning_effort": self.config.reasoning_effort if index == 0 else None,
                        "max_tool_calls": self.config.max_tool_calls,
                        "attempted_models": attempted_models,
                        "native": native_tool_usage,
                        "usage": response.get("usage") or {},
                    },
                    raw_warnings=(
                        [f"xai_model_fallback:{self.config.model}->{model}"]
                        if index > 0
                        else []
                    ),
                    latency_ms=latency_ms,
                )
            except AuthError:
                # One credential is shared by all configured models; fallback cannot help.
                raise
            except Exception as exc:
                last_exc = exc
                fallbackable = isinstance(exc, ProviderContractError) or (
                    isinstance(exc, TransportError) and exc.retryable
                ) or bool(getattr(exc, "retryable", False))
                if index + 1 < len(models) and fallbackable:
                    continue
                raise

        assert last_exc is not None
        raise last_exc

    async def _search_chat_fallback(
        self,
        query: str,
        fb: XAIFallbackEndpoint,
        *,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        deadline_seconds: float | None = None,
        started: float,
    ) -> ProviderSearchResult:
        url = f"{fb.base_url.rstrip('/')}{fb.chat_path}"
        headers = {
            "Authorization": f"Bearer {fb.api_key}",
            "Content-Type": "application/json",
        }
        payload = self.build_chat_payload(
            query,
            model=fb.model,
            reasoning_effort=fb.reasoning_effort,
            max_results=max_results,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        response = await self.transport.request_json(
            "POST",
            url,
            headers=headers,
            json=payload,
            deadline_seconds=deadline_seconds,
            provider=self.name,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = self.extract_chat_text(response)
        citations = self.extract_chat_citations(response)
        results = [
            ProviderResultItem(
                title=c.get("title", ""),
                url=c.get("url", ""),
                snippet="",
                provider=self.name,
                source_type="web",
            )
            for c in citations
            if c.get("url")
        ]
        if self.health:
            self.health.record_success(self.name, latency_ms)
        return ProviderSearchResult(
            provider=self.name,
            query=query,
            answer=text,
            results=results,
            citations=citations,
            tool_usage={
                "model": fb.model,
                "primary_model": self.config.model,
                "fallback_used": True,
                "endpoint": "fallback",
                "reasoning_effort": fb.reasoning_effort,
                "max_tool_calls": self.config.max_tool_calls,
                "attempted_models": [fb.model],
                "usage": response.get("usage") or {},
            },
            raw_warnings=["xai_endpoint_fallback_used"],
            latency_ms=latency_ms,
        )
