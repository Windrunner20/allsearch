"""Typed internal errors and redacted public conversion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AllSearchError(Exception):
    """Base error for AllSearch."""

    def __init__(self, message: str, *, code: str = "error", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


class ConfigError(AllSearchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="config_error", retryable=False)


class AuthError(AllSearchError):
    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message, code="auth_error", retryable=False)
        self.provider = provider


class TransportError(AllSearchError):
    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None) -> None:
        super().__init__(message, code="transport_error", retryable=retryable)
        self.status_code = status_code


class ProviderContractError(AllSearchError):
    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message, code="provider_contract_error", retryable=False)
        self.provider = provider


class TimeoutError_(AllSearchError):
    def __init__(self, message: str = "request timed out") -> None:
        super().__init__(message, code="timeout", retryable=True)


class CircuitOpenError(AllSearchError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, code="circuit_open", retryable=True)
        self.provider = provider


class UnsafeURLError(AllSearchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="unsafe_url", retryable=False)


class QualityError(AllSearchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="quality_error", retryable=False)


class ProviderUnavailableError(AllSearchError):
    def __init__(self, message: str, *, provider: str, code: str = "provider_unavailable") -> None:
        super().__init__(message, code=code, retryable=False)
        self.provider = provider


@dataclass(slots=True)
class PublicError:
    provider: str | None
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def redact_text(value: str) -> str:
    """Redact common secret-looking substrings for public errors/logs."""
    if not value:
        return value
    # Bearer tokens / long keys
    import re

    out = value
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***", out)
    out = re.sub(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]{8,}", r"\1***", out)
    # Generic secret assignments: password / token / secret / authorization
    out = re.sub(
        r"(?i)\b(password|token|secret|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s,;\"']{4,}",
        r"\1\2***",
        out,
    )
    out = re.sub(r"(?i)(sk-|xai-|tvly-|fc-)[A-Za-z0-9_\-]{8,}", r"\1***", out)
    out = re.sub(r"(//[^/\s:]+:)[^@/\s]+@", r"\1***@", out)
    return out


def to_public_error(
    exc: BaseException,
    *,
    provider: str | None = None,
) -> PublicError:
    if isinstance(exc, AllSearchError):
        prov = provider
        if hasattr(exc, "provider") and getattr(exc, "provider"):
            prov = getattr(exc, "provider")
        return PublicError(
            provider=prov,
            code=exc.code,
            message=redact_text(exc.message),
            retryable=exc.retryable,
        )
    return PublicError(
        provider=provider,
        code="internal_error",
        message=redact_text(str(exc) or exc.__class__.__name__),
        retryable=False,
    )
