"""/api/backup/export — what the archive is protected with, and what restore reports.

The archive holds every tenant's **plaintext** OCI private key. It used to be
protected by a 6-character-minimum password over WinZip AES, whose KDF is pinned at
PBKDF2-HMAC-SHA1 / 1000 rounds for interoperability and cannot be raised. hashcat's
13600 mode runs ~10 MH/s on one commodity GPU, so the password was the only control
and its floor was six characters with no complexity rule — for a file that is a
complete, immediately usable credential set and naturally lands in Downloads or a
NAS. The repo already knew better: ConfigStore's local master password has used
PBKDF2-SHA256 at 390 000 iterations all along.

Two things had to change together: the floor, and a strong-KDF envelope *inside* the
zip so the WinZip KDF is not the last line. Old archives must still restore — a
backup that silently stops working is only discovered on the day it is needed.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_bkenv_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "bkenv-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "bkenv-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")
pyzipper = pytest.importorskip("pyzipper")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config_store import BACKUP_KDF_ITERATIONS  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PASSWORD = "supersecret123"
_GOOD_BACKUP_PASSWORD = "Str0ng-backup-pass"

REAL_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode("ascii")
)

_USER_OCID = "ocid1.user.oc1..aaaaaaaa" + "b" * 40
_TENANCY_OCID = "ocid1.tenancy.oc1..aaaaaaaa" + "c" * 44
_FINGERPRINT = "12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef"


def _archive_item(name: str, **overrides) -> dict:
    item = {
        "id": f"archive-{name}",
        "name": name,
        "user_ocid": _USER_OCID,
        "tenancy_ocid": _TENANCY_OCID,
        "fingerprint": _FINGERPRINT,
        "region": "ap-tokyo-1",
        "compartment_ocid": "",
        "description": "",
        "enabled": True,
        "color": "#3B82F6",
        "private_key_pem": REAL_PEM,
    }
    item.update(overrides)
    return item


def _legacy_zip(payload, password: str) -> bytes:
    """An archive in the pre-envelope format: plain JSON inside a WZ_AES zip."""
    buf = io.BytesIO()
    with pyzipper.AESZipFile(
        buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("tenants.json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return buf.getvalue()


def _account(name: str) -> str:
    init_db()
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == name).one_or_none()
        if row is None:
            row = User(username=name, password_hash=hash_password(_PASSWORD))
            db.add(row)
        row.password_hash = hash_password(_PASSWORD)
        row.is_active = True
        row.totp_enabled = False
        db.commit()
        return row.id


def _seed_tenant(owner_id: str, name: str) -> None:
    with SessionLocal() as db:
        db.add(
            Tenant(
                owner_id=owner_id,
                name=name,
                user_ocid=_USER_OCID,
                tenancy_ocid=_TENANCY_OCID,
                fingerprint=_FINGERPRINT,
                region="ap-tokyo-1",
                private_key_encrypted=encrypt_text(REAL_PEM),
            )
        )
        db.commit()


def _client(username: str) -> TestClient:
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert r.status_code == 200, r.text
    return c


def _import(c: TestClient, blob: bytes, password: str):
    return c.post(
        "/api/backup/import",
        data={"password": password},
        files={"file": ("backup.zip", blob, "application/zip")},
    )


# --------------------------------------------------------------------------
# Password floor and the export/import mismatch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    [
        "abc123",          # the old floor — six characters
        "short1A!",        # eight, still nowhere near enough for a SHA1/1000 KDF
        "alllowercase",    # twelve but a single character class
        "aaaaaaaaaaaa1",   # twelve but almost no distinct characters
    ],
)
def test_export_refuses_a_password_that_cannot_protect_a_credential_file(password):
    owner = _account("bkenv-floor")
    _seed_tenant(owner, "bkenv-floor-tenant")
    with _client("bkenv-floor") as c:
        r = c.post("/api/backup/export", json={"password": password})
    assert r.status_code == 400, r.text


def test_export_and_import_agree_on_the_maximum_password_length():
    """A 600-character passphrase used to export fine and then be refused by the
    importer at validation — the tool made a file it would not open."""
    owner = _account("bkenv-maxlen")
    _seed_tenant(owner, "bkenv-maxlen-tenant")
    long_password = "Aa1!" * 150  # 600 characters
    with _client("bkenv-maxlen") as c:
        exported = c.post("/api/backup/export", json={"password": long_password})
        imported = _import(c, b"not-a-zip", long_password)
    assert exported.status_code == imported.status_code == 422


# --------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------


def test_the_zip_member_is_an_envelope_not_plaintext_tenant_json():
    owner = _account("bkenv-format")
    _seed_tenant(owner, "bkenv-format-tenant")
    with _client("bkenv-format") as c:
        r = c.post("/api/backup/export", json={"password": _GOOD_BACKUP_PASSWORD})
    assert r.status_code == 200, r.text

    with pyzipper.AESZipFile(io.BytesIO(r.content), "r") as zf:
        zf.setpassword(_GOOD_BACKUP_PASSWORD.encode("utf-8"))
        member = zf.read("tenants.json")
    envelope = json.loads(member.decode("utf-8"))
    # Whoever cracks the WinZip AES password still holds only ciphertext.
    assert envelope["version"] >= 2
    assert envelope["kdf"]["algorithm"] == "pbkdf2-hmac-sha256"
    assert envelope["kdf"]["iterations"] >= BACKUP_KDF_ITERATIONS
    assert b"PRIVATE KEY" not in member
    assert b"ocid1.user" not in member
    assert "private_key_pem" not in member.decode("utf-8")


def test_the_response_tells_the_ui_what_the_file_actually_is():
    owner = _account("bkenv-notice")
    _seed_tenant(owner, "bkenv-notice-tenant")
    with _client("bkenv-notice") as c:
        r = c.post("/api/backup/export", json={"password": _GOOD_BACKUP_PASSWORD})
    notice = json.loads(unquote(r.headers["X-OCIBot-Backup-Notice"]))
    assert notice["exported"] >= 1
    assert notice["skipped"] == []
    # 界面上必须说清楚这份文件等同于甲骨文账号本身，而不是「配置备份」。
    assert "私钥" in notice["warning"]


def test_round_trip_through_the_new_format():
    owner = _account("bkenv-round")
    _seed_tenant(owner, "bkenv-round-tenant")
    with _client("bkenv-round") as c:
        exported = c.post("/api/backup/export", json={"password": _GOOD_BACKUP_PASSWORD})
        assert exported.status_code == 200, exported.text
        restored = _import(c, exported.content, _GOOD_BACKUP_PASSWORD)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["imported"] >= 1
    assert body["skipped"] == 0


def test_the_wrong_password_does_not_restore_anything():
    owner = _account("bkenv-wrongpw")
    _seed_tenant(owner, "bkenv-wrongpw-tenant")
    with _client("bkenv-wrongpw") as c:
        exported = c.post("/api/backup/export", json={"password": _GOOD_BACKUP_PASSWORD})
        restored = _import(c, exported.content, "Wr0ng-backup-pass")
    assert restored.status_code == 400


# --------------------------------------------------------------------------
# Backward compatibility — the whole point of versioning the payload
# --------------------------------------------------------------------------


def test_an_archive_written_before_the_envelope_still_restores():
    """Every backup the operator already holds is in the old plain-JSON format."""
    _account("bkenv-legacy")
    blob = _legacy_zip(
        {"version": 1, "tenants": [_archive_item("bkenv-legacy-a"), _archive_item("bkenv-legacy-b")]},
        "old123",  # the old six-character floor
    )
    with _client("bkenv-legacy") as c:
        r = _import(c, blob, "old123")
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    assert r.json()["skipped"] == 0


def test_a_legacy_archive_that_is_a_bare_list_still_restores():
    """The oldest export shape of all: a top-level JSON list, no wrapper object."""
    _account("bkenv-legacylist")
    blob = _legacy_zip([_archive_item("bkenv-legacylist-a")], "old123")
    with _client("bkenv-legacylist") as c:
        r = _import(c, blob, "old123")
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1


# --------------------------------------------------------------------------
# Restore reporting
# --------------------------------------------------------------------------


def test_an_over_long_ocid_is_skipped_with_a_reason_not_stored_or_fatal():
    """user_ocid / tenancy_ocid / fingerprint / region / compartment_ocid went into
    the INSERT unbounded, against VARCHAR(128)/(64) columns that cfg.validate() does
    not check. SQLite overflows silently; PostgreSQL raises — and db.flush() sat in
    the loop unprotected, so one such row rolled back every tenant restored before
    it and answered a bare 500, where every other kind of bad row is merely skipped.
    """
    _account("bkenv-toolong")
    blob = _legacy_zip(
        {
            "version": 1,
            "tenants": [
                _archive_item("bkenv-toolong-ok1"),
                _archive_item("bkenv-toolong-bad", user_ocid="ocid1.user.oc1..a" + "z" * 300),
                _archive_item("bkenv-toolong-ok2"),
            ],
        },
        "old123",
    )
    with _client("bkenv-toolong") as c:
        r = _import(c, blob, "old123")
    assert r.status_code == 200, r.text
    body = r.json()
    # The rows either side of the bad one survive.
    assert body["imported"] == 2
    assert body["skipped"] == 1
    assert any("user_ocid" in reason for reason in body["skipped_reasons"])
    with SessionLocal() as db:
        assert db.query(Tenant).filter(Tenant.name == "bkenv-toolong-bad").count() == 0


def test_a_row_that_fails_validation_is_counted_and_explained():
    """"已导入 200 个租户" could be returned with none of them usable, because a row
    failing cfg.validate() was skipped with no record of any kind."""
    _account("bkenv-invalid")
    blob = _legacy_zip(
        {
            "version": 1,
            "tenants": [
                _archive_item("bkenv-invalid-ok"),
                _archive_item("bkenv-invalid-bad", user_ocid="not-an-ocid"),
            ],
        },
        "old123",
    )
    with _client("bkenv-invalid") as c:
        r = _import(c, blob, "old123")
    body = r.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert body["skipped_reasons"], "a skipped row with no reason is invisible to the operator"
    assert "跳过" in body["message"]


def test_a_garbage_private_key_in_an_archive_is_rejected_rather_than_restored():
    """Storing an unusable key is worse than skipping it: the tenant then reads as
    configured and every later page 502s with an opaque SDK error."""
    _account("bkenv-badkey")
    blob = _legacy_zip(
        {
            "version": 1,
            "tenants": [
                _archive_item(
                    "bkenv-badkey-bad",
                    private_key_pem=(
                        "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
                        "not-base64-at-all\n"
                        "-----END ENCRYPTED PRIVATE KEY-----\n"
                    ),
                ),
            ],
        },
        "old123",
    )
    with _client("bkenv-badkey") as c:
        r = _import(c, blob, "old123")
    body = r.json()
    assert body["imported"] == 0
    assert body["skipped"] == 1
    assert "私钥" in " ".join(body["skipped_reasons"])
