"""A failed quota read must never be MORE permissive than a successful one.

The two halves of the guard disagreed about who is hard-capped:
``quota_guard._blocked_by_incomplete_read`` refused only when ``free_only_mode``
was on, while ``free_quota.validate_launch_against_quota`` hard-caps on
``free_only or tier in {"", "free", "unknown"}``.

So for a tenant with ``account_tier="free"`` who unticked 「仅使用免费额度」:
  * quota read OK   -> ok=False, "A1 额度不足"  -> HTTP 400
  * quota read 429  -> snapshot degrades to {"read_incomplete": True}, the still
                       hard caps compare against zero usage, everything passes
                       -> HTTP 200 and a real LaunchInstance.

These tests pin both halves to the one shared predicate, ``free_quota.hard_free_caps``.
Unlike test_quota_fail_closed.py they do not stub the guard itself — only the OCI
read is faked, exactly as a throttled tenancy would behave.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from app import free_quota  # noqa: E402
from web.backend import quota_guard  # noqa: E402

A1 = "VM.Standard.A1.Flex"

# Tenancy already running the whole Always-Free A1 allowance.
_EXHAUSTED = {
    "account_tier": "free",
    "usage": {
        "a1_ocpu": 4.0,
        "a1_memory_gb": 24.0,
        "e2_micro_count": 0,
        "block_storage_gb": 47.0,
    },
    "remaining": {
        "a1_ocpu": 0.0,
        "a1_memory_gb": 0.0,
        "e2_micro_count": 2,
        "block_storage_gb": 153.0,
    },
    "read_incomplete": False,
}

# Another full 4 OCPU / 24 GB machine on top of that.
_SECOND_A1 = dict(
    shape=A1,
    ocpus=4,
    memory_in_gbs=24,
    boot_volume_size_in_gbs=47,
    boot_volume_vpus_per_gb=10,
)


class _Result:
    def __init__(self, data: Any):
        self.ok = True
        self.data = data
        self.message = ""


class _Session:
    """OCI session stub whose free-quota read can be made to throttle."""

    def __init__(self, snapshot: Any = None, raises: bool = False):
        self._snapshot = snapshot
        self._raises = raises

    def get_free_quota_usage(self, free_only_mode: bool = True, **_kw):
        if self._raises:
            raise RuntimeError("429 TooManyRequests")
        return _Result(self._snapshot)


def test_throttled_read_is_not_more_permissive_than_healthy_read():
    """Same tenant, same request — the failing read must not be the lenient one."""
    kw = dict(account_tier="free", free_only_mode=False, **_SECOND_A1)

    with pytest.raises(HTTPException) as healthy:
        quota_guard.enforce_launch_quota(_Session(snapshot=dict(_EXHAUSTED)), **kw)
    assert healthy.value.status_code == 400
    assert "A1" in str(healthy.value.detail)

    with pytest.raises(HTTPException) as throttled:
        quota_guard.enforce_launch_quota(_Session(raises=True), **kw)
    assert throttled.value.status_code == 503
    assert "无法完整读取" in str(throttled.value.detail)


def test_partial_snapshot_blocks_free_tier_that_opted_out():
    """read_incomplete on a flagged snapshot (not just an exception) blocks too."""
    partial = dict(_EXHAUSTED)
    partial["read_incomplete"] = True
    with pytest.raises(HTTPException) as exc:
        quota_guard.enforce_launch_quota(
            _Session(snapshot=partial),
            account_tier="free",
            free_only_mode=False,
            **_SECOND_A1,
        )
    assert exc.value.status_code == 503


def test_paid_opt_out_is_still_allowed_on_incomplete_read():
    """The one account that really is not hard-capped keeps its overage path."""
    guard = quota_guard.enforce_launch_quota(
        _Session(raises=True),
        account_tier="paid",
        free_only_mode=False,
        **_SECOND_A1,
    )
    assert guard.ok is True


def test_shape_resize_blocks_free_tier_opt_out_on_unreadable_quota():
    with pytest.raises(HTTPException) as exc:
        quota_guard.enforce_shape_resize_quota(
            _Session(raises=True),
            account_tier="free",
            free_only_mode=False,
            shape=A1,
            current_ocpus=1,
            current_memory_in_gbs=6,
            new_ocpus=4,
            new_memory_in_gbs=24,
        )
    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    "free_only,tier",
    [
        (True, "free"),
        (True, "paid"),
        (True, ""),
        (False, "free"),
        (False, ""),
        (False, "unknown"),
        (False, "FREE"),
        (False, "paid"),
    ],
)
def test_incomplete_read_block_tracks_the_validator_exactly(free_only: bool, tier: str):
    """The refuse-on-partial-read rule and the cap rule must be one predicate.

    Both sides are asserted against free_quota.hard_free_caps, so re-splitting them
    into two copies of `free_only or tier in {...}` fails here rather than in
    production.
    """
    hard = free_quota.hard_free_caps(free_only, tier)

    verdict = free_quota.validate_launch_against_quota(
        free_only_mode=free_only,
        account_tier=tier,
        usage=dict(_EXHAUSTED),
        **_SECOND_A1,
    )
    assert (not verdict.ok) is hard, "validator hard-caps disagree with the predicate"

    blocked = quota_guard._blocked_by_incomplete_read(
        {"read_incomplete": True}, free_only, tier
    )
    assert bool(blocked) is hard, "incomplete-read refusal disagrees with the predicate"


def test_healthy_read_is_never_blocked_as_incomplete():
    assert quota_guard._blocked_by_incomplete_read(dict(_EXHAUSTED), True, "free") is None


def test_missing_tier_keeps_the_old_free_only_behaviour():
    """Callers that cannot supply a tier (storage.py, instance_ops.py) are unchanged.

    They pass two positional args today; defaulting the tier to "" would newly 503
    paid tenants on a throttled read. None means "tier unknown" instead, so the fix
    can reach those call sites as a separate one-line change.
    """
    partial = {"read_incomplete": True}
    assert quota_guard._blocked_by_incomplete_read(partial, False) is None
    assert quota_guard._blocked_by_incomplete_read(partial, True) is not None
