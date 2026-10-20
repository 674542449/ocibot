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


# ---------------------------------------------------------------- 不暴露产品名（0.4.149）

FRONTEND = ROOT / "web" / "frontend"
_KEYWORD = re.compile(r"ocibot", re.I)


def _public_frontend_files():
    """会进打包产物、或原样发给访客的文件（vite.config.ts 之类的构建配置不算）。"""
    yield FRONTEND / "index.html"
    for path in (FRONTEND / "public").iterdir():
        yield path
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix in {".vue", ".ts", ".js", ".css", ".html", ".json"}:
            yield path


def test_the_public_frontend_never_names_the_product():
    """登录页、图标、manifest、打包出来的 JS 谁都能下载。产品名（连同带它的存储键名、
    响应头名、环境变量名、文件名）一出现，拿去公开仓库一搜就知道这是什么、有哪些防护。"""
    hits = []
    for path in _public_frontend_files():
        data = path.read_bytes()
        text = data.decode("utf-8", "ignore")
        for m in _KEYWORD.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{path.relative_to(ROOT)}:{line}")
    assert not hits, "前端公开文件里出现了产品名：\n" + "\n".join(hits)


def test_the_built_bundle_never_names_the_product():
    dist = FRONTEND / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("前端没构建过（web/frontend/dist 不存在）")
    hits = [str(p.relative_to(ROOT)) for p in dist.rglob("*")
            if p.is_file() and _KEYWORD.search(p.read_bytes().decode("utf-8", "ignore"))]
    assert not hits, "打包产物里出现了产品名（是不是没重新构建？）：\n" + "\n".join(hits)
    # 发出去的 index.html 不带注释（源码注释里有内部路径，拿去一搜就能找到仓库）。
    assert "<!--" not in (dist / "index.html").read_text(encoding="utf-8")


def test_unauthenticated_responses_never_name_the_product():
    outsider = TestClient(app, client=("8.8.8.8", 5100))
    health = outsider.get("/api/health")
    assert health.json() == {"status": "ok"}
    for r in (
        outsider.get("/api/auth/lock-status"),
        outsider.post("/api/auth/login", json={"username": "nobody-x", "password": "whatever1"}),
        outsider.post("/api/auth/login", json={"username": "u"}, headers={"origin": "https://evil.example"}),
    ):
        assert not _KEYWORD.search(r.text), r.text
        for name, value in r.headers.items():
            assert not _KEYWORD.search(name) and not _KEYWORD.search(value), (name, value)
    # 服务器本机照样看得到（README 的 curl 127.0.0.1、install.sh、容器健康检查）。
    local = TestClient(app, client=("127.0.0.1", 5100)).get("/api/health").json()
    assert local["version"] and local["app"]


# ---------------------------------------------------------------- 部署脚本的自检请求


def test_deploy_probes_of_the_public_address_never_use_curls_default_user_agent():
    """部署脚本从服务器自己探测公网地址时，面板看到的来源是服务器自己的公网 IP；curl 的
    默认 UA（Debian 13 是 curl/8.14.1）正好在 IP 封禁的默认规则里 —— 一次 HTTPS 自检就会
    把服务器自己的 IP 封掉。打公网地址（https://域名、http://公网IP）的 curl 必须带 -A。"""
    offenders = []
    for script in ("scripts/install.sh", "scripts/setup-proxy.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            joined = line
            if line.rstrip().endswith("\\"):
                joined = line + " " + text.splitlines()[n]  # 续行
            # verify_public_https 里用的是 "$url"（= https://域名/api/health）。
            if "curl" not in line or ("/api/health" not in joined and '"$url"' not in joined):
                continue
            if "127.0.0.1" in joined or "${API_CT}" in joined:
                continue  # 本机 / 容器网络：来源是内网地址，本来就不封
            if " -A " not in line:
                offenders.append(f"{script}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
