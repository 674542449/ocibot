"""本月费用: calendar month-to-date, alongside the rolling "last N days" total.

The two answer different questions and only coincide by accident. Picking a
7-day window on the 20th and filtering those 7 days into a box labelled 本月
would understate the month by two thirds while looking perfectly plausible — so
the month figure is computed server-side from a query window that always reaches
the 1st, and the rolling-window aggregates keep their original meaning.

That widened query is also why `total` / `daily` / `by_service` are asserted here:
the risk of covering more ground is that the numbers the page already showed
quietly start including days outside the window the user asked for.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.oci_client import TenantSession  # noqa: E402


class _Item:
    def __init__(self, day: datetime, amount: float, service: str = "Compute"):
        self.time_usage_started = day
        self.computed_amount = amount
        self.service = service
        self.currency = "USD"


class _Resp:
    def __init__(self, items):
        self.data = types.SimpleNamespace(items=items)


def _session_with(items):
    """An TenantSession wired to a stub Usage API client and nothing else."""
    s = TenantSession.__new__(TenantSession)
    s.tenant = types.SimpleNamespace(tenancy_ocid="ocid1.tenancy.oc1..t")
    client = types.SimpleNamespace(request_summarized_usages=lambda details: _Resp(items))
    s._usage = client
    return s


def _utc_midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


# days=1 makes the window "today only", so the 1st of the month is outside it on
# every day except the 1st itself. Using a fixed 7 made the whole assertion skip
# for the first nine days of every month — including the day it was written,
# which is not much of a test.
_first_of_month_note = "on the 1st the month and a 1-day window are the same range"


def test_month_to_date_covers_the_month_even_with_a_short_window(now):
    """The whole point: a short window must not truncate the month figure."""
    if now.day == 1:
        pytest.skip(_first_of_month_note)
    month_start = _utc_midnight(now.replace(day=1))

    # One charge on the 1st (inside the month, outside a 1-day window), one today.
    items = [_Item(month_start, 5.0), _Item(_utc_midnight(now), 2.0)]
    res = _session_with(items).get_usage_summary(days=1)

    assert res.ok
    assert res.data["month_to_date"] == pytest.approx(7.0), "month must include the 1st"
    assert res.data["total"] == pytest.approx(2.0), "rolling total must exclude it"


def test_window_aggregates_ignore_days_outside_the_window(now):
    """The widened query must not leak into total / daily / by_service."""
    if now.day == 1:
        pytest.skip(_first_of_month_note)
    month_start = _utc_midnight(now.replace(day=1))

    items = [_Item(month_start, 9.0, "Block Storage"), _Item(_utc_midnight(now), 1.0, "Compute")]
    res = _session_with(items).get_usage_summary(days=1)

    assert res.data["total"] == pytest.approx(1.0)
    assert [d["date"] for d in res.data["daily"]] == [_utc_midnight(now).date().isoformat()]
    services = {s["service"] for s in res.data["by_service"]}
    assert services == {"Compute"}, f"by_service leaked outside the window: {services}"
    assert res.data["month_to_date"] == pytest.approx(10.0)


def test_long_window_reaching_past_the_month_does_not_inflate_the_month(now):
    """days=90 reaches into previous months; those must not count as 本月."""
    month_start = _utc_midnight(now.replace(day=1))
    last_month = month_start - timedelta(days=3)
    items = [_Item(last_month, 100.0), _Item(month_start, 4.0)]
    res = _session_with(items).get_usage_summary(days=90)

    assert res.data["month_to_date"] == pytest.approx(4.0), "previous month must be excluded"
    assert res.data["total"] == pytest.approx(104.0), "the 90-day total still includes it"


def test_month_start_is_reported_so_the_ui_can_label_the_period(now):
    res = _session_with([_Item(_utc_midnight(now), 1.0)]).get_usage_summary(days=30)
    assert res.data["month_start"] == _utc_midnight(now.replace(day=1)).date().isoformat()


def test_undated_rows_are_not_counted_into_the_month(now):
    """Items without a timestamp bucket as "unknown", which sorts above every real
    ISO date — a plain string comparison would silently count them as this month."""
    undated = _Item(_utc_midnight(now), 3.0)
    undated.time_usage_started = None
    undated.time_usage_ended = None
    res = _session_with([undated]).get_usage_summary(days=30)
    assert res.data["month_to_date"] == pytest.approx(0.0)


def test_failed_read_reports_none_not_zero():
    """0.00 and "could not read" must not look the same for a cost figure."""
    s = TenantSession.__new__(TenantSession)
    s.tenant = types.SimpleNamespace(tenancy_ocid="ocid1.tenancy.oc1..t")
    s._usage = None
    res = s.get_usage_summary(days=30)
    assert res.ok is False
    assert res.data["month_to_date"] is None


def test_exception_path_also_reports_none():
    def boom(details):
        raise RuntimeError("usage api exploded")

    s = TenantSession.__new__(TenantSession)
    s.tenant = types.SimpleNamespace(tenancy_ocid="ocid1.tenancy.oc1..t")
    s._usage = types.SimpleNamespace(request_summarized_usages=boom)
    res = s.get_usage_summary(days=30)
    assert res.ok is False
    assert res.data["month_to_date"] is None


def test_still_a_single_oci_call(now):
    """Month-to-date must not cost a second request: that budget competes with
    the capacity retry loop for the same rate limit."""
    calls: list[object] = []

    def record(details):
        calls.append(details)
        return _Resp([_Item(_utc_midnight(now), 1.0)])

    s = TenantSession.__new__(TenantSession)
    s.tenant = types.SimpleNamespace(tenancy_ocid="ocid1.tenancy.oc1..t")
    s._usage = types.SimpleNamespace(request_summarized_usages=record)
    s.get_usage_summary(days=7)
    assert len(calls) == 1
