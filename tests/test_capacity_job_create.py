"""POST /jobs/capacity must persist the downgrade (fallback) configs.

The route validated fallback_configs against the free-tier quota and then built
the row without them, so the worker — which reads job.fallback_configs to rotate
AD x config — only ever tried the primary config.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Must be set before importing web.backend.db (engine is built at import time).
_TMP = tempfile.mkdtemp(prefix="ocibot_capjob_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'c.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "capjob-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "capjob-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityJob, Tenant, User  # noqa: E402
from web.backend.routers import jobs as jobs_router  # noqa: E402
from web.backend.schemas import CapacityJobCreate  # noqa: E402
from web.backend.worker import Worker  # noqa: E402

_PAYLOAD = {
    "display_name": "i",
    "compartment_id": "ocid1.compartment.oc1..c",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.oc1..i",
    "subnet_id": "ocid1.subnet.oc1..s",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfakekeymaterial",
    "ocpus": 4,
    "memory_in_gbs": 24,
    "boot_volume_size_in_gbs": 50,
}


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(CapacityJob).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _no_oci(monkeypatch):
    """Stub the OCI session + quota guard so only the route logic is exercised."""
    monkeypatch.setattr(jobs_router, "get_session_for_row", lambda _row: object())
    monkeypatch.setattr(jobs_router, "enforce_launch_quota", lambda *a, **k: None)


def _seed():
    with SessionLocal() as db:
        user = User(username="cap", password_hash="x")
        db.add(user)
        db.flush()
        tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
        db.add(tenant)
        db.commit()
        return user, tenant.id, user.id


def test_fallback_configs_are_persisted():
    _user, tenant_id, owner_id = _seed()
    body = CapacityJobCreate(
        tenant_id=tenant_id,
        launch_payload=dict(_PAYLOAD),
        fallback_configs=[
            {"ocpus": 2, "memory_in_gbs": 12},
            {"ocpus": 1, "memory_in_gbs": 6},
        ],
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        out = jobs_router.create_capacity_job(body, user, db)

    assert out.fallback_configs == [
        {"ocpus": 2.0, "memory_in_gbs": 12.0},
        {"ocpus": 1.0, "memory_in_gbs": 6.0},
    ]
    with SessionLocal() as db:
        row = db.get(CapacityJob, out.id)
        assert row.fallback_configs == [
            {"ocpus": 2.0, "memory_in_gbs": 12.0},
            {"ocpus": 1.0, "memory_in_gbs": 6.0},
        ], "worker rotates job.fallback_configs; an empty list silently disables downgrades"


def test_persisted_fallbacks_drive_worker_attempt_rotation():
    """End-to-end: the stored configs are what the worker actually rotates through."""
    _user, tenant_id, owner_id = _seed()
    body = CapacityJobCreate(
        tenant_id=tenant_id,
        launch_payload=dict(_PAYLOAD),
        availability_domains=["AD-1", "AD-2"],
        fallback_configs=[{"ocpus": 2, "memory_in_gbs": 12}],
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        out = jobs_router.create_capacity_job(body, user, db)

    with SessionLocal() as db:
        job = db.get(CapacityJob, out.id)
        # 2 ADs x 2 configs (primary + 1 fallback) => 4 distinct attempt plans.
        seen = set()
        for attempt in range(4):
            job.attempts = attempt
            ad, cfg, label = Worker._attempt_plan(job)
            seen.add((ad, label))
        assert len(seen) == 4, f"expected 4 AD/config combinations, got {sorted(seen)}"
        assert any("2C/12G" == label for _ad, label in seen)


def test_rejects_fallbacks_on_fixed_shape():
    _user, tenant_id, owner_id = _seed()
    payload = dict(_PAYLOAD)
    payload["shape"] = "VM.Standard.E2.1.Micro"
    payload["ocpus"] = None
    payload["memory_in_gbs"] = None
    body = CapacityJobCreate(
        tenant_id=tenant_id,
        launch_payload=payload,
        fallback_configs=[{"ocpus": 2, "memory_in_gbs": 12}],
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            jobs_router.create_capacity_job(body, user, db)
    assert exc.value.status_code == 400
    assert "Flex" in exc.value.detail


def test_rejects_too_many_fallbacks():
    _user, tenant_id, owner_id = _seed()
    body = CapacityJobCreate(
        tenant_id=tenant_id,
        launch_payload=dict(_PAYLOAD),
        fallback_configs=[{"ocpus": 1, "memory_in_gbs": 6}] * 6,
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            jobs_router.create_capacity_job(body, user, db)
    assert exc.value.status_code == 400


def test_rejects_out_of_range_fallback_values():
    _user, tenant_id, owner_id = _seed()
    body = CapacityJobCreate(
        tenant_id=tenant_id,
        launch_payload=dict(_PAYLOAD),
        fallback_configs=[{"ocpus": 999, "memory_in_gbs": 12}],
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            jobs_router.create_capacity_job(body, user, db)
    assert exc.value.status_code == 400


def test_other_users_tenant_is_not_found():
    """Ownership check: a job may only target a tenant the caller owns."""
    _user, tenant_id, _owner = _seed()
    with SessionLocal() as db:
        intruder = User(username="intruder", password_hash="x")
        db.add(intruder)
        db.commit()
        intruder_id = intruder.id
    body = CapacityJobCreate(tenant_id=tenant_id, launch_payload=dict(_PAYLOAD))
    with SessionLocal() as db:
        intruder = db.get(User, intruder_id)
        with pytest.raises(HTTPException) as exc:
            jobs_router.create_capacity_job(body, intruder, db)
    assert exc.value.status_code == 404
