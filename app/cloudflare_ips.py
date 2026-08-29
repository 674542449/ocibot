"""Cloudflare 官方 CDN 网段 —— 拉取、校验、兜底。

## 为什么单独一个模块

这是面板里**唯一**一处「把外部来源的数据直接写成防火墙规则」的地方。写进 NSG 的
每一条 CIDR 都是一条放行规则，所以取回来的东西必须逐条验过才能用：

  * 只认 HTTPS，且主机名钉死在 api.cloudflare.com；
  * 每个 CIDR 都过一遍 `ipaddress.ip_network()`，解析不了的直接丢；
  * 条数设上限 —— 一个被污染的响应返回一万条网段，不该变成一万条放行规则
    （NSG 每组最多 120 条，真写进去只会报错，但那时已经打了几十次 API）;
  * 拉不到就用**内置兜底表**，并且**明确告诉调用方这次用的是哪一份**。

## 数据源

`https://api.cloudflare.com/client/v4/ips` —— 官方、免鉴权、一次返回 v4+v6 和 etag。
比分别抓 /ips-v4 和 /ips-v6 两个纯文本页少一次往返，也少一处解析。
"""

from __future__ import annotations

import ipaddress
from typing import Any, Optional

# 兜底表：2026-08-30 从 https://api.cloudflare.com/client/v4/ips 取得。
#
# 存一份是因为「拉不到就整个功能不可用」太脆 —— 出口被墙、Cloudflare 抖动、
# 面板跑在无外网的内网里，都会让这个按钮变成死的。但兜底表**会过期**
# （Cloudflare 确实增删过网段），所以用到它时必须在返回里标出来，让界面能说
# 「这次用的是内置列表，可能已过期」。
_FALLBACK_IPV4 = (
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
)
_FALLBACK_IPV6 = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)
_FALLBACK_DATE = "2026-08-30"

CLOUDFLARE_IPS_URL = "https://api.cloudflare.com/client/v4/ips"
_ALLOWED_HOST = "api.cloudflare.com"
# Cloudflare 现在是 15 + 7。留足余量,但不给「无限」——见模块开头第三条。
_MAX_CIDRS = 128


def _clean(values: Any, want_version: int) -> list[str]:
    """只保留能解析、且版本正确的 CIDR，顺序去重。

    宽松地跳过坏值而不是整批失败：Cloudflare 某天多返回一个我们不认识的字段，
    不该让「加 CDN 网段」这个按钮直接不可用。但**坏值绝不放行**。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        if net.version != want_version:
            continue
        canonical = str(net)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
        if len(out) >= _MAX_CIDRS:
            break
    return out


def fallback_ips() -> dict[str, Any]:
    return {
        "ipv4": list(_FALLBACK_IPV4),
        "ipv6": list(_FALLBACK_IPV6),
        "etag": "",
        "source": "fallback",
        "fetched_at": _FALLBACK_DATE,
        "note": (
            f"未能连上 Cloudflare，使用面板内置的网段表（{_FALLBACK_DATE} 抓取）。"
            "Cloudflare 增删过网段，这份可能已经过期 —— 服务器能出网后建议重新执行一次。"
        ),
    }


def fetch_cloudflare_ips(timeout: float = 10.0) -> dict[str, Any]:
    """取 Cloudflare 官方网段。**永远返回可用的一份**，失败时退回内置表。

    返回 ``{"ipv4": [...], "ipv6": [...], "etag": str, "source": "live"|"fallback",
    "note": str}``。调用方必须把 ``source`` 透传给用户 —— 拿内置表当官方最新值
    用是这个功能最容易犯的错。
    """
    try:
        import httpx
    except Exception:  # noqa: BLE001  — 环境里没有 httpx 时不该让功能整个消失
        return fallback_ips()

    try:
        # trust_env=False：跟 self_update 一致,不让机器上的 HTTP_PROXY 把这次请求
        # 引到别处 —— 这份数据会变成防火墙放行规则。
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            resp = client.get(
                CLOUDFLARE_IPS_URL,
                headers={"Accept": "application/json", "User-Agent": "ocibot"},
            )
        # 不跟随跳转：目标是一个固定的 API 端点,一次 302 就意味着有人在中间改道。
        if resp.status_code != 200:
            return fallback_ips()
        if resp.url.host != _ALLOWED_HOST:
            return fallback_ips()
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return fallback_ips()

    if not isinstance(payload, dict) or not payload.get("success"):
        return fallback_ips()
    result = payload.get("result")
    if not isinstance(result, dict):
        return fallback_ips()

    ipv4 = _clean(result.get("ipv4_cidrs"), 4)
    ipv6 = _clean(result.get("ipv6_cidrs"), 6)
    if not ipv4:
        # 一条 IPv4 都没有,说明这份响应不对劲(Cloudflare 不可能没有 v4 网段)。
        return fallback_ips()
    return {
        "ipv4": ipv4,
        "ipv6": ipv6,
        "etag": str(result.get("etag") or ""),
        "source": "live",
        "note": "",
    }


def covers(existing_cidr: str, candidate: str) -> bool:
    """``existing_cidr`` 是否**完全包含** ``candidate``。

    用来跳过已经被更宽的规则覆盖的网段：一条 `0.0.0.0/0` 在场时，再加 15 条
    Cloudflare v4 网段一条都不会改变可达性，只是白占 NSG 那 120 条的额度。
    """
    try:
        a = ipaddress.ip_network(str(existing_cidr).strip(), strict=False)
        b = ipaddress.ip_network(str(candidate).strip(), strict=False)
    except ValueError:
        return False
    if a.version != b.version:
        return False
    return b.subnet_of(a)
