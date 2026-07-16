from __future__ import annotations

import pytest

from allsearch.errors import UnsafeURLError, redact_text
from allsearch.security import canonicalize_url, validate_public_http_url


def test_canonicalize_strips_tracking_and_fragment():
    url = "https://Example.COM:443/path/?utm_source=x&id=1#frag"
    assert canonicalize_url(url) == "https://example.com/path?id=1"


def test_reject_localhost_and_credentials():
    with pytest.raises(UnsafeURLError):
        validate_public_http_url("http://localhost/admin", resolve_dns=False)
    with pytest.raises(UnsafeURLError):
        validate_public_http_url("https://user:pass@example.com/", resolve_dns=False)
    with pytest.raises(UnsafeURLError):
        validate_public_http_url("ftp://example.com/a", resolve_dns=False)


def test_reject_literal_private_ip():
    with pytest.raises(UnsafeURLError):
        validate_public_http_url("http://127.0.0.1/x", resolve_dns=False)
    with pytest.raises(UnsafeURLError):
        validate_public_http_url("http://192.168.1.5/x", resolve_dns=False)


def test_canonicalize_is_order_independent_for_query_params():
    a = canonicalize_url("https://example.com/p?a=1&b=2")
    b = canonicalize_url("https://example.com/p?b=2&a=1")
    assert a == b


def test_canonicalize_does_not_raise_on_malformed_port():
    # Must not raise; returns a usable (unmerged) key.
    key = canonicalize_url("https://example.com:bad/x")
    assert isinstance(key, str) and key


def test_redact_common_secret_assignments():
    assert "***" in redact_text("password=hunter2")
    assert "***" in redact_text("token=abcdef1234567890")
    assert "***" in redact_text("secret=my-private-value")
    assert "***" in redact_text("Authorization: Bearer abcdef123456")
    # non-secret text survives
    assert redact_text("plain status message") == "plain status message"
