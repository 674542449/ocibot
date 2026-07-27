"""Backup/restore must preserve the 副区 → primary link.

Restore reissues every tenant id, so the exported ``parent_tenant_id`` is a stale
reference: it has to be remapped through the archive ids. Without that the restored
secondary-region row comes back as a standalone tenant and the launch page stops
offering it as a region of its primary.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_bkregion_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'b.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "bkregion-master-key-0123456789abc")
os.environ.setdefault("OCIBOT_JWT_SECRET", "bkregion-jwt-secret-0123456789abc")

pytest.importorskip("fastapi")
pytest.importorskip("pyzipper")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"
_USER = "backup-region-user"


@pytest.fixture(scope="module")
def client():
    init_db()
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == _USER).one_or_none()
        if user is None:
            user = User(username=_USER, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        parent = Tenant(
            owner_id=user.id,
            name="BK-Primary",
            region="ap-tokyo-1",
            user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            private_key_encrypted=encrypt_text(_PEM),
        )
        db.add(parent)
        db.flush()
        db.add(
            Tenant(
                owner_id=user.id,
                name="BK-Primary · 大阪",
                region="ap-osaka-1",
                parent_tenant_id=parent.id,
                user_ocid=parent.user_ocid,
                tenancy_ocid=parent.tenancy_ocid,
                fingerprint=parent.fingerprint,
                private_key_encrypted=parent.private_key_encrypted,
                free_only_mode=False,
            )
        )
        db.commit()

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": _USER, "password": "supersecret123"})
        assert r.status_code == 200, r.text
        yield c


def test_restored_secondary_region_still_points_at_its_primary(client):
    c = client
    export = c.post("/api/backup/export", json={"password": "backup-pass"})
    assert export.status_code == 200, export.text

    restored = c.post(
        "/api/backup/import",
        data={"password": "backup-pass"},
        files={"file": ("b.zip", io.BytesIO(export.content), "application/zip")},
    )
    assert restored.status_code == 200, restored.text
    new_ids = restored.json()["tenant_ids"]
    assert len(new_ids) == 2

    with SessionLocal() as db:
        rows = [db.get(Tenant, i) for i in new_ids]
        primary = next(r for r in rows if r.region == "ap-tokyo-1")
        secondary = next(r for r in rows if r.region == "ap-osaka-1")
        # Remapped to the NEW primary, not the archived id.
        assert secondary.parent_tenant_id == primary.id
        assert primary.parent_tenant_id == ""
        # 副区 rows are billable by design; a restore must not silently re-arm the
        # free-cap guard and start refusing launches there.
        assert secondary.free_only_mode is False
