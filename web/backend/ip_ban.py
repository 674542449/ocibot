"""按 IP 封禁：命中规则的访客 IP 直接封掉，之后它的所有请求一律 403。

两条自动规则（管理页「IP 封禁」里可改）：
  * User-Agent **完全等于**列表里的某一项（默认 ``curl/8.14.1``、``Mozilla/5.0``）——
    正常浏览器的 UA 是一长串，裸 ``Mozilla/5.0`` 只有脚本会发；
  * 用诱饵用户名登录（默认 ``admin``）—— 面板里**没有**这个账号时才算。真有一个叫
    admin 的账号的话，封掉的就是它的主人，所以那种情况下这条规则不生效。

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
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from web.backend import client_ip

log = logging.getLogger("ocibot.ip_ban")

DEFAULT_USER_AGENTS: tuple[str, ...] = ("curl/8.14.1", "Mozilla/5.0")
DEFAULT_USERNAMES: tuple[str, ...] = ("admin",)
MAX_RULES = 50
MAX_RULE_LEN = 200
REFRESH_SEC = 10.0
BANNED_DETAIL = "该 IP 已被封禁"


# ---------------------------------------------------------------- 永不封禁的地址


def _allowlist() -> list[Any]:
    from web.backend.config import get_settings

    nets = []
    for part in (get_settings().ip_ban_allowlist or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("OCIBOT_IP_BAN_ALLOWLIST 里有一项不是 IP / CIDR，已忽略：%r", part)
    return nets


def exempt_reason(ip: str) -> str:
    """这个地址不能封的原因；能封返回空串。"""
    addr = client_ip.parse_ip(ip)
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
        text = str(v or "").strip()
        if text and text not in out:
            out.append(text[:MAX_RULE_LEN])
    return out[:MAX_RULES]


def _read_rule(db: Session, key: str, default: tuple[str, ...]) -> list[str]:
    from web.backend.meta import get_meta

    stored = get_meta(db, key)
    if stored is None:
        return list(default)
    return _clean_rules(stored.splitlines())


def get_rules(db: Session) -> dict[str, list[str]]:
    from web.backend.meta import KEY_BAN_USER_AGENTS, KEY_BAN_USERNAMES

    return {
        "user_agents": _read_rule(db, KEY_BAN_USER_AGENTS, DEFAULT_USER_AGENTS),
        "usernames": _read_rule(db, KEY_BAN_USERNAMES, DEFAULT_USERNAMES),
    }


def set_rules(db: Session, *, user_agents: Iterable[Any], usernames: Iterable[Any]) -> dict[str, list[str]]:
    from web.backend.meta import KEY_BAN_USER_AGENTS, KEY_BAN_USERNAMES, set_meta

    uas = _clean_rules(user_agents)
    names = [n.lower() for n in _clean_rules(usernames)]
    set_meta(db, KEY_BAN_USER_AGENTS, "\n".join(uas))
    set_meta(db, KEY_BAN_USERNAMES, "\n".join(names))
    db.commit()
    _CACHE.set_rules(uas, names)
    return {"user_agents": uas, "usernames": names}


# ---------------------------------------------------------------- 进程内缓存


class _Cache:
    def __init__(self) -> None:
        self.ips: frozenset[str] = frozenset()
        self.user_agents: frozenset[str] = frozenset(DEFAULT_USER_AGENTS)
        self.usernames: frozenset[str] = frozenset(DEFAULT_USERNAMES)
        self.loaded_at = 0.0
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
                self.ips = ips
                self.user_agents = frozenset(rules["user_agents"])
                self.usernames = frozenset(rules["usernames"])
            except Exception:  # noqa: BLE001
                log.warning("refreshing IP ban list failed; keeping the previous one", exc_info=True)
            self.loaded_at = time.monotonic()

    def add(self, ip: str) -> None:
        with self._lock:
            self.ips = self.ips | {ip}

    def remove(self, ips: Iterable[str]) -> None:
        with self._lock:
            self.ips = self.ips - set(ips)

    def set_rules(self, user_agents: Iterable[str], usernames: Iterable[str]) -> None:
        with self._lock:
            self.user_agents = frozenset(user_agents)
            self.usernames = frozenset(usernames)

    def reset(self) -> None:
        """测试用：清空并让下一次检查重新读库。"""
        with self._lock:
            self.ips = frozenset()
            self.user_agents = frozenset(DEFAULT_USER_AGENTS)
            self.usernames = frozenset(DEFAULT_USERNAMES)
            self.loaded_at = 0.0


_CACHE = _Cache()


def is_banned(ip: str) -> bool:
    if _CACHE.stale():
        _CACHE.refresh()
    return ip in _CACHE.ips


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
    """封禁 ``ip``。返回 ``(现在是否处于封禁状态, 说明)``；不能封的地址返回 False 和原因。"""
    from web.backend.audit import write_audit
    from web.backend.models import IpBan

    ip = client_ip.normalize_ip(ip)
    why_not = exempt_reason(ip)
    if why_not:
        return False, why_not
    # 先进内存：数据库写失败时这个进程至少已经在拦了。
    _CACHE.add(ip)
    try:
        if db.scalar(select(IpBan).where(IpBan.ip == ip)) is not None:
            return True, "已在封禁列表里"
        db.add(IpBan(ip=ip, reason=reason[:32], detail=(detail or "")[:255]))
        db.commit()
    except IntegrityError:
        db.rollback()  # 同一个 IP 并发触发了两次，另一边已经写进去了
        return True, "已在封禁列表里"
    except SQLAlchemyError:
        db.rollback()
        log.warning("persisting IP ban failed ip=%s (blocked in this process only)", ip, exc_info=True)
        return True, "已封禁（写入数据库失败，仅本进程生效，重启后失效）"
    log.warning("banned ip=%s reason=%s detail=%s", ip, reason, detail)
    write_audit(
        db,
        owner_id=actor_id,
        action="security.ip_banned",
        target=ip,
        detail={"reason": reason, "detail": detail},
    )
    return True, "已封禁"


def unban_ips(db: Session, ips: Iterable[str], *, actor_id: Optional[str] = None) -> list[str]:
    """解封，返回真的从列表里删掉的那些。"""
    from web.backend.audit import write_audit
    from web.backend.models import IpBan

    wanted = {client_ip.normalize_ip(ip) for ip in ips if str(ip or "").strip()}
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
        _CACHE.add(client_ip.normalize_ip(ip))
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
        if ip in _CACHE.ips:
            await _reject(scope, send)
            return
        ua = ""
        for name, value in scope.get("headers") or ():
            if name == b"user-agent":
                ua = value.decode("latin-1").strip()
                break
        # UA **完全等于**某条规则（区分大小写，只去掉首尾空白）。
        if ua and ua in _CACHE.user_agents and not exempt_reason(ip):
            detail = f"UA={ua} {scope.get('method') or 'WS'} {scope.get('path') or ''}"
            await run_in_threadpool(ban_standalone, ip, reason="user_agent", detail=detail)
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
