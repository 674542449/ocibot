"""The panel version must be bumped with every shipped change.

/api/health's `version` is how an operator confirms an update actually landed (it is
the first thing README's troubleshooting table tells you to check). Ten commits once
shipped under one unchanged version, which made that check useless.

This test makes forgetting it fail the suite instead of being noticed in production:
Settings.app_version must equal the newest version heading in CHANGELOG.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("pydantic_settings")

from web.backend.config import Settings  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b", re.MULTILINE)


def _changelog_versions() -> list[str]:
    assert _CHANGELOG.is_file(), f"missing {_CHANGELOG}"
    return _HEADING.findall(_CHANGELOG.read_text(encoding="utf-8"))


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_app_version_matches_the_newest_changelog_entry():
    versions = _changelog_versions()
    assert versions, "CHANGELOG.md has no '## X.Y.Z' headings"
    newest = versions[0]
    current = Settings().app_version
    assert current == newest, (
        f"app_version is {current} but the newest CHANGELOG entry is {newest}.\n"
        "Every shipped change must bump web/backend/config.py::app_version AND add a "
        "matching CHANGELOG.md section — /api/health is how a deploy is verified."
    )


def test_changelog_versions_are_in_descending_order():
    """A newest-first list is what the previous test relies on."""
    versions = _changelog_versions()
    parsed = [_as_tuple(v) for v in versions]
    assert parsed == sorted(parsed, reverse=True), (
        "CHANGELOG.md versions are not newest-first: " + ", ".join(versions)
    )


def test_changelog_has_no_duplicate_versions():
    versions = _changelog_versions()
    duplicates = {v for v in versions if versions.count(v) > 1}
    assert not duplicates, f"duplicate CHANGELOG versions: {sorted(duplicates)}"


def test_health_endpoint_reports_that_version():
    """The whole point: the running app serves the bumped number."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from web.backend.main import app

    with TestClient(app) as client:
        payload = client.get("/api/health").json()
    assert payload["version"] == Settings().app_version
