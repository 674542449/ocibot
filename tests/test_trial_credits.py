"""试用积分：注册日期、总额、已用、剩余、到期时间。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import oci.tenant_manager_control_plane as tmcp

from app.oci_client import TenantSession

NOW = datetime.now(timezone.utc)


def _session(monkeypatch, *, promotion, usage_items=None, usage_error=None, sub_error=None):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(tenancy_ocid="ocid1.tenancy..t", region="ap-singapore-1", name="T")
    s._config = {"user": "u", "fingerprint": "f", "tenancy": "ocid1.tenancy..t",
                 "region": "ap-singapore-1", "key_file": "k"}
    s._identity = SimpleNamespace(
        get_compartment=lambda cid: SimpleNamespace(
            data=SimpleNamespace(time_created=datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        ),
        list_region_subscriptions=lambda t: SimpleNamespace(
            data=[SimpleNamespace(region_name="ap-singapore-1", is_home_region=True)]
        ),
    )
    asked: list = []

    def request_summarized_usages(details, **_kw):
        asked.append((details.time_usage_started, details.time_usage_ended))
        if usage_error:
            raise RuntimeError(usage_error)
        return SimpleNamespace(data=SimpleNamespace(items=usage_items or []), next_page=None, headers={})

    s._usage = SimpleNamespace(request_summarized_usages=request_summarized_usages)

    class FakeSubscription:
        def __init__(self, cfg, **_kw):
            pass

        def list_subscriptions(self, compartment_id=None, entity_version=None):
            if sub_error:
                raise RuntimeError(sub_error)
            return SimpleNamespace(data=SimpleNamespace(items=[SimpleNamespace(id="sub1", payment_model="")]))

        def get_subscription(self, subscription_id=None, entity_version=None):
            return SimpleNamespace(data=SimpleNamespace(
                promotion=promotion, start_date=datetime(2026, 9, 21, tzinfo=timezone.utc),
                subscription_tier="FREE_TIER",
            ))

    monkeypatch.setattr(tmcp, "SubscriptionClient", FakeSubscription)
    return s, asked


def _promo(status="ACTIVE", amount=300.0, started=None, expired=None):
    return [SimpleNamespace(
        amount=amount, currency_unit="USD", status=status,
        time_started=started or (NOW - timedelta(days=5)),
        time_expired=expired or (NOW + timedelta(days=25)),
        duration=30, duration_unit="DAY", is_intent_to_pay=False,
    )]


def _cost(amount, currency="USD"):
    return SimpleNamespace(computed_amount=amount, currency=currency)


def test_used_and_remaining_come_from_spend_since_the_credit_started(monkeypatch):
    s, asked = _session(monkeypatch, promotion=_promo(), usage_items=[_cost(100.25), _cost(20.25)])
    res = s.get_trial_credits()
    assert res.ok, res.message
    d = res.data
    assert d["registered_at"].startswith("2026-09-20")
    c = d["credit"]
    assert (c["amount"], c["currency"], c["status"]) == (300.0, "USD", "ACTIVE")
    assert (c["used"], c["remaining"]) == (120.5, 179.5)
    start, end = asked[0]
    assert start.hour == 0 and end.hour == 0, "Usage API 的 DAILY 粒度要求 UTC 零点"


def test_an_unreadable_spend_is_none_not_zero(monkeypatch):
    s, _ = _session(monkeypatch, promotion=_promo(), usage_error="NotAuthorizedOrNotFound")
    c = s.get_trial_credits().data["credit"]
    assert c["used"] is None and c["remaining"] is None, "读不到不能显示成「已用 0、剩余 300」"
    assert "读取费用失败" in c["used_note"]


def test_an_expired_credit_only_counts_spend_up_to_its_expiry(monkeypatch):
    started = NOW - timedelta(days=60)
    expired = NOW - timedelta(days=30)
    s, asked = _session(monkeypatch, promotion=_promo("EXPIRED", started=started, expired=expired),
                        usage_items=[_cost(300.0)])
    c = s.get_trial_credits().data["credit"]
    assert c["status"] == "EXPIRED" and c["remaining"] == 0.0
    _start, end = asked[0]
    assert end <= expired + timedelta(days=1), "过期之后的正常计费不该算进积分里"


def test_no_promotion_is_a_normal_answer(monkeypatch):
    s, _ = _session(monkeypatch, promotion=None)
    res = s.get_trial_credits()
    assert res.ok and res.data["credit"] is None and "没有试用积分" in res.message


def test_an_unreadable_subscription_is_reported(monkeypatch):
    s, _ = _session(monkeypatch, promotion=None, sub_error="boom")
    res = s.get_trial_credits()
    assert not res.ok and "订阅查询失败" in res.message
    assert res.data["registered_at"].startswith("2026-09-20"), "注册日期不依赖订阅接口"
