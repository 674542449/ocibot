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


@pytest.mark.parametrize(
    "addr",
    [
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.1",  # 0.0.0.0/8 (only 0.0.0.0 itself is "unspecified")
        "192.0.0.1",  # IETF protocol assignments
        "192.88.99.1",  # deprecated 6to4 relay anycast
        "198.18.0.1",  # benchmarking
        "240.0.0.1",  # reserved
    ],
)
def test_blocks_non_public_v4_ranges(addr):
    """Ranges that ipaddress' is_private/is_reserved do not all flag."""
    assert hostname_is_blocked(addr)


@pytest.mark.parametrize(
    "addr",
    [
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254 (cloud metadata)
        "2002:7f00:0001::",  # 6to4 -> 127.0.0.1
        "2002:a9fe:a9fe::",  # 6to4 -> 169.254.169.254
    ],
)
def test_blocks_ipv6_wrapping_private_v4(addr):
    """IPv6 forms that translate/tunnel to a blocked IPv4 must not slip through."""
    assert hostname_is_blocked(addr)


def test_allows_public_ipv6():
    assert not hostname_is_blocked("2606:4700::1111")


def test_blocks_service_ports():
    for port in (22, 3306, 6379, 6443, 9200, 10250):
        with pytest.raises(ValueError, match="端口"):
            validate_public_http_url(f"http://example.com:{port}/hook")


def test_rejects_host_with_one_private_record(monkeypatch):
    """A split-horizon name resolving to both public and private must be refused."""
    import web.backend.url_safety as us

    def fake_getaddrinfo(host, *a, **k):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(us.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="内网"):
        validate_public_http_url("https://split.example/hook")
