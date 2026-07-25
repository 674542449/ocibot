"""Unit tests for web.backend.quota_guard launch/shape enforcement."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import free_quota
from web.backend import quota_guard


class _FakeSession:
    def __init__(self, snapshot: dict | None = None, fail: bool = False):
        self._snapshot = snapshot or {
            "account_tier": "free",
            "usage": {
                "a1_ocpu": 4.0,
                "a1_memory_gb": 24.0,
                "e2_micro_count": 2,
                "block_storage_gb": 200.0,
            },
            "remaining": {
                "a1_ocpu": 0.0,
                "a1_memory_gb": 0.0,
                "e2_micro_count": 0,
                "block_storage_gb": 0.0,
            },
        }
        self._fail = fail

    def get_free_quota_usage(self, free_only_mode: bool = True):
        if self._fail:
            raise RuntimeError("oci down")
        return SimpleNamespace(ok=True, data=self._snapshot)


def test_enforce_launch_blocks_when_a1_exhausted():
    session = _FakeSession()
    with pytest.raises(HTTPException) as ei:
        quota_guard.enforce_launch_quota(
            session,
            account_tier="free",
            shape="VM.Standard.A1.Flex",
            ocpus=1,
            memory_in_gbs=6,
            boot_volume_size_in_gbs=50,
        )
    assert ei.value.status_code == 400
    assert "A1" in str(ei.value.detail) or "额度" in str(ei.value.detail)


def test_enforce_launch_allows_within_remaining():
    session = _FakeSession(
        {
            "account_tier": "free",
            "usage": {
                "a1_ocpu": 0.0,
                "a1_memory_gb": 0.0,
                "e2_micro_count": 0,
                "block_storage_gb": 50.0,
            },
            "remaining": {
                "a1_ocpu": 4.0,
                "a1_memory_gb": 24.0,
                "e2_micro_count": 2,
                "block_storage_gb": 150.0,
            },
        }
    )
    guard = quota_guard.enforce_launch_quota(
        session,
        account_tier="free",
        shape="VM.Standard.A1.Flex",
        ocpus=2,
        memory_in_gbs=12,
        boot_volume_size_in_gbs=50,
    )
    assert guard.ok


def test_enforce_launch_blocks_non_free_shape_in_free_only():
    session = _FakeSession(
        {
            "account_tier": "free",
            "usage": {
                "a1_ocpu": 0,
                "a1_memory_gb": 0,
                "e2_micro_count": 0,
                "block_storage_gb": 0,
            },
            "remaining": {
                "a1_ocpu": 4,
                "a1_memory_gb": 24,
                "e2_micro_count": 2,
                "block_storage_gb": 200,
            },
        }
    )
    with pytest.raises(HTTPException) as ei:
        quota_guard.enforce_launch_quota(
            session,
            account_tier="free",
            shape="VM.Standard.E4.Flex",
            ocpus=1,
            memory_in_gbs=16,
            boot_volume_size_in_gbs=50,
        )
    assert ei.value.status_code == 400


def test_enforce_shape_resize_blocks_over_cap():
    session = _FakeSession(
        {
            "account_tier": "free",
            "usage": {
                "a1_ocpu": 4.0,
                "a1_memory_gb": 24.0,
                "e2_micro_count": 0,
                "block_storage_gb": 50,
            },
            "remaining": {
                "a1_ocpu": 0,
                "a1_memory_gb": 0,
                "e2_micro_count": 2,
                "block_storage_gb": 150,
            },
        }
    )
    # Current instance uses 2/12; other instances already use the rest → bump to 4/24 of *this*
    # instance alone is still within free cap of the instance, but after = others(2)+new(4)=6 > 4.
    # usage a1=4, current=2 → others=2; new=4 → after=6 over.
    with pytest.raises(HTTPException):
        quota_guard.enforce_shape_resize_quota(
            session,
            account_tier="free",
            shape="VM.Standard.A1.Flex",
            current_ocpus=2,
            current_memory_in_gbs=12,
            new_ocpus=4,
            new_memory_in_gbs=24,
        )


def test_check_launch_quota_no_raise_on_block():
    session = _FakeSession()
    guard = quota_guard.check_launch_quota(
        session,
        account_tier="free",
        shape="VM.Standard.A1.Flex",
        ocpus=1,
        memory_in_gbs=6,
        boot_volume_size_in_gbs=50,
    )
    assert isinstance(guard, free_quota.GuardResult)
    assert not guard.ok


def test_free_only_for_tier():
    assert quota_guard.free_only_for_tier("free") is True
    assert quota_guard.free_only_for_tier("") is True
    assert quota_guard.free_only_for_tier("unknown") is True
    assert quota_guard.free_only_for_tier("paid") is False


def test_fallback_configs_also_validated():
    session = _FakeSession(
        {
            "account_tier": "free",
            "usage": {
                "a1_ocpu": 0,
                "a1_memory_gb": 0,
                "e2_micro_count": 0,
                "block_storage_gb": 0,
            },
            "remaining": {
                "a1_ocpu": 4,
                "a1_memory_gb": 24,
                "e2_micro_count": 2,
                "block_storage_gb": 200,
            },
        }
    )
    # Primary OK (2/12) but fallback 8/48 exceeds free cap
    with pytest.raises(HTTPException) as ei:
        quota_guard.enforce_launch_quota(
            session,
            account_tier="free",
            shape="VM.Standard.A1.Flex",
            ocpus=2,
            memory_in_gbs=12,
            boot_volume_size_in_gbs=50,
            fallback_configs=[{"ocpus": 8, "memory_in_gbs": 48}],
        )
    assert "降级" in str(ei.value.detail) or "免费" in str(ei.value.detail)
