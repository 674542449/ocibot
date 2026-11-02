"""API 密钥 + 给脚本调用的「换多出口 IP」接口（/api/v1/...）。

要守住的：
  - 新建密钥要再输一次密码，明文只返回这一次，列表里只有开头几位；
  - /api/v1 只认密钥：登录 cookie 不行；密钥也调不了别的接口；
  - 改密码 / 退出所有会话之后旧密钥一起失效，删掉的密钥立刻不能用；
  - 按私网 IP 地址换一个、一键换全部，都走网页同一套更换（ip_rotation），主 IP 不能换；
  - 别人的租户、免费账号都挡下。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_api_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "api-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "api-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import AuditLog, Tenant, User  # noqa: E402
from web.backend.routers import open_api  # noqa: E402

from tests._keys import TEST_PEM

_PW = "supersecret123"


def _R(ok=True, message="", data=None):
    return SimpleNamespace(ok=ok, message=message, data=data if data is not None else {})


class _Fake:
    """够这几个接口用的 TenantSession 替身。"""

    def __init__(self):
        self.planned: list = []

    def get_instance(self, instance_id, resolve_ips=False):
        return SimpleNamespace(compartment_id="comp")

    def secondary_private_ip_ids(self, instance_id, compartment_id):
        return {"10.0.0.11": "pv1", "10.0.0.12": "pv2"}

    def list_multi_ips(self, instance_id, compartment_id):
        return _R(True, "", {
            "primary_private_ip": "10.0.0.5",
            "items": [
                {"private_ip_id": "pv1", "private_ip": "10.0.0.11", "managed": True,
                 "public_ip_id": "a", "public_ip": "1.1.1.1", "public_ip_name": "proxy-10"},
                {"private_ip_id": "pv2", "private_ip": "10.0.0.12", "managed": False,
                 "public_ip_id": "b", "public_ip": "1.1.1.2", "public_ip_name": "proxy-2"},
            ],
        })

    def plan_multi_ip_rotation(self, instance_id, compartment_id, private_ip_ids, hints=None, expected=None):
        self.planned.append({"ids": list(private_ip_ids), "expected": expected})
        addr = {"pv1": "10.0.0.11", "pv2": "10.0.0.12"}
        return _R(True, "", {"items": [
            {"private_ip_id": i, "private_ip": addr[i], "old_public_ip_id": "x", "old_ip": "1.1.1.1",
             "name": "proxy", "compartment_id": "comp", "pool_id": "", "new_public_ip_id": "",
             "new_ip": "", "status": "pending", "message": ""}
            for i in private_ip_ids
        ]})

    def rotate_multi_ips(self, items, on_progress=None):
        return _R(True, f"已更换 {len(items)} 个公网 IP（私网 IP 不变）", {"items": [
            dict(it, status="done", new_ip=f"9.9.9.{n}") for n, it in enumerate(items, 1)
        ]})


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


@pytest.fixture
def fake(monkeypatch):
    f = _Fake()
    monkeypatch.setattr(open_api, "get_session_for_row", lambda row: f)
    return f


def _mk_user(name: str, tier: str = "paid") -> str:
    """一个用户 + 一个租户；返回租户 id。"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == name).one_or_none()
        if user is None:
            user = User(username=name, password_hash=hash_password(_PW))
            db.add(user)
            db.flush()
        tenant = db.query(Tenant).filter(Tenant.owner_id == user.id).first()
        if tenant is None:
            tenant = Tenant(owner_id=user.id, name=f"T-{name}", region="ap-tokyo-1",
                            private_key_encrypted=encrypt_text(TEST_PEM))
            db.add(tenant)
        tenant.account_tier = tier
        db.commit()
        return tenant.id


def _login(c: TestClient, name: str) -> None:
    from web.backend.rate_limit import login_ip_limiter, login_user_limiter

    login_ip_limiter._hits.clear()
    login_user_limiter._hits.clear()
    r = c.post("/api/auth/login", json={"username": name, "password": _PW})
    assert r.status_code == 200, r.text


def _new_key(c: TestClient) -> str:
    r = c.post("/api/api-keys", json={"name": "脚本", "password": _PW})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _api(key: str) -> TestClient:
    """不带任何 cookie 的客户端，只拿密钥。"""
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {key}"
    return c


def _base(tid: str, iid: str) -> str:
    return f"/api/v1/tenants/{tid}/instances/{iid}/multi-ips"


# ---------------------------------------------------------------- 密钥本身


def test_a_key_needs_the_password_and_is_shown_only_once():
    _mk_user("api-create")
    with TestClient(app) as c:
        _login(c, "api-create")
        assert c.post("/api/api-keys", json={"name": "x", "password": "wrong-password"}).status_code == 400
        r = c.post("/api/api-keys", json={"name": "代理机", "password": _PW})
        assert r.status_code == 200, r.text
        key = r.json()["key"]
        assert key.startswith("ak_") and len(key) > 40
        listed = c.get("/api/api-keys").json()["items"]
        mine = next(i for i in listed if i["id"] == r.json()["id"])
        assert "key" not in mine and mine["prefix"] == key[:9] and mine["valid"] is True
        assert mine["name"] == "代理机"
        assert key not in str(listed), "列表里不能再出现明文"


def test_v1_does_not_accept_a_login_cookie(fake):
    tid = _mk_user("api-cookie")
    with TestClient(app) as c:
        _login(c, "api-cookie")
        r = c.get(_base(tid, "ocid1.instance.oc1..cookie"))
        assert r.status_code == 401 and "API 密钥" in r.json()["detail"]


def test_a_key_cannot_call_the_normal_endpoints(fake):
    _mk_user("api-scope")
    with TestClient(app) as c:
        _login(c, "api-scope")
        key = _new_key(c)
    api = _api(key)
    assert api.get("/api/tenants").status_code == 401
    assert api.get("/api/auth/me").status_code == 401


def test_logout_all_and_delete_both_kill_a_key(fake):
    tid = _mk_user("api-revoke")
    iid = "ocid1.instance.oc1..revoke"
    with TestClient(app) as c:
        _login(c, "api-revoke")
        k1 = _new_key(c)
        assert _api(k1).get(_base(tid, iid)).status_code == 200
        c.post("/api/auth/logout-all")
    r = _api(k1).get(_base(tid, iid))
    assert r.status_code == 401 and "已失效" in r.json()["detail"]

    with TestClient(app) as c:
        _login(c, "api-revoke")
        stale = [i for i in c.get("/api/api-keys").json()["items"] if not i["valid"]]
        assert stale, "失效的密钥要在列表里标出来"
        k2 = _new_key(c)
        assert _api(k2).get(_base(tid, iid)).status_code == 200
        kid = next(i["id"] for i in c.get("/api/api-keys").json()["items"] if i["prefix"] == k2[:9])
        assert c.delete(f"/api/api-keys/{kid}").status_code == 200
    assert _api(k2).get(_base(tid, iid)).status_code == 401


def test_the_x_api_key_header_works_too(fake):
    tid = _mk_user("api-header")
    with TestClient(app) as c:
        _login(c, "api-header")
        key = _new_key(c)
    plain = TestClient(app)
    r = plain.get(_base(tid, "ocid1.instance.oc1..hdr"), headers={"X-API-Key": key})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 换 IP


def test_the_list_is_in_the_panel_s_name_order_and_has_no_ocids(fake):
    tid = _mk_user("api-list")
    with TestClient(app) as c:
        _login(c, "api-list")
        key = _new_key(c)
    r = _api(key).get(_base(tid, "ocid1.instance.oc1..list"))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["proxy-2", "proxy-10"]
    assert set(items[0]) == {"private_ip", "public_ip", "name", "managed"}


def test_rotating_one_private_ip_and_waiting_for_the_result(fake):
    tid = _mk_user("api-one")
    iid = "ocid1.instance.oc1..one"
    with TestClient(app) as c:
        _login(c, "api-one")
        key = _new_key(c)
    r = _api(key).post(_base(tid, iid) + "/10.0.0.12/rotate?wait=30")
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done"
    assert [(i["private_ip"], i["new_ip"]) for i in job["items"]] == [("10.0.0.12", "9.9.9.1")]
    assert fake.planned[-1] == {"ids": ["pv2"], "expected": None}

    status = _api(key).get(_base(tid, iid) + "/rotate").json()["job"]
    assert status["state"] == "done" and "private_ip_id" not in status["items"][0]
    with SessionLocal() as db:
        row = db.query(AuditLog).filter(AuditLog.action == "api.multi_ip.rotate", AuditLog.target == iid).one()
        assert key[:9] in row.detail and "10.0.0.12" in row.detail


def test_rotate_all_takes_every_secondary_ip(fake):
    tid = _mk_user("api-all")
    with TestClient(app) as c:
        _login(c, "api-all")
        key = _new_key(c)
    r = _api(key).post(_base(tid, "ocid1.instance.oc1..all") + "/rotate-all?wait=30")
    assert r.status_code == 200, r.text
    assert sorted(fake.planned[-1]["ids"]) == ["pv1", "pv2"]
    assert len(r.json()["job"]["items"]) == 2


def test_the_primary_an_unknown_and_a_malformed_address_are_refused(fake):
    tid = _mk_user("api-bad")
    iid = "ocid1.instance.oc1..bad"
    with TestClient(app) as c:
        _login(c, "api-bad")
        key = _new_key(c)
    api = _api(key)
    r = api.post(_base(tid, iid) + "/10.0.0.5/rotate")
    assert r.status_code == 404 and "主私网 IP" in r.json()["detail"]
    assert api.post(_base(tid, iid) + "/10.0.0.99/rotate").status_code == 404
    assert api.post(_base(tid, iid) + "/not-an-ip/rotate").status_code == 400
    assert fake.planned == [], "被挡下的一个都不能开始规划"


def test_a_failed_rotation_is_not_reported_as_ok_after_waiting(fake, monkeypatch):
    """脚本多半只看 ok：等到的结果是失败，就不能还回 true。"""
    tid = _mk_user("api-fail")
    with TestClient(app) as c:
        _login(c, "api-fail")
        key = _new_key(c)
    monkeypatch.setattr(fake, "rotate_multi_ips", lambda items, on_progress=None: _R(
        False, "已更换 0 个公网 IP（私网 IP 不变）；1 个失败，见下表",
        {"items": [dict(items[0], status="failed", message="申请新 IP 失败")]},
    ))
    r = _api(key).post(_base(tid, "ocid1.instance.oc1..fail") + "/10.0.0.11/rotate?wait=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["job"]["state"] == "failed"
    assert "1 个失败" in body["message"]


def test_without_waiting_ok_only_means_started(fake):
    tid = _mk_user("api-nowait")
    with TestClient(app) as c:
        _login(c, "api-nowait")
        key = _new_key(c)
    r = _api(key).post(_base(tid, "ocid1.instance.oc1..nowait") + "/rotate-all")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "已开始" in r.json()["message"]


def test_a_broken_tenant_session_is_a_502_with_a_reason(fake, monkeypatch):
    """建会话失败（比如私钥解不开）：和其他接口一样回 502 + 原因，不是裸 500。"""
    tid = _mk_user("api-broken")
    with TestClient(app) as c:
        _login(c, "api-broken")
        key = _new_key(c)

    def broken(row):
        raise ValueError("私钥无法解密")

    monkeypatch.setattr(open_api, "get_session_for_row", broken)
    api = TestClient(app, raise_server_exceptions=False)
    api.headers["Authorization"] = f"Bearer {key}"
    for method, path in [
        ("GET", _base(tid, "ocid1.instance.oc1..b")),
        ("POST", _base(tid, "ocid1.instance.oc1..b") + "/10.0.0.11/rotate"),
        ("POST", _base(tid, "ocid1.instance.oc1..b") + "/rotate-all"),
    ]:
        r = api.request(method, path)
        assert r.status_code == 502 and "私钥无法解密" in r.json()["detail"], (path, r.status_code, r.text)


def test_another_users_tenant_and_a_free_account_are_refused(fake):
    other = _mk_user("api-victim")
    free = _mk_user("api-free", tier="free")
    with TestClient(app) as c:
        _login(c, "api-free")
        key = _new_key(c)
    api = _api(key)
    assert api.get(_base(other, "ocid1.instance.oc1..x")).status_code == 404
    assert api.post(_base(other, "ocid1.instance.oc1..x") + "/rotate-all").status_code == 404
    assert api.post(_base(free, "ocid1.instance.oc1..x") + "/rotate-all").status_code == 403
    assert fake.planned == []
