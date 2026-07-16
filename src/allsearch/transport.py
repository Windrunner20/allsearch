"""Shared bounded async HTTP client with retries and redaction."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from allsearch.errors import AuthError, ProviderContractError, TimeoutError_, TransportError, redact_text


class HttpTransport:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 33_554_432,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self._client = client
        self._owned_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                headers={"User-Agent": "AllSearch/0.1"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        deadline_seconds: float | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        remaining = deadline_seconds if deadline_seconds is not None else self.timeout_seconds
        attempt = 0
        last_exc: BaseException | None = None

        while attempt <= self.max_retries:
            attempt += 1
            if remaining <= 0:
                raise TimeoutError_("request deadline exceeded")
            timeout = min(self.timeout_seconds, remaining)
            started = asyncio.get_event_loop().time()
            try:
                client = await self._get_client()
                response = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=json,
                    timeout=timeout,
                )
                content = response.content
                if len(content) > self.max_response_bytes:
                    raise TransportError(
                        f"response exceeds max_response_bytes ({self.max_response_bytes})",
                        retryable=False,
                        status_code=response.status_code,
                    )

                if response.status_code in {401, 403}:
                    raise AuthError(
                        f"authentication failed ({response.status_code})",
                        provider=provider,
                    )

                if response.status_code == 400:
                    raise ProviderContractError(
                        f"bad request from provider: {redact_text(response.text[:300])}",
                        provider=provider,
                    )

                if response.status_code in {408, 429} or response.status_code >= 500:
                    retryable = True
                    retry_after = response.headers.get("Retry-After")
                    delay = 0.2 * attempt + random.uniform(0, 0.2)
                    if retry_after:
                        try:
                            delay = min(5.0, float(retry_after))
                        except ValueError:
                            pass
                    last_exc = TransportError(
                        f"provider HTTP {response.status_code}",
                        retryable=retryable,
                        status_code=response.status_code,
                    )
                    if attempt <= self.max_retries and remaining - (asyncio.get_event_loop().time() - started) > delay:
                        await asyncio.sleep(delay)
                        remaining -= asyncio.get_event_loop().time() - started
                        continue
                    raise last_exc

                if response.status_code >= 400:
                    raise TransportError(
                        f"provider HTTP {response.status_code}: {redact_text(response.text[:300])}",
                        retryable=False,
                        status_code=response.status_code,
                    )

                try:
                    data = response.json()
                except ValueError as exc:
                    raise ProviderContractError(
                        "provider returned non-JSON response",
                        provider=provider,
                    ) from exc
                if not isinstance(data, dict):
                    raise ProviderContractError(
                        "provider JSON root must be an object",
                        provider=provider,
                    )
                return data
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_exc = TimeoutError_(str(exc) or "http timeout")
            except (httpx.TransportError, httpx.NetworkError) as exc:
                last_exc = TransportError(redact_text(str(exc)), retryable=True)
            except (AuthError, ProviderContractError, TransportError, TimeoutError_):
                raise
            except Exception as exc:  # pragma: no cover
                last_exc = TransportError(redact_text(str(exc)), retryable=False)

            elapsed = asyncio.get_event_loop().time() - started
            remaining -= elapsed
            if attempt <= self.max_retries and remaining > 0.05:
                await asyncio.sleep(0.15 * attempt + random.uniform(0, 0.1))
                continue
            break

        assert last_exc is not None
        raise last_exc
