"""A master-key mismatch must fail legibly, not as a blank 500.

``crypto_util.decrypt_text`` raises ``ValueError("无法解密私钥：主密钥不匹配或数据已损坏")``
and ``main.py`` registers no handler for it, so every unguarded call site turned
into an empty ``500 Internal Server Error``. Three of them were unguarded, and the
trigger is the documented, expected scenario: ``OCIBOT_MASTER_KEY`` rotated, an
install that ran on the built-in default before a real key was set, or a compose
restart where ``.env`` did not load.

The worst part was which three. Logging in with 2FA broke, *and so did the backup
export* — the one action that would have got the operator's credentials back out.
``POST /tenants/{id}/test`` already did this correctly (200 with ok=false and the
reason), which is the shape the rest should have matched.

A corrupt ciphertext is used to stand in for a rotated key: both reach the same
``InvalidToken`` inside Fernet, and it does not require mutating process-wide
settings that other test modules share.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_decguard_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "decguard-master-key-0123456789ab")
os.environ.setdefault("OCIBOT_JWT_SECRET", "decguard-jwt-secret-0123456789ab")

pytest.importorskip("fastapi")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PASSWORD = "supersecret123"
# A real key: TenantConfig.validate() now parses it, so a placeholder string will
# not survive the routes under test.
REAL_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode("ascii")
)
# Not a Fernet token at all — decrypt_text raises exactly as it does when the
# master key no longer matches what wrote the row.
BROKEN_CIPHERTEXT = "gAAAAABnot-a-real-fernet-token"


def _user(name: str, *, totp_secret: str | None = None) -> str:
    init_db()
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == name).one_or_none()
        if row is None:
            row = User(username=name, password_hash=hash_password(_PASSWORD))
            db.add(row)
        row.password_hash = hash_password(_PASSWORD)
        row.is_active = True
        row.totp_enabled = totp_secret is not None
        row.totp_secret_encrypted = totp_secret or ""
        db.commit()
        return row.id


def _tenant(owner_id: str, name: str, ciphertext: str) -> str:
    with SessionLocal() as db:
        row = Tenant(
            owner_id=owner_id,
            name=name,
            user_ocid="ocid1.user.oc1..aaaaaaaa" + "b" * 40,
            tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaa" + "c" * 44,
            fingerprint="12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
            region="ap-tokyo-1",
            private_key_encrypted=ciphertext,
        )
        db.add(row)
        db.commit()
        return row.id


def _login(client: TestClient, name: str) -> None:
    r = client.post("/api/auth/login", json={"username": name, "password": _PASSWORD})
    assert r.status_code == 200, r.text


def test_totp_login_says_the_master_key_is_wrong_instead_of_returning_a_blank_500():
    """The account cannot log in either way — but "500" and nothing else sends the
    operator chasing their authenticator's clock instead of their .env."""
    _user("decguard-2fa", totp_secret=BROKEN_CIPHERTEXT)
    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"username": "decguard-2fa", "password": _PASSWORD, "totp_code": "123456"},
        )
    assert r.status_code == 500
    detail = r.json().get("detail") or ""
    assert "无法解密" in detail
    assert "OCIBOT_MASTER_KEY" in detail


def test_export_skips_the_undecryptable_row_and_still_saves_the_healthy_ones():
    """Export is the rescue action for this incident, so one bad row must not take
    the whole archive down with it."""
    owner = _user("decguard-export")
    _tenant(owner, "decguard-good", encrypt_text(REAL_PEM))
    _tenant(owner, "decguard-broken", BROKEN_CIPHERTEXT)

    with TestClient(app) as c:
        _login(c, "decguard-export")
        r = c.post("/api/backup/export", json={"password": "Str0ng-backup-pass"})
    assert r.status_code == 200, r.text
    notice = json.loads(unquote(r.headers["X-OCIBot-Backup-Notice"]))
    assert notice["exported"] == 1
    assert [item["name"] for item in notice["skipped"]] == ["decguard-broken"]
    assert "无法解密" in notice["skipped"][0]["reason"]


def test_export_refuses_rather_than_producing_an_empty_archive():
    """Nothing decrypted means there is nothing to rescue — saying so beats handing
    back a zip that looks like a successful backup and restores zero tenants."""
    owner = _user("decguard-allbad")
    _tenant(owner, "decguard-allbad-1", BROKEN_CIPHERTEXT)

    with TestClient(app) as c:
        _login(c, "decguard-allbad")
        r = c.post("/api/backup/export", json={"password": "Str0ng-backup-pass"})
    assert r.status_code == 500
    assert "OCIBOT_MASTER_KEY" in (r.json().get("detail") or "")


def test_editing_a_tenant_with_an_undecryptable_key_explains_the_way_out():
    """update_tenant decrypts the stored key just to re-validate it, so even a
    colour change answered a blank 500. The user can fix it from this same form."""
    owner = _user("decguard-patch")
    tid = _tenant(owner, "decguard-patch-tenant", BROKEN_CIPHERTEXT)

    with TestClient(app) as c:
        _login(c, "decguard-patch")
        r = c.patch(f"/api/tenants/{tid}", json={"color": "#111111"})
    assert r.status_code == 400
    detail = r.json().get("detail") or ""
    assert "无法解密" in detail
    assert "重新粘贴" in detail


def test_supplying_a_new_key_in_the_same_edit_repairs_the_row():
    """The escape hatch the 400 points at has to actually work."""
    owner = _user("decguard-repair")
    tid = _tenant(owner, "decguard-repair-tenant", BROKEN_CIPHERTEXT)

    with TestClient(app) as c:
        _login(c, "decguard-repair")
        r = c.patch(f"/api/tenants/{tid}", json={"private_key_pem": REAL_PEM})
        assert r.status_code == 200, r.text
        assert r.json()["has_private_key"] is True
        # And the row is editable again afterwards.
        assert c.patch(f"/api/tenants/{tid}", json={"color": "#222222"}).status_code == 200
