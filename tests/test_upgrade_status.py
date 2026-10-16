"""账号「升级中 / 升级失败」状态（OSP Gateway 订阅记录的 upgrade_state）。

升级中的账号还不是付费号：等级、额度保护都照旧按免费处理，只是把状态显示出来。
Oracle 的升级记录说已升级时，即使租户管理那条订阅记录没认出来，也按付费处理。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import oci.osp_gateway as osp
from oci.exceptions import ServiceError
from oci.osp_gateway.subscription_service_client import (
    SubscriptionServiceClient as _RealOspSubscriptionClient,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.oci_client import TenantSession
from tests._oci_fakes import check_sdk_kwargs
from tests.test_account_tier import _session as _tier_session


def _with_upgrade(monkeypatch, payment_model, *, state="", details="", plan="", fail=None, upgraded_at=None):
    """一个账号：租户管理订阅记录给 ``payment_model``，OSP 升级记录给 state / plan。"""
    s = _tier_session(payment_model, monkeypatch=monkeypatch)
    calls: list[dict] = []

    class FakeOsp:
        def __init__(self, cfg, **_kw):
            self.region = cfg["region"]

        def list_subscriptions(self, osp_home_region, compartment_id, **kwargs):
            check_sdk_kwargs(
                _RealOspSubscriptionClient.list_subscriptions, osp_home_region, compartment_id, **kwargs
            )
            calls.append({"region": self.region, "osp_home_region": osp_home_region,
                          "compartment_id": compartment_id})
            if fail is not None:
                raise fail
            return SimpleNamespace(
                data=SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            upgrade_state=state or None,
                            upgrade_state_details=details or None,
                            plan_type=plan or None,
                            account_type="PERSONAL",
                            time_plan_upgrade=upgraded_at,
                        )
                    ]
                )
            )

    monkeypatch.setattr(osp, "SubscriptionServiceClient", FakeOsp)
    return s, calls


def test_reads_the_upgrade_record_from_the_home_region(monkeypatch):
    s, calls = _with_upgrade(monkeypatch, "Free Trial", state="submitted", plan="FREE_TIER")
    res = s.get_upgrade_status()
    assert res.ok, res.message
    assert res.data["upgrade_state"] == "SUBMITTED"
    assert res.data["plan_type"] == "FREE_TIER"
    # 和账单一样走主区端点，osp_home_region 也是主区。
    assert calls == [{"region": "eu-amsterdam-1", "osp_home_region": "eu-amsterdam-1",
                      "compartment_id": "ocid1.tenancy..t"}]


def test_upgrading_account_is_shown_but_still_treated_as_free(monkeypatch):
    s, _ = _with_upgrade(monkeypatch, "Free Trial", state="SUBMITTED", plan="FREE_TIER")
    data = s.get_account_status().data
    assert data["tier_code"] == "free", "升级没完成就不是付费号"
    assert "升级中" in data["tier"]
    assert data["upgrade"]["upgrade_state"] == "SUBMITTED"


def test_payg_plan_while_still_submitted_is_not_paid(monkeypatch):
    """plan_type 可能先变；升级状态还是 SUBMITTED 时不能据此放开额度保护。"""
    s, _ = _with_upgrade(monkeypatch, None, state="SUBMITTED", plan="PAYG")
    data = s.get_account_status().data
    assert data["tier_code"] != "paid"
    assert "升级中" in data["tier"]


def test_upgraded_record_makes_an_unreadable_subscription_paid(monkeypatch):
    when = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    s, _ = _with_upgrade(monkeypatch, None, state="UPGRADED", plan="PAYG", upgraded_at=when)
    data = s.get_account_status().data
    assert data["tier_code"] == "paid"
    assert data["upgrade"]["time_plan_upgrade"] == when.isoformat()


def test_upgrade_error_shows_the_reason(monkeypatch):
    s, _ = _with_upgrade(monkeypatch, "Free Trial", state="ERROR", details="TAX_ERROR")
    data = s.get_account_status().data
    assert data["tier_code"] == "free"
    assert "升级失败" in data["tier"] and "税务" in data["tier"]


def test_paid_subscription_is_not_downgraded_by_a_stale_upgrade_state(monkeypatch):
    s, _ = _with_upgrade(monkeypatch, "Pay as you go", state="SUBMITTED")
    data = s.get_account_status().data
    assert data["tier_code"] == "paid"
    assert "升级中" not in data["tier"]


def test_an_unreadable_upgrade_record_leaves_the_tier_alone(monkeypatch):
    err = ServiceError(404, "NotAuthorizedOrNotFound", {}, "not found")
    s, _ = _with_upgrade(monkeypatch, "Free Trial", fail=err)
    data = s.get_account_status().data
    assert data["tier_code"] == "free"
    assert "error" in data["upgrade"] and "NotAuthorizedOrNotFound" in data["upgrade"]["error"]


# ---------------------------------------------------------------- 缓存到租户行


def _db():
    from web.backend.db import Base
    from web.backend.models import Tenant, User

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(User(id="u1", username="u1", password_hash="x"))
    db.add(User(id="u2", username="u2", password_hash="x"))
    for tid, owner, parent in (("p", "u1", ""), ("c1", "u1", "p"), ("c2", "u1", "p"),
                               ("other", "u1", ""), ("foreign", "u2", "p")):
        db.add(Tenant(id=tid, owner_id=owner, name=tid, parent_tenant_id=parent))
    db.commit()
    return db


def _call_account(monkeypatch, db, tenant_id, upgrade, setup=None):
    from web.backend.models import Tenant
    from web.backend.routers import instances as r

    monkeypatch.setattr(r, "_tenant_or_404", lambda db_, uid, tid: db_.get(Tenant, tid))
    data = {"tier_code": "free", "tier": "x", "upgrade": upgrade, "setup": setup}
    monkeypatch.setattr(
        r, "get_session_for_row",
        lambda row: SimpleNamespace(get_account_status=lambda: SimpleNamespace(ok=True, message="", data=data)),
    )
    return r.account_status(tenant_id, SimpleNamespace(id="u1"), db)


def test_the_state_is_cached_on_the_primary_and_all_its_sub_regions(monkeypatch):
    from web.backend.models import Tenant

    db = _db()
    _call_account(monkeypatch, db, "c1", {"upgrade_state": "SUBMITTED", "upgrade_state_details": ""})
    states = {t.id: t.upgrade_state for t in db.query(Tenant).all()}
    assert states == {"p": "SUBMITTED", "c1": "SUBMITTED", "c2": "SUBMITTED",
                      "other": "", "foreign": ""}, "只改同一个账号、同一个用户的行"


def test_a_failed_read_keeps_the_cached_state(monkeypatch):
    from web.backend.models import Tenant

    db = _db()
    _call_account(monkeypatch, db, "p", {"upgrade_state": "ERROR", "upgrade_state_details": "TAX_ERROR"})
    _call_account(monkeypatch, db, "p", {"error": "读取升级状态失败：[429] TooManyRequests"})
    row = db.get(Tenant, "p")
    assert (row.upgrade_state, row.upgrade_state_details) == ("ERROR", "TAX_ERROR")


def test_the_tenant_list_carries_the_upgrade_state():
    from web.backend.models import Tenant
    from web.backend.routers.tenants import _to_out

    now = datetime.now(timezone.utc)
    row = Tenant(id="p", owner_id="u1", name="p", user_ocid="", tenancy_ocid="", fingerprint="",
                 region="ap-tokyo-1", account_tier="free", upgrade_state="SUBMITTED",
                 upgrade_state_details="", created_at=now, updated_at=now)
    out = _to_out(row)
    assert out.upgrade_state == "SUBMITTED"
    assert out.upgrade_state_details == ""
