"""Tests for the local Oracle password-expiry reminder on TenantConfig."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.config_store import TenantConfig


def _tenant(**kw) -> TenantConfig:
    base = dict(
        id=str(uuid.uuid4()),
        name="t",
        user_ocid="ocid1.user.oc1..x",
        tenancy_ocid="ocid1.tenancy.oc1..x",
        fingerprint="fp",
        region="ap-tokyo-1",
        private_key_pem="",
    )
    base.update(kw)
    return TenantConfig(**base)


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def test_default_expiry_is_120_days():
    t = _tenant(password_changed_at=_days_ago(0))
    assert t.password_expiry_days == 120
    left = t.password_days_left()
    assert left is not None and 119 <= left <= 120


def test_expired_password():
    t = _tenant(password_changed_at=_days_ago(130), password_expiry_days=120)
    left = t.password_days_left()
    assert left is not None and left < 0
    level, _text = t.password_status()
    assert level == "expired"


def test_warn_window():
    t = _tenant(password_changed_at=_days_ago(110), password_expiry_days=120)
    level, _text = t.password_status()
    assert level == "warn"  # 10 days left, within the 14-day warn window


def test_ok_when_far_from_expiry():
    t = _tenant(password_changed_at=_days_ago(10), password_expiry_days=120)
    assert t.password_status()[0] == "ok"


def test_zero_days_disables_reminder():
    t = _tenant(password_changed_at=_days_ago(500), password_expiry_days=0)
    assert t.password_days_left() is None
    assert t.password_status()[0] == "off"


def test_custom_expiry_period():
    t = _tenant(password_changed_at=_days_ago(40), password_expiry_days=30)
    assert t.password_status()[0] == "expired"  # 40 > 30


def test_falls_back_to_created_at_when_no_change_recorded():
    t = _tenant(created_at=_days_ago(200) + "T00:00:00+00:00", password_expiry_days=120)
    # No password_changed_at -> baseline is created_at (200 days ago) -> expired.
    assert t.password_status()[0] == "expired"


def test_new_fields_survive_serialization_round_trip():
    t = _tenant(password_changed_at="2026-01-01", password_expiry_days=90)
    restored = TenantConfig.from_dict(t.to_storage_dict())
    assert restored.password_changed_at == "2026-01-01"
    assert restored.password_expiry_days == 90
