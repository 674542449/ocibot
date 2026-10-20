"""按 IP 封禁：命中规则的访客 IP 直接封掉，之后它的所有请求一律 403。

自动规则（管理页「IP 封禁」里可改）：
  * User-Agent 命中 UA 列表里的某一行（默认 ``curl/8.14.1``、``Mozilla/5.0``）——
    普通一行是**完全等于**（区分大小写）；以 ``~`` 开头的一行是**包含**（不分大小写），
    ``~python-requests`` 一行就能覆盖所有版本号。正常浏览器的 UA 是一长串，
    裸 ``Mozilla/5.0`` 只有脚本会发；
  * 不带 User-Agent（或为空）的请求 —— 浏览器一定会发 UA，空的只有脚本和扫描器；
    默认开启，可关；
  * 用诱饵用户名登录（默认 ``admin``）—— 面板里**没有**这个账号时才算。真有一个叫
    admin 的账号的话，封掉的就是它的主人，所以那种情况下这条规则不生效。

保存 UA 规则时拿一组正常浏览器的 UA 试一遍（BROWSER_SAMPLES），命中任何一个就拒绝
保存 —— 一条 ``~mozilla`` 会把所有访客连同管理员自己一起封掉。

永远不封：内网 / 本机 / 保留地址（只封公网 IP）、Cloudflare 节点地址，以及
OCIBOT_IP_BAN_ALLOWLIST 里的地址。前两类的意义在于「识别错了也不会封错」：面板在反代
或 Docker 端口转发后面却没配好真实 IP 时，看到的是代理 / 网关地址，封它等于封所有人。

封禁表存在数据库里（重启不丢、API 多进程共享），每个进程在内存里留一份，每
``REFRESH_SEC`` 秒同步一次 —— 封禁检查在每个请求上跑，不能每次都查库。

被封后的解封办法：管理页「IP 封禁」，或在服务器上
``bash scripts/install.sh unban <IP>``（即 ``python -m web.backend.ip_ban unban <IP>``）。
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from functools import lru_cache
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from web.backend import client_ip

log = logging.getLogger("ocibot.ip_ban")

DEFAULT_USER_AGENTS: tuple[str, ...] = ("curl/8.14.1", "Mozilla/5.0")
DEFAULT_USERNAMES: tuple[str, ...] = ("admin",)
DEFAULT_BAN_EMPTY_UA = True
CONTAINS_PREFIX = "~"
MIN_CONTAINS_LEN = 3
MAX_RULES = 100
MAX_RULE_LEN = 200

# 常见爬虫 / 扫描器 / 爆破工具。管理页「填入常见爬虫 UA」把它们追加进输入框，确认后
# 再保存 —— 不是默认规则：其中有些（python-requests、wget）也可能是运营者自己的脚本。
RECOMMENDED_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/4.0",
    "-",
    "~python-requests",
    "~python-urllib",
    "~python-httpx",
    "~aiohttp",
    "~go-http-client",
    "~okhttp",
    "~java/",
    "~apache-httpclient",
    "~libwww-perl",
    "~node-fetch",
    "~axios/",
    "~wget/",
    "~zgrab",
    "~nuclei",
    "~sqlmap",
    "~nmap",
    "~masscan",
    "~nikto",
    "~hydra",
    "~gobuster",
    "~fuzz faster u fool",
    "~censysinspect",
    "~l9explore",
    "~l9tcpid",
)

# 正常浏览器的 UA 样本（桌面 / 手机的 Chrome、Edge、Firefox、Safari，微信内置浏览器）。
# 保存 UA 规则前逐个试，命中就拒绝保存。
BROWSER_SAMPLES: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; V2309A Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.120 Mobile Safari/537.36 "
    "XWEB/1220099 MMWEBSDK/20240404 MMWEBID/2307 MicroMessenger/8.0.49.2600(0x28003133) "
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
)
# 管理页「IP 封禁」卡片上的说明和原因名称。只经管理员接口下发、不写进前端代码 ——
# 打包出来的 JS 不用登录就能下载，规则怎么触发写在那里等于公开。
ADMIN_HELP = {
    "intro": (
        "User-Agent 命中下面某一行、不带 User-Agent（开关打开时）、或用诱饵用户名登录（面板里没有这个"
        "账号时）的请求，来源 IP 会被立即封禁，之后它的所有请求都返回 403；IPv6 地址封它所在的整个 /64。"
        "内网 / 本机 / Cloudflare 节点 / 白名单（OCIBOT_IP_BAN_ALLOWLIST）地址永远不封，已经被封的 IP "
        "写进白名单后也会放行。被封在外面时，在服务器上执行 bash scripts/install.sh unban 你的IP。"
    ),
    "ua_rules": (
        "普通一行：完全等于才封（区分大小写），如 curl/8.14.1。\n"
        "~ 开头：UA 里包含这段文字就封（不分大小写、不管版本号），如 ~python-requests。\n"
        "会误伤正常浏览器的规则（如 ~mozilla）保存时会被拒绝。"
    ),
    "usernames_label": "诱饵用户名（每行一个，不区分大小写；面板里真有这个账号时不生效）",
    "empty_ua_label": "不带 User-Agent 的请求也封（浏览器一定会带 UA，空的只有脚本和扫描器）",
}
REASON_LABELS = {
    "user_agent": "封禁 UA",
    "empty_user_agent": "没有 UA",
    "honeypot_username": "诱饵用户名",
    "manual": "手动封禁",
    "lock_decoy": "锁屏诱饵密码",
}

REFRESH_SEC = 10.0
# 新封、还没确认写进数据库的 IP 在内存里钉住多久（见 _Cache.add）。
LOCAL_PIN_SEC = 600.0
IPV6_BAN_PREFIX = 64
BANNED_DETAIL = "该 IP 已被封禁"


# ---------------------------------------------------------------- 永不封禁的地址


@lru_cache(maxsize=4)
def _parse_allowlist(text: str) -> tuple[Any, ...]:
    nets = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("OCIBOT_IP_BAN_ALLOWLIST 里有一项不是 IP / CIDR，已忽略：%r", part)
    return tuple(nets)


def _allowlist() -> tuple[Any, ...]:
    from web.backend.config import get_settings

    return _parse_allowlist(get_settings().ip_ban_allowlist or "")


def allowlisted(ip: str) -> bool:
    return client_ip.in_networks(_address_of(ip), _allowlist())


def ban_key(value: Any) -> str:
    """封禁表里的键：IPv4 是地址本身，IPv6 是它所在的 /64。

    一台 IPv6 机器手里通常是一整个 /64（2^64 个地址），只封单个地址的话换一个地址就能
    接着试。/64 一般就是一台服务器或一户宽带，封整段不会殃及别人。已经是 ``…/64`` 写法
    的输入原样规范化；其它写不成 IP 的输入原样返回（由 exempt_reason 拒绝）。
    """
    text = str(value or "").strip()
    addr = client_ip.parse_ip(text)
    if addr is None:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            return text
        if net.version == 6 and net.prefixlen == IPV6_BAN_PREFIX:
            return str(net)
        return text
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/{IPV6_BAN_PREFIX}", strict=False))
    return str(addr)


def _address_of(value: Any) -> Optional[client_ip.IPAddress]:
    """地址本身；``…/64`` 的封禁键取它的网络地址。"""
    addr = client_ip.parse_ip(value)
    if addr is not None:
        return addr
    try:
        net = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        return None
    return net.network_address if net.version == 6 and net.prefixlen == IPV6_BAN_PREFIX else None


def exempt_reason(ip: str) -> str:
    """这个地址（或 IPv6 /64）不能封的原因；能封返回空串。"""
    addr = _address_of(ip)
    if addr is None:
        return "不是有效的 IP 地址"
    if client_ip.is_cloudflare(addr):
        return "这是 Cloudflare 节点地址，封它等于封掉经过这个节点的所有人"
    if not addr.is_global:
        return "内网 / 本机 / 保留地址不封（多半是反代或 Docker 网关，封它等于封所有人）"
    if client_ip.in_networks(addr, _allowlist()):
        return "在 OCIBOT_IP_BAN_ALLOWLIST 白名单里"
    return ""


# ---------------------------------------------------------------- 规则


def _clean_rules(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for v in values:
        text = str(v or "").strip()[:MAX_RULE_LEN]
        if text.startswith(CONTAINS_PREFIX):
            # 包含匹配不分大小写，存成统一的小写，免得同一条写两遍。
            text = CONTAINS_PREFIX + text[len(CONTAINS_PREFIX):].strip().lower()
        if text and text not in out:
            out.append(text)
    return out[:MAX_RULES]


class UaRules:
    """UA 规则：普通一行完全等于（区分大小写），``~`` 开头的一行包含（不分大小写）。"""

    def __init__(self, lines: Iterable[str]) -> None:
        exact: set[str] = set()
        contains: list[str] = []
        for line in lines:
            if line.startswith(CONTAINS_PREFIX):
                pattern = line[len(CONTAINS_PREFIX):].strip().lower()
                if pattern:
                    contains.append(pattern)
            elif line:
                exact.add(line)
        self.exact = frozenset(exact)
        self.contains = tuple(contains)

    def match(self, ua: str) -> str:
        """命中的那条规则（原样写法）；没命中返回空串。"""
        if not ua:
            return ""
        if ua in self.exact:
            return ua
        low = ua.lower()
        for pattern in self.contains:
            if pattern in low:
                return CONTAINS_PREFIX + pattern
        return ""


def validate_user_agent_rules(lines: list[str], *, own_ua: str = "") -> str:
    """规则有问题时返回原因，没问题返回空串。"""
    for line in lines:
        if line.startswith(CONTAINS_PREFIX):
            pattern = line[len(CONTAINS_PREFIX):].strip()
            if len(pattern) < MIN_CONTAINS_LEN:
                return f"「{line}」太短：包含匹配（~ 开头）至少要 {MIN_CONTAINS_LEN} 个字符，太短会误伤正常访客"
    rules = UaRules(lines)
    for sample in BROWSER_SAMPLES:
        hit = rules.match(sample)
        if hit:
            return f"「{hit}」会匹配正常浏览器（{sample[:70]}…），保存后所有访客都会被封"
    if own_ua:
        hit = rules.match(own_ua.strip())
        if hit:
            return f"「{hit}」匹配你当前浏览器的 User-Agent，保存后你自己会被立即封禁"
    return ""


def _read_rule(db: Session, key: str, default: tuple[str, ...]) -> list[str]:
    from web.backend.meta import get_meta

    stored = get_meta(db, key)
    if stored is None:
        return list(default)
    return _clean_rules(stored.splitlines())


def get_rules(db: Session) -> dict[str, Any]:
    from web.backend.meta import KEY_BAN_EMPTY_UA, KEY_BAN_USER_AGENTS, KEY_BAN_USERNAMES, get_meta

    empty = get_meta(db, KEY_BAN_EMPTY_UA)
    return {
        "user_agents": _read_rule(db, KEY_BAN_USER_AGENTS, DEFAULT_USER_AGENTS),
        "usernames": _read_rule(db, KEY_BAN_USERNAMES, DEFAULT_USERNAMES),
        "ban_empty_user_agent": DEFAULT_BAN_EMPTY_UA if empty is None else empty == "true",
    }


def clean_user_agent_rules(values: Iterable[Any]) -> list[str]:
    return _clean_rules(values)


def set_rules(
    db: Session,
    *,
    user_agents: Iterable[Any],
    usernames: Iterable[Any],
    ban_empty_user_agent: Optional[bool] = None,
) -> dict[str, Any]:
    """保存规则。``user_agents`` 应先经 validate_user_agent_rules 检查（管理接口会做）。"""
    from web.backend.meta import KEY_BAN_EMPTY_UA, KEY_BAN_USER_AGENTS, KEY_BAN_USERNAMES, set_meta

    uas = _clean_rules(user_agents)
    names = [n.lower() for n in _clean_rules(usernames)]
    if ban_empty_user_agent is None:
        ban_empty_user_agent = bool(get_rules(db)["ban_empty_user_agent"])
    set_meta(db, KEY_BAN_USER_AGENTS, "\n".join(uas))
    set_meta(db, KEY_BAN_USERNAMES, "\n".join(names))
    set_meta(db, KEY_BAN_EMPTY_UA, "true" if ban_empty_user_agent else "false")
    db.commit()
    _CACHE.set_rules(uas, names, ban_empty_user_agent)
    return {"user_agents": uas, "usernames": names, "ban_empty_user_agent": ban_empty_user_agent}


# ---------------------------------------------------------------- 进程内缓存


class _Cache:
    def __init__(self) -> None:
        self.ips: frozenset[str] = frozenset()
        self.ua_rules = UaRules(DEFAULT_USER_AGENTS)
        self.ban_empty_ua = DEFAULT_BAN_EMPTY_UA
        self.usernames: frozenset[str] = frozenset(DEFAULT_USERNAMES)
        self.loaded_at = 0.0
        # 本进程刚封、还没确认进了数据库的 → 到期时间。重读时并进去，不被数据库那份冲掉：
        # 否则重读恰好落在「进内存」和「提交」之间时，新封的 IP 会被放行到下一次重读；
        # 写库失败的那种更是 10 秒后就没了。
        self._pins: dict[str, float] = {}
        self._lock = threading.Lock()

    def stale(self) -> bool:
        return time.monotonic() - self.loaded_at > REFRESH_SEC

    def refresh(self) -> None:
        """从库里重读封禁表和规则。读失败保留旧的（不能因为数据库抖一下就全部放行或全部拦截）。"""
        with self._lock:
            if not self.stale():
                return  # 别的线程刚读过
            try:
                from web.backend.db import SessionLocal
                from web.backend.models import IpBan

                with SessionLocal() as db:
                    ips = frozenset(db.scalars(select(IpBan.ip)).all())
                    rules = get_rules(db)
                now = time.monotonic()
                self._pins = {k: t for k, t in self._pins.items() if t > now}
                self.ips = ips | frozenset(self._pins)
                self.ua_rules = UaRules(rules["user_agents"])
                self.ban_empty_ua = bool(rules["ban_empty_user_agent"])
                self.usernames = frozenset(rules["usernames"])
            except Exception:  # noqa: BLE001
                log.warning("refreshing IP ban list failed; keeping the previous one", exc_info=True)
            self.loaded_at = time.monotonic()

    def add(self, key: str) -> None:
        """立即拦，并钉住直到 unpin（确认进了数据库）或 LOCAL_PIN_SEC 到期。"""
        with self._lock:
            self.ips = self.ips | {key}
            self._pins[key] = time.monotonic() + LOCAL_PIN_SEC

    def unpin(self, key: str) -> None:
        """已经确认在数据库里了，之后交给重读 —— 别的进程或命令行解封时才能及时生效。"""
        with self._lock:
            self._pins.pop(key, None)

    def remove(self, keys: Iterable[str]) -> None:
        with self._lock:
            keys = set(keys)
            self.ips = self.ips - keys
            for k in keys:
                self._pins.pop(k, None)

    def blocks(self, ip: str) -> bool:
        """这个访客地址在不在封禁表里（IPv6 按所在的 /64，也认老版本存的单个地址）。"""
        ips = self.ips
        return bool(ips) and (ban_key(ip) in ips or ip in ips)

    def set_rules(self, user_agents: Iterable[str], usernames: Iterable[str], ban_empty_ua: bool) -> None:
        with self._lock:
            self.ua_rules = UaRules(user_agents)
            self.usernames = frozenset(usernames)
            self.ban_empty_ua = bool(ban_empty_ua)

    def reset(self) -> None:
        """测试用：清空并让下一次检查重新读库。"""
        with self._lock:
            self.ips = frozenset()
            self.ua_rules = UaRules(DEFAULT_USER_AGENTS)
            self.ban_empty_ua = DEFAULT_BAN_EMPTY_UA
            self.usernames = frozenset(DEFAULT_USERNAMES)
            self._pins = {}
            self.loaded_at = 0.0


_CACHE = _Cache()


def is_banned(ip: str) -> bool:
    """被封且不在白名单里。白名单优先：被封之后把自己的 IP 写进白名单就能进来，
    README 说的「白名单永远不封」对已经在列表里的也成立。"""
    if _CACHE.stale():
        _CACHE.refresh()
    return _CACHE.blocks(ip) and not allowlisted(ip)


def is_honeypot_username(normalized: str) -> bool:
    """``normalized`` 是 NFKC 归一化后的用户名（全角 ａｄｍｉｎ 也算）。"""
    if _CACHE.stale():
        _CACHE.refresh()
    return bool(normalized) and normalized.strip().lower() in _CACHE.usernames


# ---------------------------------------------------------------- 封禁 / 解封


def ban_ip(
    db: Session,
    ip: str,
    *,
    reason: str,
    detail: str = "",
    actor_id: Optional[str] = None,
) -> tuple[bool, str]:
    """封禁 ``ip``（IPv6 封它所在的 /64）。返回 ``(现在是否处于封禁状态, 说明)``；
    不能封的地址返回 False 和原因。"""
    from web.backend.audit import write_audit
    from web.backend.models import IpBan

    why_not = exempt_reason(ip)
    if why_not:
        return False, why_not
    key = ban_key(ip)
    # 先进内存（并钉住）：数据库写失败时这个进程至少已经在拦了。
    _CACHE.add(key)
    try:
        if db.scalar(select(IpBan).where(IpBan.ip == key)) is not None:
            _CACHE.unpin(key)
            return True, "已在封禁列表里"
        db.add(IpBan(ip=key, reason=reason[:32], detail=(detail or "")[:255]))
        db.commit()
    except IntegrityError:
        db.rollback()  # 同一个 IP 并发触发了两次，另一边已经写进去了
        _CACHE.unpin(key)
        return True, "已在封禁列表里"
    except SQLAlchemyError:
        db.rollback()
        log.warning("persisting IP ban failed ip=%s (blocked in this process only)", key, exc_info=True)
        minutes = int(LOCAL_PIN_SEC // 60)
        return True, f"已封禁（写入数据库失败，只在本进程临时生效约 {minutes} 分钟）"
    _CACHE.unpin(key)
    log.warning("banned ip=%s reason=%s detail=%s", key, reason, detail)
    write_audit(
        db,
        owner_id=actor_id,
        action="security.ip_banned",
        target=key,
        detail={"reason": reason, "detail": detail, "address": client_ip.normalize_ip(ip)},
    )
    return True, "已封禁"


def unban_ips(db: Session, ips: Iterable[str], *, actor_id: Optional[str] = None) -> list[str]:
    """解封，返回真的从列表里删掉的那些。"""
    from web.backend.audit import write_audit
    from web.backend.models import IpBan

    wanted: set[str] = set()
    for ip in ips:
        if str(ip or "").strip():
            # 新的键（IPv6 是 /64）和老版本存的单个地址都认：按地址解封也能解掉它的 /64。
            wanted.add(ban_key(ip))
            wanted.add(client_ip.normalize_ip(ip))
    if not wanted:
        return []
    rows = db.scalars(select(IpBan).where(IpBan.ip.in_(wanted))).all()
    removed = [r.ip for r in rows]
    for r in rows:
        db.delete(r)
    db.commit()
    # 内存里的也去掉 —— 包括数据库里没有、只在本进程拦着的那些（写库失败时留下的）。
    _CACHE.remove(wanted)
    if removed:
        write_audit(
            db,
            owner_id=actor_id,
            action="security.ip_unbanned",
            target=", ".join(sorted(removed))[:255],
            detail={"count": len(removed)},
        )
    return removed


def list_bans(db: Session, *, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
    from web.backend.models import IpBan

    total = int(db.scalar(select(func.count()).select_from(IpBan)) or 0)
    rows = db.scalars(select(IpBan).order_by(IpBan.created_at.desc()).limit(limit)).all()
    return [
        {"ip": r.ip, "reason": r.reason, "detail": r.detail, "created_at": r.created_at} for r in rows
    ], total


def ban_standalone(ip: str, *, reason: str, detail: str) -> None:
    """中间件用：自己开一个数据库会话。"""
    from web.backend.db import SessionLocal

    try:
        with SessionLocal() as db:
            ban_ip(db, ip, reason=reason, detail=detail)
    except Exception:  # noqa: BLE001
        _CACHE.add(ban_key(ip))
        log.warning("banning ip=%s failed", ip, exc_info=True)


# ---------------------------------------------------------------- 中间件


class IpBanMiddleware:
    """纯 ASGI 中间件：HTTP 和 WebSocket 都管（BaseHTTPMiddleware 管不到 WebSocket）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        from starlette.concurrency import run_in_threadpool

        if _CACHE.stale():
            await run_in_threadpool(_CACHE.refresh)
        ip = client_ip.from_scope(scope)
        if _CACHE.blocks(ip) and not allowlisted(ip):
            await _reject(scope, send)
            return
        ua = ""
        for name, value in scope.get("headers") or ():
            if name == b"user-agent":
                ua = value.decode("latin-1").strip()
                break
        where = f"{scope.get('method') or 'WS'} {scope.get('path') or ''}"
        if not ua:
            reason = "empty_user_agent" if _CACHE.ban_empty_ua else ""
            detail = f"没有 User-Agent {where}"
        else:
            hit = _CACHE.ua_rules.match(ua)
            reason = "user_agent" if hit else ""
            detail = f"UA={ua}（规则 {hit}）{where}" if hit and hit != ua else f"UA={ua} {where}"
        if reason and not exempt_reason(ip):
            await run_in_threadpool(ban_standalone, ip, reason=reason, detail=detail)
            await _reject(scope, send)
            return
        await self.app(scope, receive, send)


async def _reject(scope: dict, send: Any) -> None:
    if scope["type"] == "websocket":
        # 握手之前 close = 拒绝连接（客户端看到 403）。
        await send({"type": "websocket.close", "code": 1008})
        return
    body = ('{"detail":"%s"}' % BANNED_DETAIL).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------- 命令行


def _main(argv: list[str]) -> int:
    """被封在外面时在服务器上解封：

        python -m web.backend.ip_ban list
        python -m web.backend.ip_ban unban 1.2.3.4 [5.6.7.8 ...]
        python -m web.backend.ip_ban clear

    API 进程最多 REFRESH_SEC 秒后读到变化。
    """
    from web.backend.db import SessionLocal, init_db

    init_db()
    cmd = argv[0] if argv else "list"
    with SessionLocal() as db:
        if cmd == "list":
            rows, total = list_bans(db, limit=10_000)
            for r in rows:
                print(f"{r['ip']}\t{r['reason']}\t{r['created_at']}\t{r['detail']}")
            print(f"共 {total} 个被封禁的 IP")
            return 0
        if cmd == "unban" and len(argv) > 1:
            removed = unban_ips(db, argv[1:])
            print(f"已解封 {len(removed)} 个：{' '.join(removed) or '（列表里没有这些 IP）'}")
            return 0
        if cmd == "clear":
            rows, _ = list_bans(db, limit=1_000_000)
            removed = unban_ips(db, [r["ip"] for r in rows])
            print(f"已清空封禁列表（{len(removed)} 个）")
            return 0
    print(_main.__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
