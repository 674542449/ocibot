"""Tests for encrypted-ZIP backup / restore of tenant API keys."""

from __future__ import annotations

import uuid

import pytest

from app.config_store import ConfigStore, TenantConfig

from tests._keys import TEST_PEM

VALID_KEY = TEST_PEM


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        id=str(uuid.uuid4()),
        name=name,
        user_ocid="ocid1.user.oc1..aaaaaaaa" + "b" * 40,
        tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaa" + "c" * 44,
        fingerprint="12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
        region="ap-tokyo-1",
        private_key_pem=VALID_KEY,
    )


def test_backup_and_restore_round_trip(tmp_path):
    src = ConfigStore(data_dir=tmp_path / "src")
    src.upsert(_make_tenant("公司A-东京"))
    src.upsert(_make_tenant("公司B-大阪"))

    archive = tmp_path / "backup.zip"
    count = src.backup_to_encrypted_zip(archive, "s3cret-pass")
    assert count == 2
    assert archive.exists()

    # The archive must not expose the private key in plaintext.
    assert b"BEGIN PRIVATE KEY" not in archive.read_bytes()

    dst = ConfigStore(data_dir=tmp_path / "dst")
    restored = dst.restore_from_encrypted_zip(archive, "s3cret-pass")
    assert len(restored) == 2
    names = {t.name for t in dst.list_tenants()}
    assert names == {"公司A-东京", "公司B-大阪"}
    # Private key survives the round trip.
    assert all(t.private_key_pem.strip() for t in dst.list_tenants())


def test_restore_with_wrong_password_raises(tmp_path):
    src = ConfigStore(data_dir=tmp_path / "src")
    src.upsert(_make_tenant("t1"))
    archive = tmp_path / "backup.zip"
    src.backup_to_encrypted_zip(archive, "correct-pass")

    dst = ConfigStore(data_dir=tmp_path / "dst")
    with pytest.raises(ValueError):
        dst.restore_from_encrypted_zip(archive, "wrong-pass")


def test_backup_rejects_short_password(tmp_path):
    src = ConfigStore(data_dir=tmp_path / "src")
    src.upsert(_make_tenant("t1"))
    with pytest.raises(ValueError):
        src.backup_to_encrypted_zip(tmp_path / "b.zip", "123")


def test_backup_rejects_empty_store(tmp_path):
    src = ConfigStore(data_dir=tmp_path / "src")
    with pytest.raises(ValueError):
        src.backup_to_encrypted_zip(tmp_path / "b.zip", "longenough")
