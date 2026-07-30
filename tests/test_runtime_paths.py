"""Tests for portable runtime paths and legacy-data migration."""

from __future__ import annotations

import json

import pytest

from app.config_store import ConfigStore, TenantConfig
from app.runtime_paths import (
    ensure_writable_directory,
    migrate_legacy_data,
    resolve_data_dir,
)
from app.scheduler import JobStore, ScheduleJob


KEY = "-----BEGIN PRIVATE KEY-----\nportable-test\n-----END PRIVATE KEY-----\n"


def _tenant() -> TenantConfig:
    return TenantConfig(
        id="portable-tenant",
        name="便携测试",
        user_ocid="ocid1.user.oc1.." + "a" * 60,
        tenancy_ocid="ocid1.tenancy.oc1.." + "b" * 60,
        fingerprint="12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
        region="ap-tokyo-1",
        private_key_pem=KEY,
    )


def test_source_mode_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("OCIBOT_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr("app.runtime_paths.is_frozen", lambda: False)
    assert resolve_data_dir() == tmp_path / "Roaming" / "ocibot"


def test_frozen_mode_uses_executable_sibling(monkeypatch, tmp_path):
    exe = tmp_path / "目录 有空格" / "OCIBot.exe"
    monkeypatch.delenv("OCIBOT_DATA_DIR", raising=False)
    monkeypatch.setattr("app.runtime_paths.is_frozen", lambda: True)
    monkeypatch.setattr("app.runtime_paths.sys.executable", str(exe))
    assert resolve_data_dir() == exe.parent / "data"


def test_environment_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / "自定义 data"
    monkeypatch.setenv("OCIBOT_DATA_DIR", str(custom))
    monkeypatch.setattr("app.runtime_paths.is_frozen", lambda: True)
    assert resolve_data_dir() == custom.resolve()


def test_migration_preserves_decryptable_tenants_and_jobs(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "portable app" / "data"
    store = ConfigStore(data_dir=source)
    store.upsert(_tenant())

    jobs = JobStore(data_dir=source)
    job = ScheduleJob(
        id="job-1",
        name="开机",
        tenant_id="portable-tenant",
        action="START",
        instance_ids=["instance-1"],
        time_of_day="08:30",
    )
    jobs.upsert_schedule(job)
    source_snapshot = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}

    result = migrate_legacy_data(destination, source=source)

    assert result.migrated is True
    assert set(result.files) >= {"tenants.json", "jobs.json", ".secret"}
    restored = ConfigStore(data_dir=destination, strict_load=True)
    assert restored.get("portable-tenant").private_key_pem == KEY
    assert JobStore(data_dir=destination).list_schedules()[0].id == "job-1"
    assert {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()} == source_snapshot


def test_migration_is_idempotent_and_never_overwrites(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "portable" / "data"
    source.mkdir()
    (source / "jobs.json").write_text('{"version": 2, "schedules": [], "retries": []}', encoding="utf-8")

    assert migrate_legacy_data(destination, source=source).migrated is True
    original = (destination / "jobs.json").read_text(encoding="utf-8")
    (source / "jobs.json").write_text('{"version": 2, "schedules": [{"id": "new"}], "retries": []}', encoding="utf-8")

    assert migrate_legacy_data(destination, source=source).migrated is False
    assert (destination / "jobs.json").read_text(encoding="utf-8") == original


def test_corrupt_migration_fails_closed_and_cleans_staging(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "portable" / "data"
    source.mkdir()
    (source / "tenants.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        migrate_legacy_data(destination, source=source)

    assert not destination.exists()
    assert not list(destination.parent.glob(".ocibot-migrate-*"))
    assert (source / "tenants.json").read_text(encoding="utf-8") == "not-json"


def test_symlink_source_is_rejected_when_supported(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "portable" / "data"
    source.mkdir()
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"version": 2, "schedules": [], "retries": []}), encoding="utf-8")
    link = source / "jobs.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("当前 Windows 权限不允许创建符号链接")

    with pytest.raises(ValueError, match="不安全"):
        migrate_legacy_data(destination, source=source)


def test_ensure_writable_directory_creates_target(tmp_path):
    target = tmp_path / "可写 路径" / "data"
    assert ensure_writable_directory(target) == target
    assert target.is_dir()
