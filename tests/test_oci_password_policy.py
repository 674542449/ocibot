"""Unit tests for Identity Domain password-policy helpers (no live OCI)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("oci")

from app.oci_client import TenantSession  # noqa: E402


class _FakeSession(TenantSession):
    """Bypass TenantSession.__init__ / OCI client construction."""

    def __init__(self) -> None:  # noqa: D107
        pass


def test_password_policy_to_dict():
    pol = SimpleNamespace(
        id="pp1",
        ocid="ocid1.passwordpolicy...",
        name="Default",
        description="d",
        password_expires_after=120,
        password_expire_warning=14,
        priority=1,
    )
    d = TenantSession._password_policy_to_dict(pol)
    assert d["id"] == "pp1"
    assert d["name"] == "Default"
    assert d["password_expires_after"] == 120
    assert d["password_expire_warning"] == 14


def test_password_policy_to_dict_never_expire():
    pol = SimpleNamespace(
        id="pp2",
        ocid="",
        name="Custom",
        description="",
        password_expires_after=None,
        password_expire_warning=None,
        priority=None,
    )
    d = TenantSession._password_policy_to_dict(pol)
    assert d["password_expires_after"] is None


def test_disable_skips_already_never_expire():
    sess = _FakeSession()
    sess.list_console_password_policies = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            ok=True,
            message="ok",
            data={
                "policies": [
                    {
                        "id": "p1",
                        "name": "Default",
                        "domain_name": "Default",
                        "domain_url": "https://idcs.example.com",
                        "password_expires_after": None,
                    }
                ]
            },
        )
    )
    result = sess.disable_console_password_expiry()
    assert result.ok is True
    assert "不强制过期" in result.message
    assert result.data["skipped"]


def test_disable_patches_when_expires_after_set():
    sess = _FakeSession()
    sess.list_console_password_policies = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            ok=True,
            message="ok",
            data={
                "policies": [
                    {
                        "id": "p1",
                        "name": "Default",
                        "domain_name": "Default",
                        "domain_url": "https://idcs.example.com",
                        "password_expires_after": 120,
                    }
                ]
            },
        )
    )
    sess._identity_domains_client = MagicMock(return_value=object())  # type: ignore[method-assign]
    sess._patch_password_policy_never_expire = MagicMock(return_value=None)  # type: ignore[method-assign]
    result = sess.disable_console_password_expiry()
    assert result.ok is True
    assert result.data["updated"]
    assert result.data["updated"][0]["password_expires_after_before"] == 120
    sess._patch_password_policy_never_expire.assert_called_once()


def test_disable_propagates_list_failure():
    sess = _FakeSession()
    sess.list_console_password_policies = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(ok=False, message="no domain", data={})
    )
    result = sess.disable_console_password_expiry()
    assert result.ok is False
    assert "no domain" in result.message


def test_patch_uses_uppercase_op_enums():
    """Identity Domains rejects lowercase op; must be ADD/REMOVE/REPLACE."""
    from oci.identity_domains.models import Operations

    sess = _FakeSession()
    client = MagicMock()
    client.patch_password_policy.return_value = SimpleNamespace(
        data=SimpleNamespace(password_expires_after=None)
    )
    after = sess._patch_password_policy_never_expire(client, "pp1")
    assert after is None
    assert client.patch_password_policy.call_count == 1
    kwargs = client.patch_password_policy.call_args.kwargs
    patch = kwargs["patch_op"]
    ops = list(patch.operations or [])
    assert ops
    for op in ops:
        assert op.op == Operations.OP_REMOVE
        assert op.op == "REMOVE"
        assert op.path in {"passwordExpiresAfter", "passwordExpireWarning"}
