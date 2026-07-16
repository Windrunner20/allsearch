"""xAI Grok Responses API web_search primary adapter."""

from __future__ import annotations

import time
from typing import Any

from allsearch.config import XAIConfig
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

        models = [self.config.model, *self.config.fallback_models]
        # Stable de-duplication in case the operator repeats the primary.
        models = list(dict.fromkeys(model for model in models if model))
        url = f"{self.config.base_url.rstrip('/')}{self.config.responses_path}"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
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
                if self.health:
                    self.health.record_failure(self.name, "auth_error", retryable=False)
                raise
            except Exception as exc:
                last_exc = exc
                fallbackable = isinstance(exc, ProviderContractError) or (
                    isinstance(exc, TransportError) and exc.retryable
                ) or bool(getattr(exc, "retryable", False))
                if index + 1 < len(models) and fallbackable:
                    continue
                break

        assert last_exc is not None
        if self.health:
            code = getattr(last_exc, "code", "error")
            retryable = bool(getattr(last_exc, "retryable", True))
            self.health.record_failure(self.name, str(code), retryable=retryable)
        raise last_exc
