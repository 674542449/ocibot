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


def test_idn_host_normalizes_to_the_same_label_httpx_dials():
    """Guards an SSRF bypass: two different IDNA encoders saw two different hosts.

    socket.getaddrinfo() uses CPython's legacy 'idna' codec (IDNA2003 + nameprep)
    while httpx uses the idna package (IDNA2008), so 'evilß.example.com' was
    *validated* as 'evilss.example.com' but *connected* to
    'xn--evil-yna.example.com'.
    """
    httpx = pytest.importorskip("httpx")
    from web.backend.url_safety import normalize_host_ascii

    for host in ("evilß.example.com", "例子.测试", "plain.example.com"):
        ours = normalize_host_ascii(host)
        theirs = httpx.URL(f"https://{host}").raw_host.decode("ascii")
        assert ours == theirs, f"{host}: validated {ours} but httpx dials {theirs}"

    # The legacy codec really does disagree — this is the bug being guarded.
    assert "evilß.example.com".encode("idna").decode() == "evilss.example.com"
    assert normalize_host_ascii("evilß.example.com") == "xn--evil-yna.example.com"


def test_idn_host_rejected_when_httpx_would_reject_it():
    """Stay in lockstep: no uts46 mapping, so we refuse exactly what httpx refuses."""
    httpx = pytest.importorskip("httpx")
    from web.backend.url_safety import normalize_host_ascii

    bad = "iℴ.example.com"  # U+2134 is disallowed by IDNA2008
    with pytest.raises(ValueError):
        normalize_host_ascii(bad)
    with pytest.raises(httpx.InvalidURL):
        httpx.URL(f"https://{bad}")


def test_idn_url_validation_returns_punycode(monkeypatch):
    """The stored URL must be the ASCII form that was actually checked."""
    import web.backend.url_safety as us

    seen: list[str] = []

    def fake_getaddrinfo(host, *a, **k):
        seen.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(us.socket, "getaddrinfo", fake_getaddrinfo)
    out = validate_public_http_url("https://evilß.example.com/hook")
    assert "xn--evil-yna.example.com" in out
    assert seen == ["xn--evil-yna.example.com"]


def test_idn_host_pointing_at_loopback_is_blocked(monkeypatch):
    import web.backend.url_safety as us

    monkeypatch.setattr(
        us.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))]
    )
    with pytest.raises(ValueError, match="内网"):
        validate_public_http_url("https://evilß.example.com/hook")


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
