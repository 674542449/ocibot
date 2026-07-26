"""An unreadable Always-Free quota must block, not look like free headroom.

Every sub-read in get_free_quota_usage is individually try/excepted, so a
throttled or failing read produced a snapshot with zeroed usage and ok=True. The
validators then saw "nothing in use, full quota available" and allowed a launch
that could create billable overage — the exact opposite of what the guard exists
for.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from web.backend.quota_guard import (  # noqa: E402
    check_launch_quota,
    enforce_launch_quota,
    enforce_shape_resize_quota,
    free_only_for_tier,
    usage_snapshot,
)

_HEALTHY = {
    "account_tier": "free",
    "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0},
    "remaining": {
        "a1_ocpu": 4.0,
        "a1_memory_gb": 24.0,
        "e2_micro_count": 2,
        "block_storage_gb": 200.0,
    },
    "read_incomplete": False,
}


class _Result:
    def __init__(self, data: Any, ok: bool = True):
        self.ok = ok
        self.data = data
        self.message = ""


class _Session:
    """free_quota session stub with a configurable snapshot."""

    def __init__(self, snapshot: Any = None, raises: bool = False):
        self._snapshot = snapshot
        self._raises = raises
        self.calls = 0

    def get_free_quota_usage(self, free_only_mode: bool = True, **_kw):
        self.calls += 1
        if self._raises:
            raise RuntimeError("429 TooManyRequests")
        return _Result(self._snapshot)


_LAUNCH = dict(
    shape="VM.Standard.A1.Flex",
    ocpus=2,
    memory_in_gbs=12,
    boot_volume_size_in_gbs=50,
    boot_volume_vpus_per_gb=10,
)


def test_read_exception_is_flagged_incomplete():
    snap = usage_snapshot(_Session(raises=True), free_only_mode=True)
    assert snap.get("read_incomplete") is True


def test_empty_snapshot_is_flagged_incomplete():
    snap = usage_snapshot(_Session(snapshot={}), free_only_mode=True)
    assert snap.get("read_incomplete") is True


def test_healthy_snapshot_is_not_flagged():
    snap = usage_snapshot(_Session(snapshot=dict(_HEALTHY)), free_only_mode=True)
    assert not snap.get("read_incomplete")


def test_launch_blocked_when_quota_unreadable():
    with pytest.raises(HTTPException) as exc:
        enforce_launch_quota(_Session(raises=True), account_tier="free", **_LAUNCH)
    assert exc.value.status_code == 503
    assert "无法完整读取" in exc.value.detail


def test_launch_blocked_when_read_partial():
    partial = dict(_HEALTHY)
    partial["read_incomplete"] = True
    with pytest.raises(HTTPException) as exc:
        enforce_launch_quota(_Session(snapshot=partial), account_tier="free", **_LAUNCH)
    assert exc.value.status_code == 503


def test_launch_allowed_on_healthy_read():
    guard = enforce_launch_quota(_Session(snapshot=dict(_HEALTHY)), account_tier="free", **_LAUNCH)
    assert guard.ok is True


def test_paid_account_is_not_blocked_by_incomplete_read():
    """Paid accounts are not hard-capped, so an unreadable quota must not stop them."""
    partial = dict(_HEALTHY)
    partial["read_incomplete"] = True
    partial["account_tier"] = "paid"
    guard = enforce_launch_quota(_Session(snapshot=partial), account_tier="paid", **_LAUNCH)
    assert guard is not None


def test_shape_resize_blocked_when_quota_unreadable():
    with pytest.raises(HTTPException) as exc:
        enforce_shape_resize_quota(
            _Session(raises=True),
            account_tier="free",
            shape="VM.Standard.A1.Flex",
            current_ocpus=1,
            current_memory_in_gbs=6,
            new_ocpus=4,
            new_memory_in_gbs=24,
        )
    assert exc.value.status_code == 503


def test_snapshot_is_taken_once_for_primary_plus_fallbacks():
    """Each snapshot is a full tenancy enumeration; six of them per launch was waste."""
    session = _Session(snapshot=dict(_HEALTHY))
    enforce_launch_quota(
        session,
        account_tier="free",
        fallback_configs=[
            {"ocpus": 2, "memory_in_gbs": 12},
            {"ocpus": 1, "memory_in_gbs": 6},
            {"ocpus": 1, "memory_in_gbs": 4},
        ],
        **_LAUNCH,
    )
    assert session.calls == 1, f"took {session.calls} snapshots, expected 1"


def test_check_launch_quota_accepts_injected_usage():
    """The worker reuses its own snapshot rather than triggering another read."""
    session = _Session(snapshot=dict(_HEALTHY))
    guard = check_launch_quota(
        session, account_tier="free", usage=dict(_HEALTHY), **_LAUNCH
    )
    assert guard is not None
    assert session.calls == 0


def test_free_only_applies_to_everything_but_paid():
    assert free_only_for_tier("paid") is False
    assert free_only_for_tier("free") is True
    assert free_only_for_tier("") is True
