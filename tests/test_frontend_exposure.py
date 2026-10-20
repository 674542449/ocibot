"""前台不暴露敏感信息：登录相关的判断全在服务端，浏览器只拿到用户自己提交的东西。

  * 打包出来的前端 JS 不用登录就能下载（/assets/ 是公开的静态文件），所以前端源码里
    不许写蜜罐的具体值或用意 —— 锁屏诱饵密码、诱饵用户名、封禁 UA 规则、封禁原因代码。
    这些说明只经管理员接口下发；
  * 登录 / 注册 / 改密码的应答正文里没有会话凭证（只在 HttpOnly Cookie 里，脚本读不到）；
  * /api/health 只对服务器本机 / 内网和已登录的人报版本号 —— 仓库是公开的，版本号等于
    告诉外人这里跑的是哪一份源码。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "web" / "frontend" / "src"

# 前端源码里不许出现的东西。123456 例外放过 Telegram bot token 的占位符「123456:ABC」。
FORBIDDEN = [
    (re.compile(r"123456(?!:)"), "锁屏诱饵密码"),
    (re.compile("诱饵"), "蜜罐的用意"),
    (re.compile(r"honeypot", re.I), "蜜罐原因代码"),
    (re.compile(r"lock_decoy"), "蜜罐原因代码"),
    (re.compile(r"curl/8\.14\.1"), "封禁 UA 规则"),
]


def test_frontend_source_does_not_reveal_the_honeypots():
    hits = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".vue", ".ts", ".js", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, what in FORBIDDEN:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line} {what}: {m.group(0)!r}")
    assert not hits, "前端代码会被公开下载，这些应改由管理员接口下发：\n" + "\n".join(hits)


# ---------------------------------------------------------------- 接口应答

_TMP = tempfile.mkdtemp(prefix="ocibot_exposure_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'd.db').as_posix()}")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.config import get_settings  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import User  # noqa: E402

_USER = "exposure-admin"
_PW = "exposure-password-1"


@pytest.fixture(autouse=True)
def _user():
    init_db()
    with SessionLocal() as db:
        if db.query(User).filter(User.username == _USER).one_or_none() is None:
            db.add(User(username=_USER, password_hash=hash_password(_PW), is_admin=True))
            db.commit()
    from web.backend import lock_screen

    with SessionLocal() as db:
        lock_screen.disable(db)


def test_the_login_response_carries_no_session_token():
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": _USER, "password": _PW})
    assert r.status_code == 200
    assert r.json() == {"username": _USER}
    assert c.cookies.get("ocibot_token"), "凭证在 Cookie 里"
    assert "httponly" in r.headers["set-cookie"].lower()


def test_health_tells_the_version_only_to_people_who_may_know_it():
    version = get_settings().app_version
    # 服务器本机 / 内网（README 和 install.sh 的 curl、容器 healthcheck、面板内更新）。
    for ip in ("127.0.0.1", "172.18.0.1", "10.0.0.2"):
        assert TestClient(app, client=(ip, 5000)).get("/api/health").json()["version"] == version
    # 外网未登录：只有状态。
    outsider = TestClient(app, client=("8.8.8.8", 5000))
    body = outsider.get("/api/health").json()
    assert body["status"] == "ok" and "version" not in body
    # 外网但已登录：看得到（侧栏、排障都要）。
    insider = TestClient(app, client=("8.8.8.8", 5001))
    assert insider.post("/api/auth/login", json={"username": _USER, "password": _PW}).status_code == 200
    assert insider.get("/api/health").json()["version"] == version
    # 伪造 / 过期的凭证不算登录。
    outsider.cookies.set("ocibot_token", "not-a-jwt")
    assert "version" not in outsider.get("/api/health").json()


def test_the_admin_api_supplies_the_explanations_instead():
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": _USER, "password": _PW}).status_code == 200
    lock = c.get("/api/admin/lock-screen").json()
    assert "123456" in lock["help"]
    bans = c.get("/api/admin/ip-bans").json()
    assert bans["help"]["intro"] and bans["help"]["ua_rules"]
    assert bans["reason_labels"]["lock_decoy"]
