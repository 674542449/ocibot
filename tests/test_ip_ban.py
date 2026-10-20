"""按 IP 封禁（web/backend/ip_ban.py）与 Cloudflare 后的真实 IP（web/backend/client_ip.py）。

要守住的事：
  * UA **完全等于** curl/8.14.1 或 Mozilla/5.0 的公网 IP 被封，之后它的任何请求都是 403；
    真实浏览器的长 UA、相近的版本号不受影响；
  * 用诱饵用户名 admin 登录直接封 IP（全角 / 大小写变体也算）—— 但面板里真有一个
    admin 账号时不生效，否则封的是账号主人；
  * 内网 / 本机 / Cloudflare 节点 / 白名单地址永远不封：识别错了也不会把所有人封掉；
  * 在 Cloudflare 后面时按 CF-Connecting-IP 的真实访客 IP 封、按它限流；这个头只在来源
    确实是 Cloudflare 段时才采用，直连伪造它没用；
  * 管理员能查看、手动封、解封、改规则，且不能把自己封掉；服务器上能用命令行解封。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_ipban_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'd.db').as_posix()}")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from web.backend import ip_ban  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.meta import KEY_BAN_USER_AGENTS, KEY_BAN_USERNAMES  # noqa: E402
from web.backend.models import AppMeta, IpBan, User  # noqa: E402

_ADMIN = "ipban-admin"
_PW = "ipban-password-123"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CF_EDGE = "104.16.1.1"  # 在 Cloudflare 公布的 104.16.0.0/13 里
CF_EDGE_2 = "172.64.3.4"


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with SessionLocal() as db:
        db.query(IpBan).delete()
        db.query(AppMeta).filter(AppMeta.key.in_([KEY_BAN_USER_AGENTS, KEY_BAN_USERNAMES])).delete()
        db.query(User).filter(User.username.in_(["admin", "Admin"])).delete()
        if db.query(User).filter(User.username == _ADMIN).one_or_none() is None:
            db.add(User(username=_ADMIN, password_hash=hash_password(_PW), is_admin=True))
        db.commit()
    ip_ban._CACHE.reset()
    yield
    ip_ban._CACHE.reset()


def _client(ip: str) -> TestClient:
    return TestClient(app, client=(ip, 40000))


def _banned_ips() -> set[str]:
    with SessionLocal() as db:
        return {r[0] for r in db.query(IpBan.ip).all()}


def _admin(ip: str = "8.8.8.8") -> TestClient:
    c = _client(ip)
    r = c.post("/api/auth/login", json={"username": _ADMIN, "password": _PW}, headers={"User-Agent": BROWSER_UA})
    assert r.status_code == 200, r.text
    return c


# ---------------------------------------------------------------- UA 规则


@pytest.mark.parametrize("ua", ["curl/8.14.1", "Mozilla/5.0"])
def test_an_exact_banned_user_agent_bans_the_ip_for_good(ua):
    c = _client("9.9.9.9")
    r = c.get("/api/health", headers={"User-Agent": ua})
    assert r.status_code == 403
    assert r.json()["detail"] == ip_ban.BANNED_DETAIL
    assert _banned_ips() == {"9.9.9.9"}
    # 换成正常浏览器也没用 —— 封的是 IP。
    assert c.get("/api/health", headers={"User-Agent": BROWSER_UA}).status_code == 403
    assert c.get("/", headers={"User-Agent": BROWSER_UA}).status_code == 403
    # 别人不受影响。
    assert _client("8.8.4.4").get("/api/health", headers={"User-Agent": BROWSER_UA}).status_code == 200


@pytest.mark.parametrize("ua", [BROWSER_UA, "curl/8.14.0", "curl/8.14.10", "Curl/8.14.1", "Mozilla/5.0 (X11)"])
def test_only_an_exact_match_counts(ua):
    assert _client("9.9.9.9").get("/api/health", headers={"User-Agent": ua}).status_code == 200
    assert _banned_ips() == set()


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "172.18.0.1", "192.168.1.9", "100.64.1.1", "::1", CF_EDGE])
def test_private_loopback_and_cloudflare_addresses_are_never_banned(ip):
    """反代 / Docker 网关 / Cloudflare 节点被误当成访客时，封它等于封所有人。
    （服务器上自己 curl 一下 /api/health 也走这条：来源是本机或 Docker 网关。）"""
    assert _client(ip).get("/api/health", headers={"User-Agent": "curl/8.14.1"}).status_code == 200
    assert _banned_ips() == set()


def test_the_allowlist_is_never_banned(monkeypatch):
    from web.backend.config import get_settings

    monkeypatch.setattr(get_settings(), "ip_ban_allowlist", "9.9.9.0/24, 1.2.3.4")
    assert _client("9.9.9.9").get("/api/health", headers={"User-Agent": "curl/8.14.1"}).status_code == 200
    assert _banned_ips() == set()


def test_a_banned_ip_cannot_open_the_web_terminal_either():
    with SessionLocal() as db:
        ip_ban.ban_ip(db, "9.9.9.9", reason="manual")
    with pytest.raises(WebSocketDisconnect) as exc:
        with _client("9.9.9.9").websocket_connect("/api/ws/anything"):
            pass
    assert exc.value.code == 1008


def test_bans_survive_a_restart():
    with SessionLocal() as db:
        ip_ban.ban_ip(db, "9.9.9.9", reason="manual")
    ip_ban._CACHE.reset()  # 相当于进程重启：内存里的列表没了，从库里重读
    assert _client("9.9.9.9").get("/api/health").status_code == 403


# ---------------------------------------------------------------- 诱饵用户名


@pytest.mark.parametrize("name", ["admin", "Admin", " ADMIN ", "ａｄｍｉｎ"])
def test_logging_in_as_admin_bans_the_ip(name):
    c = _client("45.33.32.156")
    r = c.post("/api/auth/login", json={"username": name, "password": "whatever1"}, headers={"User-Agent": BROWSER_UA})
    assert r.status_code == 403
    assert _banned_ips() == {"45.33.32.156"}
    assert c.get("/api/health").status_code == 403
    with SessionLocal() as db:
        row = db.query(IpBan).one()
        assert row.reason == "honeypot_username"


def test_a_real_admin_account_is_not_a_honeypot():
    with SessionLocal() as db:
        db.add(User(username="admin", password_hash=hash_password("real-password-1")))
        db.commit()
    c = _client("45.33.32.156")
    r = c.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert r.status_code == 401, "真有 admin 账号时只是普通的登录失败"
    assert _banned_ips() == set()
    assert c.post("/api/auth/login", json={"username": "admin", "password": "real-password-1"}).status_code == 200


def test_other_wrong_usernames_are_only_rate_limited():
    r = _client("45.33.32.156").post("/api/auth/login", json={"username": "root", "password": "whatever1"})
    assert r.status_code == 401
    assert _banned_ips() == set()


# ---------------------------------------------------------------- Cloudflare


def test_behind_cloudflare_the_real_visitor_is_banned_not_the_edge():
    edge = _client(CF_EDGE)
    r = edge.get("/api/health", headers={"User-Agent": "curl/8.14.1", "CF-Connecting-IP": "9.9.9.9"})
    assert r.status_code == 403
    assert _banned_ips() == {"9.9.9.9"}
    # 同一个攻击者换一个 Cloudflare 节点进来也一样被挡。
    other_edge = _client(CF_EDGE_2)
    assert other_edge.get("/api/health", headers={"CF-Connecting-IP": "9.9.9.9"}).status_code == 403
    # 经过同一个节点的其他访客照常访问。
    assert edge.get("/api/health", headers={"CF-Connecting-IP": "8.8.4.4", "User-Agent": BROWSER_UA}).status_code == 200


def test_a_forged_cf_header_from_outside_cloudflare_is_ignored():
    """直连源站的人自己写 CF-Connecting-IP，既不能嫁祸给别人，也不能躲过封禁。"""
    r = _client("8.8.4.4").get("/api/health", headers={"User-Agent": "curl/8.14.1", "CF-Connecting-IP": "9.9.9.9"})
    assert r.status_code == 403
    assert _banned_ips() == {"8.8.4.4"}


def test_the_login_limiter_counts_real_visitors_not_the_cloudflare_edge():
    """以前按节点 IP 计数：攻击者刷满之后，从同一个节点来的运营者也被 429。"""
    edge = _client(CF_EDGE)
    for _ in range(12):
        edge.post("/api/auth/login", json={"username": "someone", "password": "x" * 8},
                  headers={"CF-Connecting-IP": "9.9.9.9", "User-Agent": BROWSER_UA})
    attacker = edge.post("/api/auth/login", json={"username": "someone", "password": "x" * 8},
                         headers={"CF-Connecting-IP": "9.9.9.9", "User-Agent": BROWSER_UA})
    assert attacker.status_code == 429
    me = edge.post("/api/auth/login", json={"username": _ADMIN, "password": _PW},
                   headers={"CF-Connecting-IP": "8.8.4.4", "User-Agent": BROWSER_UA})
    assert me.status_code == 200


# ---------------------------------------------------------------- 管理页


def test_the_admin_page_lists_bans_and_shows_what_ip_it_sees():
    with SessionLocal() as db:
        ip_ban.ban_ip(db, "9.9.9.9", reason="user_agent", detail="UA=curl/8.14.1")
    c = _admin()
    data = c.get("/api/admin/ip-bans").json()
    assert [b["ip"] for b in data["bans"]] == ["9.9.9.9"]
    assert data["total"] == 1
    assert data["rules"] == {"user_agents": ["curl/8.14.1", "Mozilla/5.0"], "usernames": ["admin"]}
    assert data["viewer"]["your_ip"] == "8.8.8.8"

    via_cf = _admin(CF_EDGE)
    viewer = via_cf.get("/api/admin/ip-bans", headers={"CF-Connecting-IP": "8.8.4.4"}).json()["viewer"]
    assert viewer == {**viewer, "your_ip": "8.8.4.4", "peer_ip": CF_EDGE, "via_cloudflare": True}


def test_manual_ban_unban_and_self_protection():
    c = _admin()
    assert c.post("/api/admin/ip-bans", json={"ip": "9.9.9.9"}).status_code == 200
    assert _client("9.9.9.9").get("/api/health").status_code == 403
    r = c.post("/api/admin/ip-bans", json={"ip": "8.8.8.8"})
    assert r.status_code == 400 and "你自己" in r.json()["detail"]
    r = c.post("/api/admin/ip-bans", json={"ip": "10.1.2.3"})
    assert r.status_code == 400 and "内网" in r.json()["detail"]

    r = c.post("/api/admin/ip-bans/unban", json={"ips": ["9.9.9.9", "1.1.1.1"]})
    assert r.json()["removed"] == ["9.9.9.9"]
    assert _client("9.9.9.9").get("/api/health").status_code == 200


def test_rules_can_be_changed_but_not_to_ban_yourself():
    c = _admin()
    r = c.put("/api/admin/ip-ban-rules", json={"user_agents": [BROWSER_UA], "usernames": ["admin"]},
              headers={"User-Agent": BROWSER_UA})
    assert r.status_code == 400 and "你自己" in r.json()["detail"]

    r = c.put("/api/admin/ip-ban-rules", json={"user_agents": ["sqlmap/1.8"], "usernames": ["Root", "admin"]})
    assert r.json()["rules"] == {"user_agents": ["sqlmap/1.8"], "usernames": ["root", "admin"]}
    assert _client("9.9.9.9").get("/api/health", headers={"User-Agent": "curl/8.14.1"}).status_code == 200
    assert _client("9.9.9.8").get("/api/health", headers={"User-Agent": "sqlmap/1.8"}).status_code == 403
    r = _client("9.9.9.7").post("/api/auth/login", json={"username": "root", "password": "whatever1"})
    assert r.status_code == 403


def test_non_admins_cannot_touch_the_ban_list():
    with SessionLocal() as db:
        if db.query(User).filter(User.username == "ipban-user").one_or_none() is None:
            db.add(User(username="ipban-user", password_hash=hash_password(_PW)))
            db.commit()
    c = _client("8.8.8.8")
    assert c.post("/api/auth/login", json={"username": "ipban-user", "password": _PW}).status_code == 200
    assert c.get("/api/admin/ip-bans").status_code == 403
    assert c.post("/api/admin/ip-bans/unban", json={"ips": ["9.9.9.9"]}).status_code == 403


# ---------------------------------------------------------------- 命令行


def test_the_command_line_can_unban(capsys):
    with SessionLocal() as db:
        ip_ban.ban_ip(db, "9.9.9.9", reason="manual")
        ip_ban.ban_ip(db, "9.9.9.8", reason="manual")
    assert ip_ban._main(["unban", "9.9.9.9"]) == 0
    assert _banned_ips() == {"9.9.9.8"}
    assert ip_ban._main(["clear"]) == 0
    assert _banned_ips() == set()
    assert "已清空" in capsys.readouterr().out
