"""POST /tenants/{id}/launch-quota-check — the pre-submit free-tier verdict.

The panel uses this to block a launch BEFORE showing the confirm step. It exists so
the UI does not reimplement the quota math: it runs the same check_launch_quota the
launch path enforces with, so the pre-submit verdict cannot drift from what the
server will actually do.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_qcheck_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'q.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "qcheck-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "qcheck-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"


class _Result:
    def __init__(self, ok=True, message="", data=None):
        self.ok = ok
        self.message = message
        self.data = data or {}


def _bucket(used: float, limit: float) -> dict:
    return {
        "used": used,
        "limit": float(limit),
        "remaining": float(limit) - used,
        "ratio": used / limit if limit else 0.0,
        "status": "ok",
        "soft": False,
    }


def _snapshot(a1_ocpu=2.0, a1_mem=12.0, e2=1, disk=100.0, tier="free", incomplete=False) -> dict:
    return {
        "account_tier": tier,
        "free_only_mode": tier != "paid",
        "read_incomplete": incomplete,
        "limits": {
            "a1_ocpu": 4,
            "a1_memory_gb": 24,
            "e2_micro_count": 2,
            "block_storage_gb": 200,
            "object_storage_gb": 20,
            "public_ip_soft": 2,
        },
        "usage": {
            "a1_ocpu": a1_ocpu,
            "a1_memory_gb": a1_mem,
            "e2_micro_count": e2,
            "block_storage_gb": disk,
        },
        "remaining": {
            "a1_ocpu": 4 - a1_ocpu,
            "a1_memory_gb": 24 - a1_mem,
            "e2_micro_count": 2 - e2,
            "block_storage_gb": 200 - disk,
        },
        "buckets": {
            "a1_ocpu": _bucket(a1_ocpu, 4),
            "a1_memory_gb": _bucket(a1_mem, 24),
            "e2_micro_count": _bucket(e2, 2),
            "block_storage_gb": _bucket(disk, 200),
        },
        "overall_status": "ok",
        "summary_lines": ["A1 remaining"],
    }


# Module-scoped: a per-test login would trip the login rate limiter (10 / 5 min per
# IP+username) once this file grew past ten tests.
@pytest.fixture(scope="module")
def client():
    init_db()
    username = "quota-check-user"
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        else:
            user.password_hash = hash_password("supersecret123")
            user.is_active = True
            user.totp_enabled = False
        tenant = db.query(Tenant).filter(Tenant.owner_id == user.id).one_or_none()
        if tenant is None:
            tenant = Tenant(
                owner_id=user.id,
                name="QT",
                region="ap-tokyo-1",
                user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                private_key_encrypted=encrypt_text(_PEM),
            )
            db.add(tenant)
        db.commit()
        tenant_id = tenant.id

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": username, "password": "supersecret123"})
        assert r.status_code == 200, r.text
        yield c, tenant_id


def _stub_usage(monkeypatch, snapshot: dict):
    session = MagicMock()
    session.get_free_quota_usage.return_value = _Result(True, "", dict(snapshot))
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)
    return session


def _body(**over) -> dict:
    body = {
        "shape": "VM.Standard.A1.Flex",
        "image_id": "ocid1.image.oc1..i",
        "ocpus": 2,
        "memory_in_gbs": 12,
        "boot_volume_size_in_gbs": 50,
        "boot_volume_vpus_per_gb": 10,
    }
    body.update(over)
    return body


def test_config_that_exactly_fits_is_allowed(client, monkeypatch):
    c, tid = client
    _stub_usage(monkeypatch, _snapshot())
    r = c.post(f"/api/tenants/{tid}/launch-quota-check", json=_body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["blocked"] is False, d
    assert d["errors"] == []


def test_response_carries_the_usage_the_panel_shows(client, monkeypatch):
    """The UI renders used/limit/remaining from this payload."""
    c, tid = client
    _stub_usage(monkeypatch, _snapshot())
    d = c.post(f"/api/tenants/{tid}/launch-quota-check", json=_body()).json()
    assert d["usage"]["a1_ocpu"] == 2.0
    assert d["usage"]["block_storage_gb"] == 100.0
    assert d["limits"]["a1_ocpu"] == 4
    assert d["remaining"]["a1_memory_gb"] == 12.0
    assert d["buckets"]["block_storage_gb"]["limit"] == 200.0
    assert d["account_tier"] == "free"


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"ocpus": 3}, "A1 额度不足"),
        ({"memory_in_gbs": 20}, "A1 额度不足"),
        ({"boot_volume_size_in_gbs": 150}, "块存储"),
        ({"shape": "VM.Standard.E4.Flex"}, "付费 Shape"),
    ],
)
def test_over_quota_configs_are_blocked_with_a_reason(client, monkeypatch, over, needle):
    c, tid = client
    _stub_usage(monkeypatch, _snapshot())
    d = c.post(f"/api/tenants/{tid}/launch-quota-check", json=_body(**over)).json()
    assert d["blocked"] is True, d
    assert any(needle in e for e in d["errors"]), d["errors"]


def test_paid_account_is_still_blocked_by_default(client, monkeypatch):
    """A "paid" tier must NOT silently disable the caps.

    Oracle reports "paid" for any account that was ever upgraded, which is the
    common case for people who only use free resources. Inferring intent from the
    tier meant 50GB already used plus a 200GB boot volume (250 > 200) passed with a
    mere warning. Intent is now the tenant's explicit free_only_mode flag, default on.
    """
    c, tid = client
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == tid).update({"account_tier": "paid"})
        db.commit()
    try:
        _stub_usage(monkeypatch, _snapshot(tier="paid"))
        d = c.post(f"/api/tenants/{tid}/launch-quota-check", json=_body(ocpus=3)).json()
        assert d["blocked"] is True, d
        assert d["free_only_mode"] is True
    finally:
        with SessionLocal() as db:
            db.query(Tenant).filter(Tenant.id == tid).update({"account_tier": ""})
            db.commit()


def test_disabling_free_only_allows_deliberate_overage(client, monkeypatch):
    """Opting out is explicit and per tenant, so paid use is still possible."""
    c, tid = client
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == tid).update(
            {"account_tier": "paid", "free_only_mode": False}
        )
        db.commit()
    try:
        _stub_usage(monkeypatch, _snapshot(tier="paid"))
        d = c.post(f"/api/tenants/{tid}/launch-quota-check", json=_body(ocpus=3)).json()
        assert d["blocked"] is False, d
        assert d["free_only_mode"] is False
        assert d["warnings"], "overage should still be warned about"
    finally:
        with SessionLocal() as db:
            db.query(Tenant).filter(Tenant.id == tid).update(
                {"account_tier": "", "free_only_mode": True}
            )
            db.commit()


def test_the_reported_scenario_is_blocked(client, monkeypatch):
    """Exactly the case reported: 50GB already used on an AMD free instance, then an
    A1 free instance with 4 OCPU / 24 GB / 200 GB boot. 50 + 200 = 250 > 200."""
    c, tid = client
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == tid).update({"account_tier": "paid"})
        db.commit()
    try:
        _stub_usage(monkeypatch, _snapshot(a1_ocpu=0.0, a1_mem=0.0, e2=1, disk=50.0, tier="paid"))
        d = c.post(
            f"/api/tenants/{tid}/launch-quota-check",
            json=_body(ocpus=4, memory_in_gbs=24, boot_volume_size_in_gbs=200),
        ).json()
        assert d["blocked"] is True, d
        assert any("块存储" in e for e in d["errors"]), d["errors"]
        # A1 4/24 alone is exactly the whole free allowance, so only disk should fail.
        assert not any("A1" in e for e in d["errors"]), d["errors"]
    finally:
        with SessionLocal() as db:
            db.query(Tenant).filter(Tenant.id == tid).update({"account_tier": ""})
            db.commit()


def test_incomplete_read_is_surfaced(client, monkeypatch):
    """The panel warns that the numbers may be an undercount."""
    c, tid = client
    _stub_usage(monkeypatch, _snapshot(incomplete=True))
    d = c.post(f"/api/tenants/{tid}/launch-quota-check", json=_body()).json()
    assert d["read_incomplete"] is True


def test_other_users_tenant_is_404(client, monkeypatch):
    c, _tid = client
    _stub_usage(monkeypatch, _snapshot())
    r = c.post("/api/tenants/not-mine/launch-quota-check", json=_body())
    assert r.status_code == 404
