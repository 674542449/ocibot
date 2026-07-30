"""SSRF guards for outbound HTTP (webhooks, Bark custom servers, etc.)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urlunparse


# Hard-blocked hostnames (cloud metadata & common internal names).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)


# Ports for service protocols that should never be an HTTP webhook target. This
# is defence in depth only — the address checks above are the real control.
_BLOCKED_PORTS = frozenset(
    {
        22,  # ssh
        23,  # telnet
        25,  # smtp
        445,  # smb
        2049,  # nfs
        2375,  # docker (plain)
        2376,  # docker (tls)
        2379,  # etcd
        3306,  # mysql
        5432,  # postgres
        6379,  # redis
        6443,  # kubernetes api
        9200,  # elasticsearch
        10250,  # kubelet
        11211,  # memcached
        27017,  # mongodb
    }
)

# Non-public IPv4 ranges that `is_private` / `is_reserved` do not all cover.
_BLOCKED_V4_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this host on this network" — 0.0.0.1 is not is_unspecified
        "169.254.0.0/16",  # link-local incl. cloud metadata 169.254.169.254
        "100.64.0.0/10",  # carrier-grade NAT — reachable internal space
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # deprecated 6to4 relay anycast
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "240.0.0.0/4",  # reserved
    )
)

# IPv6 ranges that embed or tunnel to an IPv4 address, which must be re-checked.
_NAT64_NET = ipaddress.ip_network("64:ff9b::/96")
_6TO4_NET = ipaddress.ip_network("2002::/16")


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract the IPv4 address tunnelled/translated inside an IPv6 address."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        return sixtofour
    if ip in _NAT64_NET:
        # 64:ff9b::/96 — the low 32 bits are the translated IPv4 address.
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4_NET:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if ip.version == 4:
        return any(ip in net for net in _BLOCKED_V4_NETS)
    # IPv6: unique-local / link-local, plus anything wrapping a blocked IPv4.
    if ip in ipaddress.ip_network("fc00::/7") or ip in ipaddress.ip_network("fe80::/10"):
        return True
    embedded = _embedded_v4(ip)
    if embedded is not None and _is_blocked_ip(embedded):
        return True
    return False


def hostname_is_blocked(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h in _BLOCKED_HOSTNAMES:
        return True
    if h.endswith(".local") or h.endswith(".internal") or h.endswith(".localhost"):
        return True
    # Literal IP in host
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        return _is_blocked_ip(ip)
    except ValueError:
        return False


def resolve_and_check_host(host: str) -> None:
    """Resolve DNS and reject if any address is non-public. Raises ValueError.

    Every resolved address must be public — a hostname with one public and one
    private A record is rejected outright.

    Known limitation: this is a check-then-connect sequence, so a hostname whose
    DNS answer changes between this call and the actual socket connect (DNS
    rebinding) can still slip through. Closing that needs connect-time address
    pinning; until then the mitigations are that only authenticated users can
    register outbound targets, redirects are never followed, and the send path
    re-validates.
    """
    # Normalize IDN to the same A-label the HTTP client will dial, so this check
    # and the actual connection cannot resolve two different names.
    h = normalize_host_ascii(host)
    if hostname_is_blocked(h):
        raise ValueError(f"禁止访问内网/本地地址：{host}")
    # Strip brackets for IPv6 literals already handled above
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析主机名：{host}") from exc
    if not infos:
        raise ValueError(f"无法解析主机名：{host}")
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise ValueError(f"禁止访问内网/本地地址：{host} → {addr}")


def normalize_host_ascii(host: str) -> str:
    """Return the exact A-label httpx will connect to.

    socket.getaddrinfo() encodes a non-ASCII hostname with CPython's legacy
    'idna' codec (IDNA2003 + nameprep), while httpx encodes with the idna package
    (IDNA2008, uts46). Those disagree: 'evilß.example.com' resolves as
    'evilss.example.com' during validation but httpx connects to
    'xn--evil-yna.example.com' — a different host entirely, so the SSRF check
    could be passed by one name while another is dialled. Normalize to httpx's
    form up front so both steps see the same name.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h or h.isascii():
        return h
    try:
        import idna

        # Mirror httpx's encode_host exactly: idna.encode(host.lower()) with no
        # uts46 mapping (verified in httpx/_urlparse.py). Enabling uts46 here
        # would accept codepoints httpx then refuses, i.e. validate a name that
        # never gets dialled.
        return idna.encode(h).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - idna.IDNAError and friends
        raise ValueError(f"主机名无效（国际化域名无法编码）：{host}") from exc


def validate_public_http_url(url: str, *, allow_http: bool = True) -> str:
    """Validate user-supplied webhook/Bark URL. Returns normalized URL or raises ValueError."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL 不能为空")
    if len(raw) > 2048:
        raise ValueError("URL 过长")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    if scheme == "http" and not allow_http:
        raise ValueError("仅允许 https:// URL")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("URL 不得包含用户名/密码")
    # Block odd ports commonly used for internal services? Allow standard + common alt.
    port = parsed.port
    if port is not None and port in _BLOCKED_PORTS:
        raise ValueError(f"禁止使用端口 {port}")
    ascii_host = normalize_host_ascii(host)
    resolve_and_check_host(ascii_host)
    if ascii_host == host:
        return raw
    # Persist/return the punycode form so send-time re-validation and httpx both
    # act on the same name that was checked here.
    netloc = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunparse(parsed._replace(netloc=netloc))


def assert_safe_outbound_url(url: str) -> None:
    """Same as validate_public_http_url but no return (for call sites that already store URL)."""
    validate_public_http_url(url, allow_http=True)
