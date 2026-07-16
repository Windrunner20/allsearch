"""Provider protocols and shared helpers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from allsearch.models import ProviderFetchResult, ProviderSearchResult


@runtime_checkable
class DiscoveryProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        **kwargs: Any,
    ) -> ProviderSearchResult: ...


@runtime_checkable
class FetchProvider(Protocol):
    name: str

    async def scrape(self, url: str, **kwargs: Any) -> ProviderFetchResult: ...


def citation_from_item(item: dict[str, Any]) -> dict[str, str] | None:
    url = str(item.get("url") or item.get("link") or "").strip()
    if not url:
        return None
    title = str(item.get("title") or item.get("name") or url)
    return {"title": title, "url": url}
