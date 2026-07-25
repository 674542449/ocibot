"""Tests for outbound URL SSRF guards."""

from __future__ import annotations

import pytest

from web.backend.url_safety import (
    hostname_is_blocked,
    validate_public_http_url,
)


def test_blocks_localhost_and_metadata():
    assert hostname_is_blocked("localhost")
    assert hostname_is_blocked("127.0.0.1")
    assert hostname_is_blocked("169.254.169.254")
    assert hostname_is_blocked("10.0.0.5")
    assert hostname_is_blocked("192.168.1.1")
    assert hostname_is_blocked("metadata.google.internal")


def test_allows_public_hostname_shape():
    # Pure hostname blocklist — public names are not blocked by name alone.
    assert not hostname_is_blocked("api.day.app")
    assert not hostname_is_blocked("example.com")


def test_validate_public_http_url_rejects_private():
    with pytest.raises(ValueError):
        validate_public_http_url("http://127.0.0.1/hook")
    with pytest.raises(ValueError):
        validate_public_http_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ValueError):
        validate_public_http_url("http://10.1.2.3/x")
    with pytest.raises(ValueError):
        validate_public_http_url("ftp://example.com/x")
    with pytest.raises(ValueError):
        validate_public_http_url("https://user:pass@example.com/x")


def test_validate_public_http_url_accepts_https_public(monkeypatch):
    # Avoid real DNS: force resolve to a public IP.
    import web.backend.url_safety as us

    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(us.socket, "getaddrinfo", fake_getaddrinfo)
    out = validate_public_http_url("https://example.com/hook")
    assert out.startswith("https://example.com")


def test_validate_rejects_dns_to_private(monkeypatch):
    import web.backend.url_safety as us

    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("10.0.0.8", 0))]

    monkeypatch.setattr(us.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="内网"):
        validate_public_http_url("https://evil.example/hook")
