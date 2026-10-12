"""试用积分：注册日期、总额、已用、剩余、到期时间（到期 = 注册日期 + 30 天）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import oci.tenant_manager_control_plane as tmcp
from oci.tenant_manager_control_plane.subscription_client import (
    SubscriptionClient as _RealSubscriptionClient,
)

from app.oci_client import TenantSession
from tests._oci_fakes import check_sdk_kwargs

NOW = datetime.now(timezone.utc)


def _session(monkeypatch, *, promotion, registered=None, usage_items=None, usage_error=None,
             sub_error=None, detail_error=None):
    registered = registered or (NOW - timedelta(days=5))
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(tenancy_ocid="ocid1.tenancy..t", region="ap-singapore-1", name="T")
    s._config = {"user": "u", "fingerprint": "f", "tenancy": "ocid1.tenancy..t",
                 "region": "ap-singapore-1", "key_file": "k"}
    s._identity = SimpleNamespace(
        get_compartment=lambda cid: SimpleNamespace(data=SimpleNamespace(time_created=registered)),
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

        def get_subscription(self, subscription_id=None, **kwargs):
            # 和真 SDK 一样挑参数：0.4.135 多传了 entity_version，生产里每次都失败。
            check_sdk_kwargs(_RealSubscriptionClient.get_subscription, subscription_id, **kwargs)
            if detail_error is not None:
                raise detail_error
            return SimpleNamespace(data=SimpleNamespace(
                promotion=promotion, start_date=registered, subscription_tier="FREE_TIER",
            ))

    monkeypatch.setattr(tmcp, "SubscriptionClient", FakeSubscription)
    return s, asked


def _promo(status="ACTIVE", amount=300.0, expired=None):
    return [SimpleNamespace(
        amount=amount, currency_unit="USD", status=status,
        time_started=NOW - timedelta(days=5),
        # Oracle 给的到期时间故意和「注册 + 30 天」不同：页面只认后者。
        time_expired=expired or (NOW + timedelta(days=90)),
        duration=30, duration_unit="DAY", is_intent_to_pay=False,
    )]


def _cost(amount, currency="USD"):
    return SimpleNamespace(computed_amount=amount, currency=currency)


def test_expiry_is_registration_plus_thirty_days(monkeypatch):
    registered = NOW - timedelta(days=5)
    s, asked = _session(monkeypatch, promotion=_promo(), registered=registered,
                        usage_items=[_cost(100.25), _cost(20.25)])
    res = s.get_trial_credits()
    assert res.ok, res.message
    c = res.data["credit"]
    assert c["time_expired"] == (registered + timedelta(days=30)).isoformat()
    assert c["status"] == "ACTIVE"
    assert (c["amount"], c["currency"]) == (300.0, "USD")
    assert (c["used"], c["remaining"]) == (120.5, 179.5)
    start, end = asked[0]
    assert start.hour == 0 and end.hour == 0, "Usage API 的 DAILY 粒度要求 UTC 零点"
    assert start.date() == registered.date(), "已用从注册日期起算"


def test_past_thirty_days_is_expired_and_counts_spend_only_until_then(monkeypatch):
    registered = NOW - timedelta(days=45)
    s, asked = _session(monkeypatch, promotion=_promo(), registered=registered, usage_items=[_cost(300.0)])
    c = s.get_trial_credits().data["credit"]
    assert c["status"] == "EXPIRED", "注册已满 30 天，即使 Oracle 记录里还写着有效"
    assert c["remaining"] == 0.0
    _start, end = asked[0]
    assert end <= registered + timedelta(days=31), "过期之后的正常计费不该算进积分里"


def test_an_unreadable_spend_is_none_not_zero(monkeypatch):
    s, _ = _session(monkeypatch, promotion=_promo(), usage_error="NotAuthorizedOrNotFound")
    c = s.get_trial_credits().data["credit"]
    assert c["used"] is None and c["remaining"] is None, "读不到不能显示成「已用 0、剩余 300」"
    assert "读取费用失败" in c["used_note"]


def test_without_a_promotion_record_expiry_and_spend_are_still_shown(monkeypatch):
    """总额读不到时不编数字，但到期时间、已用照样能按注册日期算出来。"""
    s, _ = _session(monkeypatch, promotion=None, usage_items=[_cost(12.0)])
    res = s.get_trial_credits()
    assert res.ok
    c = res.data["credit"]
    assert c["amount"] is None and c["remaining"] is None
    assert c["used"] == 12.0 and c["time_expired"]
    assert any("读不到" in n for n in res.data["notes"])


def test_a_failed_detail_read_is_reported_not_hidden(monkeypatch):
    from oci.exceptions import ServiceError

    s, _ = _session(
        monkeypatch,
        promotion=_promo(),
        detail_error=ServiceError(404, "NotAuthorizedOrNotFound", {}, "Authorization failed"),
    )
    res = s.get_trial_credits()
    assert res.ok, "注册日期在手，到期时间和已用仍然能给"
    assert res.data["credit"]["amount"] is None
    assert any("NotAuthorizedOrNotFound" in n for n in res.data["notes"])


def test_an_unreadable_subscription_still_gives_dates(monkeypatch):
    s, _ = _session(monkeypatch, promotion=None, sub_error="boom")
    res = s.get_trial_credits()
    assert res.ok
    assert res.data["credit"]["time_expired"]
    assert any("订阅查询失败" in n for n in res.data["notes"])


def test_no_registration_date_at_all_is_an_error(monkeypatch):
    s, _ = _session(monkeypatch, promotion=None, sub_error="boom")
    s._identity.get_compartment = lambda cid: (_ for _ in ()).throw(RuntimeError("denied"))
    res = s.get_trial_credits()
    assert not res.ok and "注册日期" in res.message
