"""访客真实 IP —— 登录限流、审计日志、IP 封禁共用这一个判断。

``request.client.host`` 是起点：run.py 只在 OCIBOT_TRUST_PROXY=1 时让 uvicorn 从受信反代
（OCIBOT_FORWARDED_ALLOW_IPS）的 X-Forwarded-For 里取来源，别处不再自己解析那个头
（理由见 routers/auth.py::_client_ip 的 docstring）。

再往前一层是 Cloudflare：面板挂在 Cloudflare CDN 后面时，上面那一步拿到的是
**Cloudflare 节点**的地址 —— 内置 Caddy 也只会把它的直接来源（Cloudflare 节点）写进
X-Forwarded-For。于是所有访客共用几个节点 IP：登录限流按节点计数，攻击者刷满之后
运营者自己从同一个节点登录也被挡；审计日志里记的也全是节点 IP；按 IP 封禁更是会把
节点连同经过它的所有人一起封掉。

真实访客 IP 在 Cloudflare 加的 ``CF-Connecting-IP`` 里。这个头**只在来源确实是
Cloudflare 公布的 IP 段时**才采用：直连源站的人可以随便写这个头，但他的来源地址不在
这些段里，头会被忽略。已知缺口：Cloudflare WARP 用户的出口也在这些段里，直连源站时
可以伪造这个头 —— 要彻底堵上，源站防火墙应只放行 Cloudflare 段（web/AUDIT.md 有记）。
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any, Iterable, Optional, Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# https://www.cloudflare.com/ips-v4 、 https://www.cloudflare.com/ips-v6（2026-10 核对）。
# Cloudflare 很少改这份列表；改了的话这里要跟着更新，否则新段来的请求会退回按节点 IP 算。
CLOUDFLARE_RANGES = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_CLOUDFLARE_NETS = tuple(ipaddress.ip_network(n) for n in CLOUDFLARE_RANGES)


def parse_ip(value: Any) -> Optional[IPAddress]:
    """解析成 IP 地址；IPv4 映射的 IPv6（::ffff:1.2.3.4）还原成 IPv4。不是 IP 返回 None。"""
    try:
        addr = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def normalize_ip(value: Any) -> str:
    """规范写法（IPv6 压缩、映射地址还原），不是 IP 就原样返回去掉空白的串。"""
    addr = parse_ip(value)
    return str(addr) if addr is not None else str(value or "").strip()


def in_networks(addr: Optional[IPAddress], nets: Iterable[Any]) -> bool:
    if addr is None:
        return False
    return any(addr.version == n.version and addr in n for n in nets)


def is_cloudflare(value: Any) -> bool:
    return in_networks(parse_ip(value), _CLOUDFLARE_NETS)


@lru_cache(maxsize=1)
def _trust_cloudflare() -> bool:
    from web.backend.config import get_settings

    return bool(get_settings().trust_cloudflare)


def resolve(peer: str, cf_connecting_ip: Optional[str]) -> str:
    """``peer`` 是连接来源（已经过 uvicorn 的反代处理）；来自 Cloudflare 时换成真实访客 IP。"""
    if cf_connecting_ip and _trust_cloudflare() and is_cloudflare(peer):
        real = parse_ip(cf_connecting_ip)
        if real is not None:
            return str(real)
    addr = parse_ip(peer)
    return str(addr) if addr is not None else (peer or "unknown")


def from_request(request: Any) -> str:
    peer = request.client.host if request.client else ""
    return resolve(peer or "unknown", request.headers.get("cf-connecting-ip"))


def from_scope(scope: dict) -> str:
    client = scope.get("client") or ("", 0)
    peer = str(client[0] or "")
    cf = None
    for name, value in scope.get("headers") or ():
        if name == b"cf-connecting-ip":
            cf = value.decode("latin-1")
            break
    return resolve(peer or "unknown", cf)
