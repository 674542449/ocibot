"""账号「准备中」：刚注册、Oracle 还在开通的账号要标出来。

依据都是 Oracle 文档里的状态字段（订阅 / 租户 lifecycle_state=CREATING、主区区域订阅
status=IN_PROGRESS），外加一条经验依据：新号在主区的 A1 / E2 Micro 计算配额全是 0。
等级（tier_code）不受影响 —— 额度保护照旧按订阅记录判定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import oci.osp_gateway as osp
import oci.tenant_manager_control_plane as tmcp
from oci.tenant_manager_control_plane.subscription_client import (
    SubscriptionClient as _RealSubscriptionClient,
)

from tests._oci_fakes import check_sdk_kwargs
from tests.test_account_tier import _session as _tier_session
from tests.test_upgrade_status import _call_account, _db


def _session(
    monkeypatch,
    *,
    sub_state="ACTIVE",
    root_state="ACTIVE",
    region_status="READY",
    registered_days_ago=1,
    limits=(4.0, 2.0),
    region="ap-tokyo-1",
    home="ap-tokyo-1",
    subscription_fails=False,
    identity_fails=False,
):
    s = _tier_session("Free Trial", monkeypatch=monkeypatch, home_region=home)
    s.tenant.region = region

    class FakeSubscription:
        def __init__(self, cfg, **_kw):
            pass

        def list_subscriptions(self, compartment_id, **kwargs):
            check_sdk_kwargs(_RealSubscriptionClient.list_subscriptions, compartment_id, **kwargs)
            if subscription_fails:
                raise RuntimeError("no subscription access")
            item = SimpleNamespace(payment_model="Free Trial", id="sub1", lifecycle_state=sub_state)
            return SimpleNamespace(data=SimpleNamespace(items=[item]))

        def get_subscription(self, subscription_id, **kwargs):
            check_sdk_kwargs(_RealSubscriptionClient.get_subscription, subscription_id, **kwargs)
            return SimpleNamespace(data=SimpleNamespace(subscription_tier="", promotion=None))

    class FakeOsp:
        def __init__(self, cfg, **_kw):
            pass

        def list_subscriptions(self, osp_home_region, compartment_id, **_kw):
            return SimpleNamespace(data=SimpleNamespace(items=[]))

    monkeypatch.setattr(tmcp, "SubscriptionClient", FakeSubscription)
    monkeypatch.setattr(osp, "SubscriptionServiceClient", FakeOsp)

    def fail(*_a, **_k):
        raise RuntimeError("identity unavailable")

    root = SimpleNamespace(
        lifecycle_state=root_state,
        time_created=datetime.now(timezone.utc) - timedelta(days=registered_days_ago),
    )
    s._identity = SimpleNamespace(
        get_tenancy=lambda t: SimpleNamespace(
            data=SimpleNamespace(name="T", home_region_key="NRT", description="")
        ),
        get_compartment=fail if identity_fails else (lambda cid: SimpleNamespace(data=root)),
        list_region_subscriptions=fail if identity_fails else (
            lambda t: SimpleNamespace(
                data=[SimpleNamespace(region_name=home, is_home_region=True, status=region_status)]
            )
        ),
    )
    values = [
        SimpleNamespace(name="standard-a1-core-count", availability_domain="AD-1",
                        scope_type="AD", value=limits[0]),
        SimpleNamespace(name="standard-e2-micro-core-count", availability_domain="AD-1",
                        scope_type="AD", value=limits[1]),
    ]
    s._limits = SimpleNamespace(list_limit_values=lambda t, **_kw: SimpleNamespace(data=values))
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, *a, **kw: fn(*a, **kw))
    return s


def test_a_ready_account_is_ready(monkeypatch):
    data = _session(monkeypatch).get_account_status().data
    assert data["setup"]["state"] == "READY"
    assert data["setup"]["reasons"] == []
    assert "准备中" not in data["tier"]


def test_a_subscription_still_being_created_is_provisioning(monkeypatch):
    data = _session(monkeypatch, sub_state="CREATING").get_account_status().data
    assert data["setup"]["state"] == "PROVISIONING"
    assert "准备中" in data["tier"]
    assert data["tier_code"] == "free", "准备中只改显示，不改等级判定"
    assert any("CREATING" in r for r in data["setup"]["reasons"])


def test_a_tenancy_still_being_created_is_provisioning(monkeypatch):
    data = _session(monkeypatch, root_state="CREATING").get_account_status().data
    assert data["setup"]["state"] == "PROVISIONING"


def test_a_home_region_still_in_progress_is_provisioning(monkeypatch):
    data = _session(monkeypatch, region_status="IN_PROGRESS").get_account_status().data
    assert data["setup"]["state"] == "PROVISIONING"
    assert any("IN_PROGRESS" in r for r in data["setup"]["reasons"])


def test_a_new_account_without_compute_quota_is_provisioning(monkeypatch):
    data = _session(monkeypatch, limits=(0.0, 0.0), registered_days_ago=1).get_account_status().data
    assert data["setup"]["state"] == "PROVISIONING"
    assert any("配额" in r for r in data["setup"]["reasons"])


def test_an_old_account_without_compute_quota_is_not_called_provisioning(monkeypatch):
    """注册一个月了配额还是 0，是别的原因，不能说成「还在开通」。"""
    data = _session(monkeypatch, limits=(0.0, 0.0), registered_days_ago=30).get_account_status().data
    assert data["setup"]["state"] == "READY"


def test_zero_quota_outside_the_home_region_is_not_a_signal(monkeypatch):
    """免费 A1 额度只在主区；副区读到 0 是正常的。"""
    s = _session(monkeypatch, limits=(0.0, 0.0), region="us-ashburn-1", home="ap-tokyo-1")
    # 不算「准备中」；新号在副区上也不下「已就绪」的结论（见 test_account_state_review）。
    assert s.get_account_status().data["setup"]["state"] != "PROVISIONING"


def test_nothing_readable_is_no_verdict(monkeypatch):
    s = _session(monkeypatch, subscription_fails=True, identity_fails=True)
    setup = s.get_account_status().data["setup"]
    assert setup["state"] == "", "读不到不能说成「已就绪」"


def test_the_setup_state_is_cached_on_the_whole_account(monkeypatch):
    from web.backend.models import Tenant

    db = _db()
    _call_account(monkeypatch, db, "c1", {"error": "x"}, setup={"state": "PROVISIONING"})
    states = {t.id: t.setup_state for t in db.query(Tenant).all()}
    assert states == {"p": "PROVISIONING", "c1": "PROVISIONING", "c2": "PROVISIONING",
                      "other": "", "foreign": ""}
    # 读不到（state 为空）不动缓存；读到已就绪就清掉「准备中」。
    _call_account(monkeypatch, db, "p", {"error": "x"}, setup={"state": ""})
    assert db.get(Tenant, "p").setup_state == "PROVISIONING"
    _call_account(monkeypatch, db, "p", {"error": "x"}, setup={"state": "READY"})
    assert {t.setup_state for t in db.query(Tenant).filter(Tenant.owner_id == "u1", Tenant.id != "other")} == {"READY"}
