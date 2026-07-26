"""End-to-end checks for upload limits and response security headers.

Covers the async->sync conversion of the upload routes (UploadFile handling in a
threadpool handler), the request-body ceiling, the per-route bounded read, and
the baseline security headers.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path

import pytest

# Must be set before importing web.backend.db (engine is built at import time).
_TMP = tempfile.mkdtemp(prefix="ocibot_upload_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'u.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "upload-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "upload-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")
pyzipper = pytest.importorskip("pyzipper")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.main import app  # noqa: E402


_USERNAME = "upload-limits-tester"
_PASSWORD = "supersecret123"


@pytest.fixture(scope="module")
def client():
    # Context manager form runs the lifespan, which calls init_db().
    with TestClient(app) as c:
        # Seed the account directly rather than via /auth/register: the engine is
        # built at import time, so this module may share a DB that another test
        # module already populated — and self-registration closes after the first
        # user, which would leave us without a session.
        from sqlalchemy import select

        from web.backend.auth import hash_password
        from web.backend.db import SessionLocal
        from web.backend.models import User

        with SessionLocal() as db:
            existing = db.scalar(select(User).where(User.username == _USERNAME))
            if existing is None:
                db.add(User(username=_USERNAME, password_hash=hash_password(_PASSWORD)))
            else:
                existing.password_hash = hash_password(_PASSWORD)
                existing.is_active = True
                existing.totp_enabled = False
            db.commit()

        r = c.post("/api/auth/login", json={"username": _USERNAME, "password": _PASSWORD})
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
        yield c


def _encrypted_zip(password: bytes, tenants: list | None = None) -> bytes:
    buf = io.BytesIO()
    with pyzipper.AESZipFile(
        buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(password)
        zf.writestr("tenants.json", json.dumps({"version": 1, "tenants": tenants or []}).encode())
    return buf.getvalue()


def test_valid_backup_import_round_trips(client):
    """The sync handler still parses a real AES ZIP correctly."""
    r = client.post(
        "/api/backup/import",
        data={"password": "hunter22"},
        files={"file": ("b.zip", _encrypted_zip(b"hunter22"), "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 0


def test_wrong_password_is_400_not_500(client):
    r = client.post(
        "/api/backup/import",
        data={"password": "definitely-wrong"},
        files={"file": ("b.zip", _encrypted_zip(b"hunter22"), "application/zip")},
    )
    assert r.status_code == 400
    assert "密码" in r.json()["detail"]


def test_body_over_global_ceiling_is_413(client):
    """Refused up front so an oversized body is never spooled to disk."""
    r = client.post(
        "/api/backup/import",
        data={"password": "hunter22"},
        files={"file": ("big.zip", b"a" * (33 * 1024 * 1024), "application/zip")},
    )
    assert r.status_code == 413
    assert "过大" in r.json()["detail"]


def test_body_over_route_limit_is_400(client):
    """Under the global ceiling but over the backup route's own 20MB cap."""
    r = client.post(
        "/api/backup/import",
        data={"password": "hunter22"},
        files={"file": ("mid.zip", b"a" * (21 * 1024 * 1024), "application/zip")},
    )
    assert r.status_code == 400
    assert "20MB" in r.json()["detail"]


def test_upload_requires_auth():
    with TestClient(app) as anon:
        r = anon.post(
            "/api/backup/import",
            data={"password": "hunter22"},
            files={"file": ("b.zip", _encrypted_zip(b"hunter22"), "application/zip")},
        )
        assert r.status_code == 401


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert r.headers["X-Permitted-Cross-Domain-Policies"] == "none"
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "script-src 'self'" in csp


def test_hsts_only_when_cookie_secure(client):
    """HSTS on a plain-HTTP deployment would lock users out; gated on the TLS flag."""
    r = client.get("/api/health")
    assert "Strict-Transport-Security" not in r.headers  # cookie_secure defaults to 0


def test_unknown_api_path_is_json_404(client):
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json()["detail"] == "Not Found"
