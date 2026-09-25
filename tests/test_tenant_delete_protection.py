"""租户删除保护（Deletion Protection）与批量删除。

What has to hold:
  - a protected tenant cannot be deleted, singly (409) or in a batch (skipped);
  - a primary whose 副区 is protected cannot be deleted either — the 副区 goes
    with its primary, so deleting the parent would bypass the child's protection;
  - a batch deletes everything else and reports each refusal, instead of one
    protected row aborting the whole batch;
  - selecting a primary takes its 副区 along, and selecting both is not an error;
  - another account's tenant is indistinguishable from a missing one;
  - protection is a separate switch: the edit form's PATCH cannot turn it off.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_delprot_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'd.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "delprot-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "delprot-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import AuditLog, Tenant, User  # noqa: E402

from tests._keys import TEST_PEM

_PW = "supersecret123"
_USER = "delprot-a"
_OTHER = "delprot-b"


def _user_id(name: str) -> str:
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == name).one_or_none()
        if user is None:
            user = User(username=name, password_hash=hash_password(_PW))
            db.add(user)
            db.commit()
        return user.id


def _tenant(owner: str, name: str, *, parent: str = "", protected: bool = False) -> str:
    with SessionLocal() as db:
        row = Tenant(
            owner_id=owner,
            name=name,
            user_ocid="ocid1.user.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            region="ap-osaka-1" if parent else "ap-tokyo-1",
            parent_tenant_id=parent,
            private_key_encrypted=encrypt_text(TEST_PEM),
            delete_protected=protected,
        )
        db.add(row)
        db.commit()
        return row.id


def _exists(tid: str) -> bool:
    with SessionLocal() as db:
        return db.get(Tenant, tid) is not None


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


@pytest.fixture(scope="module")
def owner(_db) -> str:
    return _user_id(_USER)


@pytest.fixture(scope="module")
def client(owner):
    from web.backend.rate_limit import login_ip_limiter, login_user_limiter

    login_ip_limiter._hits.clear()
    login_user_limiter._hits.clear()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": _USER, "password": _PW})
        assert r.status_code == 200, r.text
        yield c


def test_a_new_tenant_is_unprotected_and_the_flag_is_serialized(client, owner):
    tid = _tenant(owner, "plain")
    row = next(t for t in client.get("/api/tenants").json() if t["id"] == tid)
    assert row["delete_protected"] is False


def test_toggle_protection_and_single_delete_is_refused(client, owner):
    tid = _tenant(owner, "guarded")
    r = client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": True})
    assert r.status_code == 200, r.text
    assert r.json()["delete_protected"] is True

    r = client.delete(f"/api/tenants/{tid}")
    assert r.status_code == 409, r.text
    assert "删除保护" in r.json()["detail"]
    assert _exists(tid)

    r = client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": False})
    assert r.json()["delete_protected"] is False
    assert client.delete(f"/api/tenants/{tid}").status_code == 200
    assert not _exists(tid)


def test_toggling_is_audited(client, owner):
    tid = _tenant(owner, "audited")
    client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": True})
    client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": False})
    with SessionLocal() as db:
        actions = [
            a.action
            for a in db.query(AuditLog).filter(AuditLog.owner_id == owner, AuditLog.target == "audited")
        ]
    assert "tenant.protect" in actions and "tenant.unprotect" in actions


def test_patch_cannot_switch_protection_off(client, owner):
    """The edit form sends whole-record PATCHes; protection must not ride along."""
    tid = _tenant(owner, "patched", protected=True)
    r = client.patch(f"/api/tenants/{tid}", json={"description": "x", "delete_protected": False})
    assert r.status_code == 200, r.text
    assert r.json()["delete_protected"] is True


def test_a_protected_secondary_blocks_deleting_its_primary(client, owner):
    parent = _tenant(owner, "primary-with-guarded-child")
    child = _tenant(owner, "guarded-child", parent=parent, protected=True)

    r = client.delete(f"/api/tenants/{parent}")
    assert r.status_code == 409, r.text
    assert "guarded-child" in r.json()["detail"]
    assert _exists(parent) and _exists(child)

    r = client.post("/api/tenants/batch-delete", json={"ids": [parent]})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == []
    assert _exists(parent) and _exists(child)


def test_batch_deletes_the_rest_and_reports_each_refusal(client, owner):
    a = _tenant(owner, "batch-a")
    b = _tenant(owner, "batch-b")
    guarded = _tenant(owner, "batch-guarded", protected=True)

    r = client.post(
        "/api/tenants/batch-delete", json={"ids": [a, guarded, b, "no-such-tenant"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["deleted"]) == {a, b}
    skipped = {s["id"]: s for s in body["skipped"]}
    assert set(skipped) == {guarded, "no-such-tenant"}
    assert "删除保护" in skipped[guarded]["reason"]
    assert not _exists(a) and not _exists(b)
    assert _exists(guarded)


def test_batch_takes_secondaries_along_and_tolerates_selecting_both(client, owner):
    parent = _tenant(owner, "batch-parent")
    child = _tenant(owner, "batch-child", parent=parent)
    other_child = _tenant(owner, "batch-child-2", parent=parent)

    # Child listed first, then its parent: neither order may produce an error.
    r = client.post("/api/tenants/batch-delete", json={"ids": [child, parent]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["deleted"]) == {parent, child, other_child}
    assert body["skipped"] == []
    assert "副区" in body["message"]
    for tid in (parent, child, other_child):
        assert not _exists(tid)


def test_batch_clears_a_default_pointing_at_a_deleted_tenant(client, owner):
    tid = _tenant(owner, "batch-locked")
    assert client.put("/api/auth/locked-tenant", json={"tenant_id": tid}).status_code == 200
    client.post("/api/tenants/batch-delete", json={"ids": [tid]})
    assert client.get("/api/auth/me").json()["locked_tenant_id"] == ""


def test_batch_cannot_touch_another_accounts_tenant(client, owner):
    foreign = _tenant(_user_id(_OTHER), "foreign")
    r = client.post("/api/tenants/batch-delete", json={"ids": [foreign]})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == []
    # Same reason text as a tenant that does not exist at all.
    assert r.json()["skipped"] == [{"id": foreign, "name": "", "reason": "租户不存在"}]
    assert _exists(foreign)


def test_protection_toggle_on_another_accounts_tenant_is_404(client):
    foreign = _tenant(_user_id(_OTHER), "foreign-2")
    r = client.post(f"/api/tenants/{foreign}/delete-protection", json={"protected": True})
    assert r.status_code == 404


def test_batch_request_is_bounded(client):
    assert client.post("/api/tenants/batch-delete", json={"ids": []}).status_code == 422
    too_many = [f"id-{i}" for i in range(201)]
    assert client.post("/api/tenants/batch-delete", json={"ids": too_many}).status_code == 422


# --------------------------------------------------------------------------
# 0.4.117：受保护的租户排在最前，多个之间「先保护的在前」—— 靠 delete_protected_at。
# --------------------------------------------------------------------------


def _row(client, tid: str) -> dict:
    return next(t for t in client.get("/api/tenants").json() if t["id"] == tid)


def test_protection_time_is_recorded_kept_on_repeat_and_cleared(client, owner):
    tid = _tenant(owner, "timed")
    assert _row(client, tid)["delete_protected_at"] is None

    first = client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": True}).json()
    assert first["delete_protected_at"], "开启保护必须记时间，否则排不出先后"

    # 重复开启不能重新计时 —— 否则一次重复请求就把它在列表里的位置往后挪了。
    again = client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": True}).json()
    assert again["delete_protected_at"] == first["delete_protected_at"]

    off = client.post(f"/api/tenants/{tid}/delete-protection", json={"protected": False}).json()
    assert off["delete_protected_at"] is None


def test_protection_times_follow_the_order_they_were_set(client, owner):
    a = _tenant(owner, "order-a")
    b = _tenant(owner, "order-b")
    # b 先保护，a 后保护：先后以保护时间为准，与添加顺序无关。
    tb = client.post(f"/api/tenants/{b}/delete-protection", json={"protected": True}).json()
    ta = client.post(f"/api/tenants/{a}/delete-protection", json={"protected": True}).json()
    assert tb["delete_protected_at"] <= ta["delete_protected_at"]


def test_the_frontend_sorts_protected_tenants_first_by_protection_time():
    """没有 JS 测试运行器，只能对源码下断言（同 test_locked_tenant.py 的做法）。"""
    src = (
        Path(__file__).resolve().parents[1] / "web" / "frontend" / "src" / "views" / "TenantsView.vue"
    ).read_text(encoding="utf-8")
    assert "delete_protected_at || t.created_at" in src
    assert ".sort(byProtection)" in src


def test_backup_round_trips_the_protection_time():
    from datetime import datetime, timezone

    from web.backend.routers.backup import _iso_or_empty, _parse_iso

    when = datetime(2026, 9, 26, 8, 30, tzinfo=timezone.utc)
    assert _parse_iso(_iso_or_empty(when)) == when
    # SQLite 读回来是 naive —— 按 UTC 处理，而不是丢掉。
    assert _parse_iso(_iso_or_empty(when.replace(tzinfo=None))) == when
    assert _iso_or_empty(None) == "" and _parse_iso("") is None and _parse_iso("garbage") is None
