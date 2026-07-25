"""Resolve writable runtime paths and migrate legacy application data."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

APP_DIR_NAME = "ocibot"
PORTABLE_DATA_DIR_NAME = "data"
DATA_FILES = ("tenants.json", "jobs.json", ".secret", ".salt")
DATA_DIR_ENV = "OCIBOT_DATA_DIR"


@dataclass(frozen=True)
class MigrationResult:
    migrated: bool = False
    source: Optional[Path] = None
    destination: Optional[Path] = None
    files: tuple[str, ...] = ()


def is_frozen() -> bool:
    """Return whether the process is running from a frozen executable."""
    return bool(getattr(sys, "frozen", False))


def legacy_data_dir() -> Path:
    """Return the historical per-user data directory without creating it."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base.expanduser() / APP_DIR_NAME


def application_dir() -> Path:
    """Return the directory containing the executable or source entry point."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_data_dir() -> Path:
    """Resolve the active data directory without creating it."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        return application_dir() / PORTABLE_DATA_DIR_NAME
    return legacy_data_dir()


def _validate_json_file(path: Path, expected_root: type) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取待迁移文件 {path.name}: {exc}") from exc
    if not isinstance(payload, expected_root):
        raise ValueError(f"待迁移文件 {path.name} 的格式无效")


def _default_validate_staging(staging: Path) -> None:
    """Validate copied files before they replace an empty portable directory."""
    tenants_path = staging / "tenants.json"
    if tenants_path.exists():
        _validate_json_file(tenants_path, dict)
        # Strict mode proves encrypted entries are readable with the copied key.
        from app.config_store import ConfigStore

        ConfigStore(data_dir=staging, strict_load=True)

    jobs_path = staging / "jobs.json"
    if jobs_path.exists():
        _validate_json_file(jobs_path, dict)
        from app.scheduler import JobStore

        JobStore(data_dir=staging)


def migrate_legacy_data(
    destination: Path,
    source: Optional[Path] = None,
    validator: Optional[Callable[[Path], None]] = None,
) -> MigrationResult:
    """Copy legacy data into an empty portable directory, preserving the source."""
    destination = Path(destination)
    source = Path(source) if source is not None else legacy_data_dir()

    if source.resolve() == destination.resolve() or not source.is_dir():
        return MigrationResult(destination=destination)

    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"数据路径不是文件夹: {destination}")
        if any(destination.iterdir()):
            return MigrationResult(destination=destination)

    selected: list[Path] = []
    for name in DATA_FILES:
        candidate = source / name
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"旧数据包含不安全的文件类型: {candidate}")
        selected.append(candidate)

    if not selected:
        return MigrationResult(destination=destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".ocibot-migrate-", dir=str(destination.parent)))
    installed = False
    try:
        for item in selected:
            shutil.copy2(item, staging / item.name)
        (validator or _default_validate_staging)(staging)

        if destination.exists():
            if any(destination.iterdir()):
                return MigrationResult(destination=destination)
            destination.rmdir()
        staging.replace(destination)
        installed = True
        return MigrationResult(
            migrated=True,
            source=source,
            destination=destination,
            files=tuple(item.name for item in selected),
        )
    finally:
        if not installed:
            shutil.rmtree(staging, ignore_errors=True)


def ensure_writable_directory(path: Path) -> Path:
    """Create a data directory and prove that the current user can write to it."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=str(path))
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise PermissionError(
            f"数据目录不可写: {path}\n请把 OCIBot 文件夹移动到桌面或其他可写位置。"
        ) from exc
    return path


def prepare_runtime_data() -> tuple[Path, MigrationResult]:
    """Resolve, migrate when frozen, and validate the active data directory."""
    data_dir = resolve_data_dir()
    result = MigrationResult(destination=data_dir)
    if is_frozen() and not os.environ.get(DATA_DIR_ENV, "").strip():
        result = migrate_legacy_data(data_dir)
    return ensure_writable_directory(data_dir), result
