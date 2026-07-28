"""Outbound data transfer (10 TB/month Always Free) tracking.

This is the one free allowance the quota guard never watched, and a realistic way
to be billed by surprise. It is tracked for visibility and a monthly alert only —
egress is not knowable at create time, so it must never block a launch, and the
figure is an upper bound rather than a bill (see TenantSession.get_network_egress_usage).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import free_quota
from app.oci_client import TenantSession


def _dp(value):
    return SimpleNamespace(timestamp=datetime.now(timezone.utc), value=value)


def _session(series, *, raises=None):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        region="ap-tokyo-1", compartment_ocid="", tenancy_ocid="ocid1.tenancy..t", name="T"
    )

    def _summarize(compartment_id, details, **kwargs):
        if raises is not None:
            raise raises
        _summarize.seen = {"compartment": compartment_id, "details": details, "kwargs": kwargs}
        return SimpleNamespace(data=series)

    s._monitoring = SimpleNamespace(summarize_metrics_data=_summarize)
    s._summarize = _summarize
    return s


# ------------------------------------------------------------------ OCI query


def test_hourly_buckets_are_summed_into_gb():
    gib = 1024**3
    s = _session(
        [
            SimpleNamespace(aggregated_datapoints=[_dp(gib), _dp(gib)]),
            # A second VNIC's series must add to the same total, not replace it.
            SimpleNamespace(aggregated_datapoints=[_dp(2 * gib)]),
        ]
    )
    result = s.get_network_egress_usage()
    assert result.ok
    assert result.data["egress_gb"] == 4.0
    assert result.data["approximate"] is True
    assert result.data["region"] == "ap-tokyo-1"


def test_query_covers_the_calendar_month_and_subtree():
    s = _session([])
    s.get_network_egress_usage()
    seen = s._summarize.seen
    details = seen["details"]
    assert details.namespace == "oci_vcn"
    # A per-interval byte count, so .sum() — not the .rate() the cumulative
    # oci_computeagent counters need.
    assert details.query == "VnicToNetworkBytes[1h].sum()"
    assert details.start_time.day == 1
    assert seen["kwargs"].get("compartment_id_in_subtree") is True
    # No compartment on the tenant -> falls back to the tenancy root.
    assert seen["compartment"] == "ocid1.tenancy..t"


def test_garbage_datapoints_are_skipped():
    s = _session(
        [
            SimpleNamespace(
                aggregated_datapoints=[
                    _dp(float("nan")),
                    _dp(-5.0),  # counter reset
                    _dp("not a number"),
                    _dp(1024**3),
                ]
            )
        ]
    )
    assert s.get_network_egress_usage().data["egress_gb"] == 1.0


def test_read_failure_is_reported_not_raised():
    s = _session([], raises=RuntimeError("monitoring unreachable"))
    result = s.get_network_egress_usage()
    assert result.ok is False
    assert "monitoring unreachable" in result.message


# ------------------------------------------------------------------- snapshot


def _snapshot(egress=None):
    return free_quota.build_quota_snapshot(
        instances=[], volumes=[], egress_usage=egress
    )


def test_bucket_is_absent_when_egress_was_not_requested():
    """Present-and-zero would read as "10 TB still free" on a page that never
    checked. The launch guard omits egress, so the bucket must simply not exist."""
    snap = _snapshot()
    assert "egress_gb" not in snap["buckets"]
    assert "egress_gb" not in snap["limits"]
    assert "egress_gb" not in snap["usage"]
    assert not any("出网流量" in line for line in snap["summary_lines"])


def test_bucket_appears_with_the_free_allowance_when_requested():
    snap = _snapshot({"egress_gb": 2048.0, "region": "ap-tokyo-1"})
    bucket = snap["buckets"]["egress_gb"]
    assert bucket["used"] == 2048.0
    assert bucket["limit"] == free_quota.FREE_EGRESS_GB
    assert snap["usage"]["egress_gb"] == 2048.0
    assert snap["usage"]["egress_region"] == "ap-tokyo-1"
    assert snap["remaining"]["egress_gb"] == free_quota.FREE_EGRESS_GB - 2048.0
    assert any("出网流量" in line for line in snap["summary_lines"])


def test_egress_overage_never_blocks_the_overall_status():
    """It is an upper bound over one region — treating it as a hard cap would make
    an unrelated launch look blocked. Soft, like the public-IP bucket."""
    snap = _snapshot({"egress_gb": free_quota.FREE_EGRESS_GB * 2})
    assert snap["buckets"]["egress_gb"]["soft"] is True
    assert snap["overall_status"] == "ok"


def test_launch_validation_ignores_egress_entirely():
    """Egress is not knowable at create time; the guard must not read it."""
    guard = free_quota.validate_launch_against_quota(
        shape="VM.Standard.A1.Flex",
        ocpus=4,
        memory_in_gbs=24,
        boot_volume_size_in_gbs=50,
        free_only_mode=True,
        usage=_snapshot({"egress_gb": free_quota.FREE_EGRESS_GB * 5}),
    )
    assert guard.ok is True, guard.error_messages()


@pytest.mark.parametrize(
    "used,should_alert",
    [
        (0.0, False),
        (free_quota.FREE_EGRESS_GB * 0.79, False),
        (free_quota.FREE_EGRESS_GB * 0.8, True),
        (free_quota.FREE_EGRESS_GB * 1.5, True),
    ],
)
def test_alert_threshold_leaves_room_to_react(used, should_alert):
    """Alerting only on overage would be useless — by then the bill exists."""
    assert (used >= free_quota.FREE_EGRESS_GB * free_quota.EGRESS_ALERT_RATIO) is should_alert
