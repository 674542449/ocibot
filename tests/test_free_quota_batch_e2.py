"""The free-tier caps apply to the whole batch, including E2.1.Micro.

``validate_launch_against_quota`` multiplies every resource by ``count`` into
``units`` and the A1 and block-storage branches both compare against that. The
E2.1.Micro branch did not — it asked only ``e2_rem < 1``, i.e. "is there at least
one slot free", never "are there n". With 1 of the 2 free micros already running,
``count=3`` therefore passed the guard and issued three LaunchInstance calls.

``tests/test_launch_count.py`` covers the batch arithmetic for A1 OCPUs, A1
memory and boot volume — E2.1.Micro was the one shape it never exercised, which
is why the suite stayed green.
"""

from __future__ import annotations

import pytest

from app.free_quota import FREE_E2_MICRO_COUNT, validate_launch_against_quota

MICRO = "VM.Standard.E2.1.Micro"


def _used(e2: int = 0, disk: float = 0.0) -> dict:
    return {
        "a1_ocpu": 0.0,
        "a1_memory_gb": 0.0,
        "e2_micro_count": e2,
        "block_storage_gb": disk,
    }


def _check(*, count: int, already: int, free_only: bool = True, tier: str = "free"):
    return validate_launch_against_quota(
        usage={"usage": _used(e2=already)},
        shape=MICRO,
        ocpus=1,
        memory_in_gbs=1,
        # Keep the 200 GB block-storage cap out of the way: it incidentally
        # blocked count>4 and was masking this bug.
        boot_volume_size_in_gbs=10,
        count=count,
        free_only_mode=free_only,
        account_tier=tier,
    )


def _codes(result) -> set[str]:
    return {i.code for i in getattr(result, "issues", [])}


def test_batch_exceeding_remaining_micros_is_blocked():
    """1 already running, 3 requested, cap 2 -> must be refused."""
    assert FREE_E2_MICRO_COUNT == 2
    result = _check(count=3, already=1)
    assert "e2_insufficient" in _codes(result), (
        "batch of 3 micros passed with only 1 free slot left — "
        f"issues={[i.code for i in result.issues]}"
    )
    assert not result.ok


@pytest.mark.parametrize("already,count", [(0, 3), (1, 2), (2, 1), (1, 3), (0, 8)])
def test_every_over_cap_combination_is_blocked(already: int, count: int):
    assert already + count > FREE_E2_MICRO_COUNT  # guard the test's own premise
    assert "e2_insufficient" in _codes(_check(count=count, already=already))


@pytest.mark.parametrize("already,count", [(0, 1), (0, 2), (1, 1)])
def test_batches_that_fit_are_still_allowed(already: int, count: int):
    """The fix must not over-block: anything within the cap still passes."""
    assert already + count <= FREE_E2_MICRO_COUNT
    assert "e2_insufficient" not in _codes(_check(count=count, already=already))


def test_over_cap_is_only_a_warning_when_free_caps_are_not_hard():
    """A paid tenant that opted out gets a billing warning, not a refusal."""
    result = _check(count=3, already=1, free_only=False, tier="paid")
    assert "e2_insufficient" not in _codes(result)
    assert "e2_insufficient" in {w.code for w in result.warnings}


def test_message_reports_needed_versus_remaining():
    """The operator needs to see both numbers to understand the refusal."""
    result = _check(count=3, already=1)
    msg = next(i.message for i in result.issues if i.code == "e2_insufficient")
    assert "3" in msg and "1" in msg, msg
