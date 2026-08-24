"""登录审计视图对管理员的可见范围。

背景：`routers/audit.py` 的注释宣称管理员现在能看到撞库尝试，但实现只兑现了一半。
`auth.py` 在**用户名存在**时会把 `auth.login_failed` / `auth.totp_failed` 归属到被匹配
上的账号（owner_id 非空），而审计查询把管理员限制在 `owner_id == 自己 OR IS NULL`。
于是管理员看得见针对**瞎编用户名**的失败，恰恰看不见针对**真实账号**的失败 ——
后者才是撞库真正的信号，「密码对了但 2FA 挡住」更是这张表里信号最强的一行。

这里钉住修复后的取舍：
  - auth_only=true 且请求者是管理员 → 放宽到全部账号的登录类事件；
  - 其余任何组合 → owner 维度的隔离原样不动（这才是隐私隔离要挡的东西）；
  - 放宽只在 `_AUTH_ACTIONS` 白名单内生效，租户/实例等操作不得随之泄露。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_authvis_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "authvis-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "authvis-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import AuditLog, User  # noqa: E402

_PASSWORD = "supersecret123"
_WRONG = "definitely-not-the-password"

_ADMIN = "authvis-admin"
_BOB = "authvis-bob"
_CAROL = "authvis-carol"


def _reset_login_limiter() -> None:
    """登录限流器是进程内的，键是客户端 IP —— 整个测试套件共用 "testclient" 这一个。

    30 次/5 分钟的 IP 上限是按真实攻击者定的，不是按一个连着登录十几次的测试模块定的；
    不清空的话，本模块能不能跑绿取决于在它之前有多少模块登录过，表现为随机的 429。
    与 tests/test_locked_tenant.py 同样的处理。
    """
    from web.backend.rate_limit import login_ip_limiter, login_user_limiter

    login_ip_limiter._hits.clear()
    login_user_limiter._hits.clear()


def _upsert_user(db, name: str, *, is_admin: bool) -> str:
    row = db.query(User).filter(User.username == name).one_or_none()
    if row is None:
        row = User(username=name, password_hash=hash_password(_PASSWORD), is_admin=is_admin)
        db.add(row)
    else:
        row.password_hash = hash_password(_PASSWORD)
        row.is_admin = is_admin
        row.is_active = True
        row.totp_enabled = False
    db.commit()
    return row.id


@pytest.fixture(scope="module")
def users() -> dict[str, str]:
    init_db()
    with SessionLocal() as db:
        return {
            _ADMIN: _upsert_user(db, _ADMIN, is_admin=True),
            _BOB: _upsert_user(db, _BOB, is_admin=False),
            _CAROL: _upsert_user(db, _CAROL, is_admin=False),
        }


def _login(client: TestClient, name: str, password: str = _PASSWORD):
    _reset_login_limiter()
    return client.post("/api/auth/login", json={"username": name, "password": password})


def _audit(client: TestClient, *, auth_only: bool):
    r = client.get("/api/audit", params={"limit": 500, "auth_only": auth_only})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


def _rows_for(listed, target: str, action: str | None = None) -> list[dict]:
    return [
        r
        for r in listed
        if r["target"] == target and (action is None or r["action"] == action)
    ]


def _seed(owner_id: str | None, action: str, target: str, detail: str = "") -> None:
    """直接写审计行。

    归属逻辑（谁的 owner_id）在 auth.py / admin.py 里，本模块要钉的是**查询端**的
    可见范围，所以这些行的写入路径不必走一遍真实请求。走真实请求更有价值的那一条
    —— 针对真实账号的密码错误 —— 下面用的是真的 POST /api/auth/login。
    """
    with SessionLocal() as db:
        db.add(AuditLog(owner_id=owner_id, action=action, target=target, detail=detail))
        db.commit()


def test_admin_sees_failed_logins_against_a_real_account(users):
    """核心复现：针对真实账号的密码错误，管理员必须看得见。

    修复前这里返回的是「瞎编用户名有，真实账号没有」—— 正好把撞库信号滤掉了。
    """
    with TestClient(app) as c:
        for _ in range(6):
            assert _login(c, _BOB, _WRONG).status_code == 401
        # 对照组：不存在的用户名，owner_id 为空，修复前后都应该可见。
        assert _login(c, "authvis-ghost", _WRONG).status_code == 401
        assert _login(c, _ADMIN).status_code == 200
        listed = _audit(c, auth_only=True)

    ghost = _rows_for(listed, "authvis-ghost", "auth.login_failed")
    assert ghost, "匿名失败登录本来就可见，这条断言挂了说明是别的东西坏了"

    bob = _rows_for(listed, _BOB, "auth.login_failed")
    assert len(bob) >= 6, f"管理员看不到针对真实账号 {_BOB} 的失败登录：{listed!r}"
    assert json.loads(bob[0]["detail"])["reason"] == "bad_password"


def test_admin_sees_the_highest_signal_event_totp_failed(users):
    """密码正确、只被 2FA 挡住 —— 这行归属到被攻击的账号，所以管理员原本看不到。"""
    _seed(users[_CAROL], "auth.totp_failed", _CAROL, '{"reason": "bad_totp_password_was_correct"}')
    with TestClient(app) as c:
        assert _login(c, _ADMIN).status_code == 200
        listed = _audit(c, auth_only=True)
    assert _rows_for(listed, _CAROL, "auth.totp_failed"), f"totp_failed 仍不可见：{listed!r}"


def test_admin_auth_view_covers_the_whole_account_lifecycle(users):
    """开关 2FA、改密码、注册也属于登录时间线，`_AUTH_ACTIONS` 原来漏了它们。"""
    _seed(users[_BOB], "auth.register", _BOB)
    _seed(users[_BOB], "auth.totp_enabled", _BOB)
    _seed(users[_BOB], "auth.totp_disabled", _BOB)
    with TestClient(app) as c:
        # 改密码走真实路径：它会 bump token_version，是登录时间线的一部分。
        # 新密码必须与旧的不同（路由自己有这条 400 检查），所以改完再把库里的
        # hash 恢复回去，后面的用例仍然用 _PASSWORD 登录。
        assert _login(c, _BOB).status_code == 200
        r = c.post(
            "/api/auth/change-password",
            json={"old_password": _PASSWORD, "new_password": _PASSWORD + "-rotated"},
        )
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        with SessionLocal() as db:
            _upsert_user(db, _BOB, is_admin=False)
        assert _login(c, _ADMIN).status_code == 200
        listed = _audit(c, auth_only=True)
    actions = {r["action"] for r in _rows_for(listed, _BOB)}
    for expected in (
        "auth.register",
        "auth.totp_enabled",
        "auth.totp_disabled",
        "auth.change_password",
    ):
        assert expected in actions, f"{expected} 不在登录时间线里：{sorted(actions)}"


def test_admin_credential_actions_stay_out_of_the_auth_view(users):
    """admin.reset_password / admin.user_patch 走真实路由写进库，但不进「只看登录」。

    看起来它们该进去（都会顶掉会话），这里把**不进去**钉住，因为这是量过的：
      - 它们归属在执行操作的管理员名下，不是被改的账号，所以对被重置者毫无用处；
      - 它们不是 auth.* 前缀，而 tests/test_login_audit.py 断言 auth_only 返回的每一
        行都以 auth. 开头。整套跑时全部模块共用一个 sqlite 库，本模块按字母序在它前面
        跑、先写下这两行，加进白名单后那条断言实测会挂。
    要改成「算」，得连写入端归属、那条不变量、前端 ACTION_LABELS 一起改。
    """
    with TestClient(app) as c:
        assert _login(c, _ADMIN).status_code == 200
        r = c.post(f"/api/admin/users/{users[_CAROL]}/reset-password")
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        r = c.patch(f"/api/admin/users/{users[_CAROL]}", json={"is_active": True})
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        auth_view = _audit(c, auth_only=True)
        full_view = _audit(c, auth_only=False)
    # 行确实写进去了（否则下面那条断言是空转）。
    assert {r["action"] for r in _rows_for(full_view, _CAROL)} >= {
        "admin.reset_password",
        "admin.user_patch",
    }
    assert not [r for r in auth_view if not r["action"].startswith("auth.")], (
        "「只看登录」里出现了 auth.* 之外的动作"
    )
    # 重置会清掉 2FA 并换掉密码，把 carol 恢复成后面用例期待的样子。
    with SessionLocal() as db:
        _upsert_user(db, _CAROL, is_admin=False)


def test_the_exception_does_not_leak_non_login_activity(users):
    """放宽只在登录类白名单内生效。租户/实例操作才是 owner 隔离真正要挡的东西。"""
    _seed(users[_BOB], "instance.terminate", "ocid1.instance.authvis")
    _seed(users[_BOB], "tenant.create", "authvis-tenant")
    with TestClient(app) as c:
        assert _login(c, _ADMIN).status_code == 200
        auth_view = _audit(c, auth_only=True)
        full_view = _audit(c, auth_only=False)
    assert not _rows_for(auth_view, "ocid1.instance.authvis")
    assert not _rows_for(auth_view, "authvis-tenant")
    # auth_only=false 的默认视图完全没动过：管理员照样只看自己 + 匿名。
    assert not _rows_for(full_view, "ocid1.instance.authvis")
    assert not _rows_for(full_view, "authvis-tenant")


def test_default_view_keeps_owner_isolation_for_login_rows_too(users):
    """例外的作用域是 auth_only=true。不带这个参数时，别人的登录行仍然不可见。"""
    with TestClient(app) as c:
        assert _login(c, _BOB, _WRONG).status_code == 401
        assert _login(c, _ADMIN).status_code == 200
        full_view = _audit(c, auth_only=False)
    assert not _rows_for(full_view, _BOB, "auth.login_failed"), (
        "auth_only=false 的视图不该跟着放宽"
    )


def test_non_admin_sees_only_their_own_login_rows(users):
    """普通用户的 auth_only 视图不受影响：既看不到别人的，也看不到匿名行。"""
    _seed(users[_CAROL], "auth.login_failed", _CAROL, '{"reason": "bad_password"}')
    _seed(None, "auth.login_failed", "authvis-anonymous-name")
    with TestClient(app) as c:
        assert _login(c, _BOB, _WRONG).status_code == 401
        assert _login(c, _BOB).status_code == 200
        listed = _audit(c, auth_only=True)
    assert _rows_for(listed, _BOB, "auth.login_failed"), "自己的失败登录本来就该看得见"
    assert not _rows_for(listed, _CAROL), f"普通用户看到了别人的登录行：{listed!r}"
    assert not _rows_for(listed, "authvis-anonymous-name"), "普通用户看到了匿名行"
