"""租户页「等级查询」的精简版（get_tier_status / GET /tenants/{id}/tier）和注册日期缓存。

精简版只读等级要用的几样：不读租户详情、不拉整张计算配额表、不读公网 IP 配额；
「准备中」要看的 A1 配额只在新号、在主区时按名字查一条。它和账号用量页那份完整报告
共用同一套判定，两边的结论必须一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.oci_client import A1_CORE_LIMIT_NAME, TRIAL_DAYS
from tests.test_account_setup import _session
from tests.test_upgrade_status import _db


def _spy(s):
    """记下精简版不该碰的调用，以及区域订阅、配额各被问了几次。"""
    calls = {"tenancy": 0, "limit_names": [], "region_subs": 0}
    identity = s._identity
    real_limits = s._limits.list_limit_values
    real_region_subs = identity.list_region_subscriptions

    def get_tenancy(*_a, **_k):
        calls["tenancy"] += 1
        raise AssertionError("精简版不该读租户详情")

    def list_limit_values(t, **kw):
        calls["limit_names"].append(kw.get("name"))
        return real_limits(t, **kw)

    def list_region_subscriptions(t):
        calls["region_subs"] += 1
        return real_region_subs(t)

    s._identity = SimpleNamespace(
        get_tenancy=get_tenancy,
        get_compartment=identity.get_compartment,
        list_region_subscriptions=list_region_subscriptions,
    )
    s._limits = SimpleNamespace(
        list_limit_values=list_limit_values,
        list_services=lambda *a, **k: (_ for _ in ()).throw(AssertionError("精简版不该读公网 IP 配额")),
    )
    return calls


def test_an_established_account_needs_no_quota_read(monkeypatch):
    s = _session(monkeypatch, registered_days_ago=30)
    calls = _spy(s)
    data = s.get_tier_status().data
    assert data["tier_code"] == "free"
    assert data["setup"]["state"] == "READY"
    assert calls["tenancy"] == 0
    assert calls["limit_names"] == [], "过了 7 天，「配额是 0」这条依据不用，配额一条都不该查"
    assert calls["region_subs"] == 1, "区域订阅只问一次，主区顺带记进缓存"


def test_a_new_account_reads_only_the_a1_limit(monkeypatch):
    s = _session(monkeypatch, registered_days_ago=1, limits=(0.0, 2.0))
    calls = _spy(s)
    data = s.get_tier_status().data
    assert calls["limit_names"] == [A1_CORE_LIMIT_NAME]
    assert data["setup"]["state"] == "PROVISIONING"


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"sub_state": "CREATING"},
        {"root_state": "CREATING"},
        {"region_status": "IN_PROGRESS"},
        {"limits": (0.0, 0.0), "registered_days_ago": 1},
        {"limits": (0.0, 0.0), "registered_days_ago": 30},
        {"limits": (0.0, 0.0), "registered_days_ago": 1, "region": "us-ashburn-1"},
    ],
)
def test_the_light_probe_agrees_with_the_full_report(monkeypatch, kw):
    light = _session(monkeypatch, **kw).get_tier_status().data
    full = _session(monkeypatch, **kw).get_account_status().data
    for key in ("tier", "tier_code", "tier_reason"):
        assert light[key] == full[key], key
    assert light["setup"]["state"] == full["setup"]["state"]
    assert light["setup"]["reasons"] == full["setup"]["reasons"]


def test_the_full_report_asks_for_region_subscriptions_once(monkeypatch):
    s = _session(monkeypatch, registered_days_ago=30)
    count = {"n": 0}
    real = s._identity.list_region_subscriptions

    def counted(t):
        count["n"] += 1
        return real(t)

    s._identity.list_region_subscriptions = counted
    s.get_account_status()
    assert count["n"] == 1


# ---------------------------------------------------------------- 路由与注册日期缓存


def _route(monkeypatch, db, tenant_id, method, data):
    from web.backend.models import Tenant
    from web.backend.routers import instances as r

    monkeypatch.setattr(r, "_tenant_or_404", lambda db_, uid, tid: db_.get(Tenant, tid))
    monkeypatch.setattr(
        r, "get_session_for_row",
        lambda row: SimpleNamespace(**{method: lambda: SimpleNamespace(ok=True, message="", data=data)}),
    )
    route = {"get_tier_status": r.tier_status, "get_trial_credits": r.trial_credits}[method]
    return route(tenant_id, SimpleNamespace(id="u1"), db)


def test_the_tier_route_caches_everything_on_the_whole_account(monkeypatch):
    from web.backend.models import Tenant

    db = _db()
    reg = "2026-10-10T08:00:00+00:00"
    out = _route(monkeypatch, db, "c1", "get_tier_status", {
        "tier_code": "free", "tier": "升级中", "upgrade": {"upgrade_state": "SUBMITTED"},
        "setup": {"state": "READY", "registered_at": reg},
    })
    assert out["ok"] is True
    for tid in ("p", "c1", "c2"):
        row = db.get(Tenant, tid)
        assert (row.account_tier, row.upgrade_state, row.setup_state) == ("free", "SUBMITTED", "READY")
        assert row.registered_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
    assert db.get(Tenant, "other").registered_at is None


def test_the_trial_credit_query_also_records_the_registration_date(monkeypatch):
    from web.backend.models import Tenant

    db = _db()
    _route(monkeypatch, db, "p", "get_trial_credits", {"registered_at": "2026-10-01T00:00:00Z"})
    assert db.get(Tenant, "c2").registered_at is not None


def test_an_unchanged_registration_date_is_not_rewritten(monkeypatch):
    """SQLite 读回来不带时区，直接比较会永远「不相等」，每次查询都白写一次库。"""
    from web.backend.models import Tenant
    from web.backend.routers import instances as r

    db = _db()
    data = {"setup": {"registered_at": "2026-10-10T08:00:00+00:00"}}
    _route(monkeypatch, db, "p", "get_tier_status", data)
    db.expire_all()  # 从库里重新读，拿到的是不带时区的值
    commits = {"n": 0}
    real_commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (commits.__setitem__("n", commits["n"] + 1), real_commit()))
    r._cache_account_state(db, db.get(Tenant, "p"), data)
    assert commits["n"] == 0


def test_the_tenant_list_shows_when_the_trial_credit_expires():
    from web.backend.models import Tenant
    from web.backend.routers.tenants import _to_out

    now = datetime.now(timezone.utc)
    reg = datetime(2026, 10, 1, 9, 30)  # 不带时区 —— SQLite 读回来就是这样
    row = Tenant(id="p", owner_id="u1", name="p", user_ocid="", tenancy_ocid="", fingerprint="",
                 region="ap-tokyo-1", registered_at=reg, created_at=now, updated_at=now)
    out = _to_out(row)
    assert out.registered_at == reg.replace(tzinfo=timezone.utc)
    assert out.trial_expires_at == reg.replace(tzinfo=timezone.utc) + timedelta(days=TRIAL_DAYS)

    row.registered_at = None
    out = _to_out(row)
    assert out.registered_at is None and out.trial_expires_at is None
