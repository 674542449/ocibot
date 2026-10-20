"""登录锁屏（web/backend/lock_screen.py）。

要守住的事：
  * 没设锁屏密码时完全不生效（升级后不会把运营者挡在外面）；
  * 开了之后，登录 / 注册接口要先解锁 —— 直接调接口的脚本连账号密码都试不了；
  * 锁屏页提示的「默认密码 123456」是诱饵：公网 IP 输它直接封；锁屏密码不许设成它；
  * 输错有限流；改锁屏密码让旧的解锁凭证作废；解锁凭证不能当登录会话用；
  * 服务器上能用命令行关掉锁屏。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_lock_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'd.db').as_posix()}")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend import ip_ban, lock_screen  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import AuditLog, IpBan, User  # noqa: E402

_ADMIN = "lock-admin"
_USER = "lock-user"
_PW = "lock-password-123"
LOCK_PW = "open-sesame-9"


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with SessionLocal() as db:
        lock_screen.disable(db)
        db.query(IpBan).delete()
        for name, admin in ((_ADMIN, True), (_USER, False)):
            if db.query(User).filter(User.username == name).one_or_none() is None:
                db.add(User(username=name, password_hash=hash_password(_PW), is_admin=admin))
        db.commit()
    ip_ban._CACHE.reset()
    yield
    # 共享库：不论测试成败都关掉锁屏，免得挡住同一进程里别的模块的登录。
    with SessionLocal() as db:
        lock_screen.disable(db)
        db.query(IpBan).delete()
        db.commit()
    ip_ban._CACHE.reset()


def _client(ip: str = "testclient") -> TestClient:
    return TestClient(app) if ip == "testclient" else TestClient(app, client=(ip, 41000))


def _login(c: TestClient, name: str = _ADMIN):
    return c.post("/api/auth/login", json={"username": name, "password": _PW})


def _enable(pw: str = LOCK_PW) -> None:
    with SessionLocal() as db:
        lock_screen.set_password(db, pw)


# ---------------------------------------------------------------- 没开 / 开启


def test_nothing_changes_until_a_lock_password_is_set():
    c = _client()
    assert c.get("/api/auth/lock-status").json() == {"enabled": False, "unlocked": True, "default_hint": ""}
    assert _login(c).status_code == 200
    assert c.post("/api/auth/unlock", json={"password": "123456"}).json()["unlocked"] is True


def test_the_admin_turns_it_on_but_not_with_the_decoy_or_a_short_password():
    c = _client()
    assert _login(c).status_code == 200
    r = c.put("/api/admin/lock-screen", json={"password": "123456"})
    assert r.status_code == 400 and "诱饵" in r.json()["detail"]
    r = c.put("/api/admin/lock-screen", json={"password": "12345"})
    assert r.status_code == 400 and "至少" in r.json()["detail"]
    r = c.put("/api/admin/lock-screen", json={"password": LOCK_PW})
    assert r.status_code == 200 and r.json()["enabled"] is True
    # 开锁屏的管理员自己的会话不受影响。
    assert c.get("/api/auth/me").status_code == 200

    r = c.delete("/api/admin/lock-screen")
    assert r.json()["enabled"] is False


def test_only_admins_can_change_it():
    c = _client()
    assert _login(c, _USER).status_code == 200
    assert c.put("/api/admin/lock-screen", json={"password": LOCK_PW}).status_code == 403
    assert c.delete("/api/admin/lock-screen").status_code == 403


# ---------------------------------------------------------------- 开着的时候


def test_login_and_register_need_the_lock_opened_first():
    _enable()
    c = _client()
    assert c.get("/api/auth/lock-status").json() == {"enabled": True, "unlocked": False, "default_hint": "123456"}
    r = _login(c)
    assert r.status_code == 403 and r.json()["detail"] == lock_screen.UNLOCK_REQUIRED
    r = c.post("/api/auth/register", json={"username": "newbie", "password": "whatever-123"})
    assert r.status_code == 403 and r.json()["detail"] == lock_screen.UNLOCK_REQUIRED

    r = c.post("/api/auth/unlock", json={"password": "wrong-one"})
    assert r.status_code == 401
    with SessionLocal() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "auth.unlock_failed").count() >= 1

    r = c.post("/api/auth/unlock", json={"password": LOCK_PW})
    assert r.status_code == 200
    assert c.get("/api/auth/lock-status").json()["unlocked"] is True
    assert _login(c).status_code == 200


def test_the_decoy_password_bans_the_ip():
    _enable()
    c = _client("8.8.8.8")
    r = c.post("/api/auth/unlock", json={"password": "123456"})
    assert r.status_code == 403 and r.json()["detail"] == ip_ban.BANNED_DETAIL
    with SessionLocal() as db:
        row = db.query(IpBan).one()
        assert (row.ip, row.reason) == ("8.8.8.8", "lock_decoy")
    assert c.get("/api/health").status_code == 403
    assert c.post("/api/auth/unlock", json={"password": LOCK_PW}).status_code == 403


def test_the_decoy_with_spaces_counts_too():
    _enable()
    r = _client("8.8.4.4").post("/api/auth/unlock", json={"password": " 123456 "})
    assert r.status_code == 403
    with SessionLocal() as db:
        assert db.query(IpBan).count() == 1


def test_an_exempt_address_typing_the_decoy_just_gets_a_wrong_password():
    """内网 / 白名单地址不封：当成普通的输错，不说破那是诱饵。"""
    _enable()
    r = _client("127.0.0.1").post("/api/auth/unlock", json={"password": "123456"})
    assert r.status_code == 401
    with SessionLocal() as db:
        assert db.query(IpBan).count() == 0


def test_guessing_the_lock_password_is_rate_limited():
    _enable()
    c = _client()
    codes = [c.post("/api/auth/unlock", json={"password": f"guess-{i}"}).status_code for i in range(11)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429
    # 限流之后猜对了也不放。
    assert c.post("/api/auth/unlock", json={"password": LOCK_PW}).status_code == 429


def test_changing_the_lock_password_voids_earlier_unlocks():
    _enable()
    c = _client()
    assert c.post("/api/auth/unlock", json={"password": LOCK_PW}).status_code == 200
    _enable("another-secret-7")
    r = _login(c)
    assert r.status_code == 403 and r.json()["detail"] == lock_screen.UNLOCK_REQUIRED


def test_an_expired_unlock_is_refused():
    import jwt

    _enable()
    with SessionLocal() as db:
        stale = jwt.encode(
            {"typ": "unlock", "fp": lock_screen._fingerprint(lock_screen._stored_hash(db)),
             "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            lock_screen._signing_key(), algorithm="HS256",
        )
        assert lock_screen.token_valid(db, stale) is False
        assert lock_screen.token_valid(db, lock_screen.issue_token(db)) is True


def test_an_unlock_token_is_not_a_login_session():
    _enable()
    c = _client()
    r = c.post("/api/auth/unlock", json={"password": LOCK_PW})
    token = r.cookies.get(lock_screen.UNLOCK_COOKIE)
    assert token
    other = _client()
    other.cookies.set("ocibot_token", token)
    assert other.get("/api/auth/me").status_code == 401
    assert other.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_a_honeypot_username_is_banned_even_without_unlocking():
    _enable()
    r = _client("8.8.8.8").post("/api/auth/login", json={"username": "admin", "password": "whatever1"})
    assert r.status_code == 403 and r.json()["detail"] == ip_ban.BANNED_DETAIL


def test_the_command_line_turns_it_off(capsys):
    _enable()
    assert lock_screen._main(["off"]) == 0
    with SessionLocal() as db:
        assert lock_screen.enabled(db) is False
    assert "已关闭" in capsys.readouterr().out
