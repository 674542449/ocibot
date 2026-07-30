"""Login auditing — enough to spot credential stuffing, without storing passwords.

The events were already being written; failed attempts against a username that does
not exist carry no owner_id, and the audit endpoint filtered on owner_id, so the
panel recorded every attempt and then showed none of them.

The submitted password is deliberately never stored. On a failed attempt it is
overwhelmingly someone else's real leaked credential, or the operator's own password
with one character wrong, and this log is served to a browser, kept in Postgres and
copied into every backup archive. It also adds no detection signal: username + IP +
outcome is what identifies a stuffing run.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_loginaudit_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "loginaudit-master-key-0123456789")
os.environ.setdefault("OCIBOT_JWT_SECRET", "loginaudit-jwt-secret-0123456789")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import AuditLog, User  # noqa: E402

_PASSWORD = "supersecret123"
_WRONG = "definitely-not-the-password"


@pytest.fixture(scope="module")
def admin():
    init_db()
    name = "audit-admin"
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == name).one_or_none()
        if row is None:
            row = User(username=name, password_hash=hash_password(_PASSWORD), is_admin=True)
            db.add(row)
        else:
            row.password_hash = hash_password(_PASSWORD)
            row.is_admin = True
            row.is_active = True
            row.totp_enabled = False
        db.commit()
    return name


def _rows(action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(
            db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.created_at).all()
        )


def _detail(row: AuditLog) -> dict:
    return json.loads(row.detail or "{}")


def test_failed_login_records_username_ip_and_reason(admin):
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "not-a-real-user", "password": _WRONG})
        assert r.status_code == 401
    row = [x for x in _rows("auth.login_failed") if x.target == "not-a-real-user"][-1]
    d = _detail(row)
    assert d["reason"] == "no_such_user"
    assert d["ip"]
    # Anonymous: there is no account to attribute an invented username to.
    assert row.owner_id is None


def test_the_submitted_password_is_never_written_anywhere(admin):
    """The core constraint. Stored failed-login passwords would be a plaintext
    credential database inside the audit log."""
    secret = "PleaseD0NotStoreMe!"
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": admin, "password": secret})
        c.post("/api/auth/login", json={"username": "ghost-user", "password": secret})
    with SessionLocal() as db:
        blob = " ".join(
            f"{r.action} {r.target} {r.detail}" for r in db.query(AuditLog).all()
        )
    assert secret not in blob


def test_wrong_password_for_a_real_account_is_attributed_to_it(admin):
    """So the account's owner sees attempts against it without being an admin."""
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": admin, "password": _WRONG}).status_code == 401
    row = [x for x in _rows("auth.login_failed") if x.target == admin][-1]
    assert _detail(row)["reason"] == "bad_password"
    assert row.owner_id is not None


def test_successful_login_is_recorded_with_the_client(admin):
    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"username": admin, "password": _PASSWORD},
            headers={"User-Agent": "OCIBotTest/1.0"},
        )
        assert r.status_code == 200
    d = _detail(_rows("auth.login")[-1])
    assert d["reason"] == "ok"
    assert d["ua"] == "OCIBotTest/1.0"


def test_admin_sees_anonymous_failures_through_the_api(admin):
    """The actual bug: these rows existed but the endpoint filtered them out."""
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "sprayed-name", "password": _WRONG})
        assert (
            c.post("/api/auth/login", json={"username": admin, "password": _PASSWORD}).status_code
            == 200
        )
        listed = c.get("/api/audit", params={"limit": 200, "auth_only": True}).json()
    targets = {row["target"] for row in listed}
    assert "sprayed-name" in targets
    assert all(str(row["action"]).startswith("auth.") for row in listed)


def test_auth_only_filter_excludes_other_actions(admin):
    with SessionLocal() as db:
        owner = db.query(User).filter(User.username == admin).one().id
        db.add(AuditLog(owner_id=owner, action="instance.terminate", target="ocid1.instance..x"))
        db.commit()
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"username": admin, "password": _PASSWORD}).status_code
            == 200
        )
        auth_rows = c.get("/api/audit", params={"auth_only": True, "limit": 200}).json()
        all_rows = c.get("/api/audit", params={"limit": 200}).json()
    assert not any(r["action"] == "instance.terminate" for r in auth_rows)
    assert any(r["action"] == "instance.terminate" for r in all_rows)


def test_non_admin_does_not_see_anonymous_rows(admin):
    """Anonymous failures name accounts that are not theirs; only an admin has a
    reason to read them."""
    with SessionLocal() as db:
        plain = db.query(User).filter(User.username == "audit-plain").one_or_none()
        if plain is None:
            plain = User(username="audit-plain", password_hash=hash_password(_PASSWORD))
            db.add(plain)
        else:
            plain.password_hash = hash_password(_PASSWORD)
            plain.is_admin = False
            plain.is_active = True
        db.add(AuditLog(owner_id=None, action="auth.login_failed", target="someone-elses-name"))
        db.commit()
    with TestClient(app) as c:
        assert (
            c.post(
                "/api/auth/login", json={"username": "audit-plain", "password": _PASSWORD}
            ).status_code
            == 200
        )
        listed = c.get("/api/audit", params={"limit": 200}).json()
    assert all(r["target"] != "someone-elses-name" for r in listed)
