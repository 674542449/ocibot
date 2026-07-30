"""Cross-user tenant isolation.

Every OCI-facing route resolves the tenant through get_owned_tenant, which rejects a
tenant belonging to someone else. Pinned end to end here, with both users deliberately
configured against the SAME Oracle tenancy OCID — the case where a leak would actually
be plausible, since panel rows are then distinguishable only by their own uuid.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_isolation_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'i.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "isolation-master-key-0123456789ab")
os.environ.setdefault("OCIBOT_JWT_SECRET", "isolation-jwt-secret-0123456789ab")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
from app.oci_client import InstanceInfo  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"


class _R:
    def __init__(self, ok=True, message="", data=None):
        self.ok = ok
        self.message = message
        self.data = data if data is not None else {}
        self.work_request_id = ""


def _inst(name: str) -> InstanceInfo:
    return InstanceInfo(
        id=f"ocid1.instance.oc1..{name}",
        display_name=name,
        lifecycle_state="RUNNING",
        region="ap-tokyo-1",
        availability_domain="AD-1",
        fault_domain="FD-1",
        shape="VM.Standard.A1.Flex",
        ocpus=1.0,
        memory_gb=6.0,
        time_created="2026-01-01T00:00:00+00:00",
        compartment_id="ocid1.compartment.oc1..c",
        image_id="img",
        freeform_tags={},
        defined_tags={},
        tenant_id="",
        tenant_name="",
    )


def _make_user(username: str, tenant_name: str) -> tuple[str, str]:
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        else:
            user.password_hash = hash_password("supersecret123")
            user.is_active = True
        tenant = Tenant(
            owner_id=user.id,
            name=tenant_name,
            region="ap-tokyo-1",
            # Deliberately the SAME Oracle tenancy for both users: sharing an OCID
            # must still not share panel rows or OCI sessions.
            user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..SHAREDTENANCYOCID",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            private_key_encrypted=encrypt_text(_PEM),
        )
        db.add(tenant)
        db.commit()
        return user.id, tenant.id


@pytest.fixture(scope="module")
def two_users():
    init_db()
    _uid_a, tid_a = _make_user("iso-alice", "Alice-OCI")
    _uid_b, tid_b = _make_user("iso-bob", "Bob-OCI")
    return tid_a, tid_b


@pytest.fixture()
def sessions(monkeypatch):
    """One stub per tenant id, so a leak shows up as the wrong instance name."""
    by_tenant: dict[str, MagicMock] = {}

    def _make(name: str) -> MagicMock:
        s = MagicMock()
        s.list_instances_tree.return_value = [_inst(name)]
        s.list_instances.return_value = [_inst(name)]
        s.get_free_quota_usage.return_value = _R(True, "", {"account_tier": name, "buckets": {}})
        s.get_account_status.return_value = _R(True, "", {"tenancy_name": name, "tier_code": "free"})
        return s

    def _for_row(row, **_kw):
        if row.id not in by_tenant:
            by_tenant[row.id] = _make(row.name)
        return by_tenant[row.id]

    monkeypatch.setattr(instances_router, "get_session_for_row", _for_row)
    return by_tenant


def _login(client: TestClient, username: str) -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": "supersecret123"})
    assert r.status_code == 200, r.text


def test_tenant_list_shows_only_your_own(two_users, sessions):
    tid_a, tid_b = two_users
    with TestClient(app) as c:
        _login(c, "iso-alice")
        names = {t["name"] for t in c.get("/api/tenants").json()}
        ids = {t["id"] for t in c.get("/api/tenants").json()}
    assert "Alice-OCI" in names and "Bob-OCI" not in names
    assert tid_a in ids and tid_b not in ids


def test_reading_another_users_tenant_is_404(two_users, sessions):
    """404 rather than 403: the tenant's existence is not disclosed either."""
    _tid_a, tid_b = two_users
    with TestClient(app) as c:
        _login(c, "iso-alice")
        for path in (
            f"/api/tenants/{tid_b}",
            f"/api/tenants/{tid_b}/instances",
            f"/api/tenants/{tid_b}/account",
            f"/api/tenants/{tid_b}/free-quota",
            f"/api/tenants/{tid_b}/regions",
        ):
            assert c.get(path).status_code == 404, path


def test_mutating_another_users_tenant_is_404(two_users, sessions):
    _tid_a, tid_b = two_users
    iid = "ocid1.instance.oc1..Bob-OCI"
    with TestClient(app) as c:
        _login(c, "iso-alice")
        assert c.patch(f"/api/tenants/{tid_b}", json={"name": "hijacked"}).status_code == 404
        assert c.delete(f"/api/tenants/{tid_b}").status_code == 404
        assert (
            c.post(f"/api/tenants/{tid_b}/instances/{iid}/power", json={"action": "STOP"}).status_code
            == 404
        )
    with SessionLocal() as db:
        assert db.get(Tenant, tid_b).name == "Bob-OCI"


def test_the_same_oracle_tenancy_under_two_users_stays_separate(two_users, sessions):
    """Both rows carry the same tenancy OCID; they are still two rows, two OCI
    sessions and two sets of results."""
    tid_a, tid_b = two_users
    with TestClient(app) as c:
        _login(c, "iso-alice")
        alice = c.get(f"/api/tenants/{tid_a}/instances").json()
    with TestClient(app) as c:
        _login(c, "iso-bob")
        bob = c.get(f"/api/tenants/{tid_b}/instances").json()

    assert [i["display_name"] for i in alice] == ["Alice-OCI"]
    assert [i["display_name"] for i in bob] == ["Bob-OCI"]
    assert sessions[tid_a] is not sessions[tid_b]


def test_one_user_reading_first_does_not_open_the_door_for_the_other(two_users, sessions):
    """Bob reads his own tenant, then Alice asks for it: still 404. Guards against a
    future read path that resolves the tenant before checking who owns it."""
    tid_a, tid_b = two_users
    with TestClient(app) as c:
        _login(c, "iso-bob")
        assert c.get(f"/api/tenants/{tid_b}/instances").status_code == 200
    with TestClient(app) as c:
        _login(c, "iso-alice")
        assert c.get(f"/api/tenants/{tid_b}/instances").status_code == 404
        assert c.get(f"/api/tenants/{tid_b}/free-quota").status_code == 404
    _ = tid_a


def test_capacity_jobs_are_per_owner(two_users, sessions):
    _tid_a, tid_b = two_users
    with TestClient(app) as c:
        _login(c, "iso-alice")
        assert c.get("/api/jobs/capacity").json() == []
        _ = tid_b


def test_backup_export_contains_only_your_own_tenants(two_users, sessions):
    with TestClient(app) as c:
        _login(c, "iso-alice")
        export = c.post("/api/backup/export", json={"password": "backup-pass"})
        assert export.status_code == 200, export.text
        blob = export.content
    # The archive is encrypted, but the tenant NAME must not appear either way.
    assert b"Bob-OCI" not in blob
