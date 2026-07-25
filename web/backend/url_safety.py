"""SSRF guards for outbound HTTP (webhooks, Bark custom servers, etc.)."""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse


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


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"))
        or (ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"))
        or (ip.version == 6 and ip in ipaddress.ip_network("fe80::/10"))
        # IPv4-mapped IPv6
        or (ip.version == 6 and getattr(ip, "ipv4_mapped", None) is not None and _is_blocked_ip(ip.ipv4_mapped))
    )


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
    """Resolve DNS and reject if any address is non-public. Raises ValueError."""
    h = (host or "").strip().lower().rstrip(".")
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
    if port is not None and port in {22, 25, 2375, 2376, 3306, 5432, 6379, 11211, 27017}:
        raise ValueError(f"禁止使用端口 {port}")
    resolve_and_check_host(host)
    return raw


def assert_safe_outbound_url(url: str) -> None:
    """Same as validate_public_http_url but no return (for call sites that already store URL)."""
    validate_public_http_url(url, allow_http=True)
