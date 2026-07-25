"""Unit tests for web tenant password-policy helpers and schemas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from web.backend.models import Tenant  # noqa: E402
from web.backend.routers.tenants import (  # noqa: E402
    _normalize_password_changed_at,
    _password_policy_fields,
)
from web.backend.schemas import PasswordPolicyUpdate, TenantOut  # noqa: E402


def _row(**kw) -> Tenant:
    base = dict(
        id="tid",
        owner_id="uid",
        name="t",
        user_ocid="ocid1.user.oc1..x",
        tenancy_ocid="ocid1.tenancy.oc1..x",
        fingerprint="fp",
        region="ap-tokyo-1",
        private_key_encrypted="enc",
        password_changed_at="",
        password_expiry_days=120,
        created_at=datetime.now(timezone.utc) - timedelta(days=10),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return Tenant(**base)


def test_normalize_password_changed_at():
    assert _normalize_password_changed_at(None) == ""
    assert _normalize_password_changed_at("") == ""
    assert _normalize_password_changed_at(" 2026-03-15 ") == "2026-03-15"
    assert _normalize_password_changed_at("2026-03-15T12:00:00Z") == "2026-03-15"
    with pytest.raises(Exception) as ei:
        _normalize_password_changed_at("not-a-date")
    assert getattr(ei.value, "status_code", None) == 400


def test_policy_fields_expired():
    changed = (datetime.now(timezone.utc) - timedelta(days=130)).date().isoformat()
    row = _row(password_changed_at=changed, password_expiry_days=120)
    fields = _password_policy_fields(row)
    assert fields["password_status"] == "expired"
    assert fields["password_days_left"] is not None and fields["password_days_left"] < 0
    assert fields["password_expires_on"]  # real YYYY-MM-DD
    assert fields["password_changed_at"] == changed


def test_policy_fields_ok_with_real_expiry_date():
    changed = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    row = _row(password_changed_at=changed, password_expiry_days=120)
    fields = _password_policy_fields(row)
    assert fields["password_status"] == "ok"
    assert fields["password_days_left"] is not None and 100 <= fields["password_days_left"] <= 110
    # expiry = changed + 120 days
    expected = (
        datetime.strptime(changed, "%Y-%m-%d").date() + timedelta(days=120)
    ).isoformat()
    assert fields["password_expires_on"] == expected


def test_policy_fields_off_when_days_zero():
    row = _row(password_changed_at="2026-01-01", password_expiry_days=0)
    fields = _password_policy_fields(row)
    assert fields["password_status"] == "off"
    assert fields["password_days_left"] is None
    assert fields["password_expires_on"] == ""


def test_password_policy_update_schema_bounds():
    PasswordPolicyUpdate(password_expiry_days=0)
    PasswordPolicyUpdate(password_expiry_days=3650)
    PasswordPolicyUpdate(mark_changed_today=True)
    with pytest.raises(Exception):
        PasswordPolicyUpdate(password_expiry_days=-1)
    with pytest.raises(Exception):
        PasswordPolicyUpdate(password_expiry_days=3651)


def test_tenant_out_includes_computed_fields():
    changed = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
    row = _row(password_changed_at=changed, password_expiry_days=90)
    fields = _password_policy_fields(row)
    out = TenantOut(
        id=row.id,
        name=row.name,
        user_ocid=row.user_ocid,
        tenancy_ocid=row.tenancy_ocid,
        fingerprint=row.fingerprint,
        region=row.region,
        compartment_ocid="",
        description="",
        enabled=True,
        color="#3B82F6",
        has_private_key=True,
        password_changed_at=fields["password_changed_at"],
        password_expiry_days=fields["password_expiry_days"],
        password_expires_on=fields["password_expires_on"],
        password_days_left=fields["password_days_left"],
        password_status=fields["password_status"],
        account_tier="",
        budget_monthly_usd=0.0,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    assert out.password_expires_on
    assert out.password_status == "ok"
    assert out.password_days_left is not None
