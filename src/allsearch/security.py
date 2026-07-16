"""URL canonicalization and SSRF destination validation."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from allsearch.errors import UnsafeURLError

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def canonicalize_url(url: str) -> str:
    """Normalize URL for dedupe without erasing material query parameters.

    Never raises: malformed ports/hosts degrade to returning the trimmed raw
    string so callers (e.g. merge) can skip instead of crashing.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return raw

    # parsed.port raises ValueError on malformed ports; guard it.
    try:
        port = parsed.port
    except ValueError:
        # Malformed URL (e.g. bad port). Keep a usable key but mark it unmergeable
        # by returning the raw string untouched, so distinct malformed URLs stay
        # distinct rather than silently colliding.
        return raw
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    elif port:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    # Drop fragment
    fragment = ""
    # Filter tracking params; sort remaining params for order-independent dedupe
    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query_items.sort()
    query = urlencode(query_items, doseq=True)

    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    try:
        return urlunparse((scheme, netloc, path, "", query, fragment))
    except Exception:
        return raw


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in ipaddress.ip_network("0.0.0.0/8"))
    )


def validate_public_http_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate absolute HTTP(S) public URL; reject SSRF targets.

    Returns the original URL if valid (not rewritten).
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeURLError("URL is empty")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURLError("only http/https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeURLError("URL hostname is required")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    host = parsed.hostname
    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        raise UnsafeURLError("localhost destinations are not allowed")

    # Literal IP host
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise UnsafeURLError("private/reserved IP destinations are not allowed")
        return raw
    except ValueError:
        pass

    if not resolve_dns:
        return raw

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for host: {host}") from exc

    if not infos:
        raise UnsafeURLError(f"DNS resolution returned no addresses for host: {host}")

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise UnsafeURLError("resolved destination is private/reserved and not allowed")

    return raw


def hostname_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
