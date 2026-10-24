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


# ---------------------------------------------------------------- 0.4.141 检查出的问题


def test_a_locked_database_does_not_lose_the_query_result(monkeypatch):
    """缓存只是顺手记一下。SQLite 在 worker 写库时会报 database is locked —— 以前这会让
    一次已经成功的 Oracle 查询变成 502，结果直接丢掉；「查询试用积分」0.4.141 之前
    根本不写库，这是新引入的失败方式。"""
    from sqlalchemy.exc import OperationalError

    db = _db()

    def locked():
        raise OperationalError("UPDATE tenants", {}, Exception("database is locked"))

    monkeypatch.setattr(db, "commit", locked)
    data = {"tier_code": "free", "setup": {"state": "READY", "registered_at": "2026-10-10T08:00:00+00:00"}}
    assert _route(monkeypatch, db, "p", "get_tier_status", data)["data"] == data
    trial = {"registered_at": "2026-10-01T00:00:00Z", "credit": {"amount": 300}}
    assert _route(monkeypatch, db, "p", "get_trial_credits", trial)["data"] == trial


def test_a_non_utc_registration_time_is_stored_as_utc(monkeypatch):
    """SQLite 存带时区的时间时直接丢掉时区、不换算：+08:00 的 11 点会被当成 UTC 11 点
    存下，读回来差 8 小时。"""
    from web.backend.models import Tenant

    db = _db()
    _route(monkeypatch, db, "p", "get_tier_status", {"setup": {"registered_at": "2026-10-05T11:00:00+08:00"}})
    db.expire_all()
    stored = db.get(Tenant, "p").registered_at
    stored = stored if stored.tzinfo else stored.replace(tzinfo=timezone.utc)
    assert stored == datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 0.4.157 等级查询提速


def _gate(monkeypatch, s, gate, *, region_subs=True, compartment=True, oracle=True, seen=None):
    """让选中的几条读取先在 ``gate`` 上碰头：一条接一条发的话，第一条就等到超时。"""
    import oci.osp_gateway as osp
    import oci.tenant_manager_control_plane as tmcp

    seen = seen if seen is not None else {}
    identity = s._identity
    real_regions, real_root = identity.list_region_subscriptions, identity.get_compartment

    def regions(t):
        if region_subs:
            gate.wait()
        return real_regions(t)

    def root(cid):
        if compartment:
            gate.wait()
        return real_root(cid)

    s._identity = SimpleNamespace(
        get_tenancy=identity.get_tenancy, get_compartment=root, list_region_subscriptions=regions
    )
    Sub, Osp = tmcp.SubscriptionClient, osp.SubscriptionServiceClient

    class GatedSub(Sub):
        def __init__(self, cfg, **kw):
            seen.setdefault("sub", []).append((cfg["region"], kw))
            super().__init__(cfg, **kw)

        def list_subscriptions(self, *a, **k):
            if oracle:
                gate.wait()
            return super().list_subscriptions(*a, **k)

    class GatedOsp(Osp):
        def __init__(self, cfg, **kw):
            seen.setdefault("osp", []).append((cfg["region"], kw))
            super().__init__(cfg, **kw)

        def list_subscriptions(self, osp_home_region, compartment_id, **k):
            seen.setdefault("osp_home", []).append(osp_home_region)
            if oracle:
                gate.wait()
            return super().list_subscriptions(osp_home_region, compartment_id, **k)

    monkeypatch.setattr(tmcp, "SubscriptionClient", GatedSub)
    monkeypatch.setattr(osp, "SubscriptionServiceClient", GatedOsp)
    return seen


def _all_read(data):
    return (
        data["tier_code"] == "free"
        and data["setup"]["state"] == "READY"
        and data["setup"]["registered_at"]
        and "error" not in data["upgrade"]
    )


def test_the_tier_reads_go_out_together(monkeypatch):
    """以前区域订阅、订阅记录、升级记录、根区间一条接一条地发，要等五六次往返，
    其中两条还跨洲走主区端点。会话里已有主区时，四条应该同时在路上。"""
    import threading

    s = _session(monkeypatch, registered_days_ago=30)
    s._home_region_name = "ap-tokyo-1"
    _gate(monkeypatch, s, threading.Barrier(4, timeout=5))
    assert _all_read(s.get_tier_status().data)


def test_a_cold_session_learns_the_home_region_first(monkeypatch):
    """还没有主区时，订阅记录、升级记录要等区域订阅回来 —— 它们要走的就是主区端点，
    先发就会落到租户自己的区域、各自再问一次主区。根区间不用等，和区域订阅一起发。"""
    import threading

    s = _session(monkeypatch, registered_days_ago=30, region="ap-osaka-1", home="ap-tokyo-1")
    count = {"n": 0}
    real = s._identity.list_region_subscriptions

    def counted(t):
        count["n"] += 1
        return real(t)

    s._identity.list_region_subscriptions = counted
    seen = _gate(monkeypatch, s, threading.Barrier(2, timeout=5), oracle=False)
    assert _all_read(s.get_tier_status().data)
    assert count["n"] == 1
    assert [r for r, _ in seen["sub"]] == ["ap-tokyo-1"]
    assert [r for r, _ in seen["osp"]] == ["ap-tokyo-1"] and seen["osp_home"] == ["ap-tokyo-1"]


def test_account_reads_give_up_quickly(monkeypatch):
    """订阅记录、升级记录的 client 以前用默认重试：连网络错误也重试 8 次、总长 600 秒。
    主区端点不通时一次等级查询要白等一两分钟，换区域的那条退路基本等不到。"""
    import threading

    s = _session(monkeypatch, registered_days_ago=30)
    seen = _gate(monkeypatch, s, threading.Barrier(1), region_subs=False, compartment=False, oracle=False)
    s.get_tier_status()
    for key in ("sub", "osp"):
        (_region, kw), = seen[key]
        attempts = [c.max_attempts for c in kw["retry_strategy"].checkers.checkers if hasattr(c, "max_attempts")]
        assert attempts == [3], key
        assert kw["timeout"] == (5.0, 30.0), key
        assert "circuit_breaker_strategy" in kw, "每租户 × 每服务的熔断器不能丢"


# ---------------------------------------------------------------- 0.4.168 单次查询再快一点


def _subscriptions(monkeypatch, payment_model, tier=""):
    """订阅列表只给付费模式；完整记录（GetSubscription）给 subscription_tier，并记下被读了几次。"""
    import oci.tenant_manager_control_plane as tmcp

    calls = {"get": 0}

    class Sub:
        def __init__(self, cfg, **_kw):
            pass

        def list_subscriptions(self, compartment_id, **_kw):
            item = SimpleNamespace(payment_model=payment_model, id="sub1", lifecycle_state="ACTIVE")
            return SimpleNamespace(data=SimpleNamespace(items=[item]))

        def get_subscription(self, subscription_id, **_kw):
            calls["get"] += 1
            return SimpleNamespace(data=SimpleNamespace(subscription_tier=tier, promotion=None))

    monkeypatch.setattr(tmcp, "SubscriptionClient", Sub)
    return calls


@pytest.mark.parametrize(
    "payment_model, tier, expected, reads",
    [
        ("Free Trial", "", "free", 0),
        ("Pay as you go", "", "paid", 0),
        # 付费模式判断不出来，才去读完整记录补判。
        ("", "FREETIER", "free", 1),
        ("Something new", "PAYG", "paid", 1),
    ],
)
def test_the_full_record_is_read_only_when_the_payment_model_cannot_decide(
    monkeypatch, payment_model, tier, expected, reads
):
    """完整记录要等列表回来才能发，是等级查询里最长的那一串。付费模式够判的时候不读它，
    每次查询少等一轮跨区往返。"""
    s = _session(monkeypatch, registered_days_ago=30)
    calls = _subscriptions(monkeypatch, payment_model, tier)
    assert s.get_tier_status().data["tier_code"] == expected
    assert calls["get"] == reads


def test_trial_credits_still_read_the_full_record(monkeypatch):
    """积分金额只在完整记录里：试用积分照旧一律读。"""
    s = _session(monkeypatch, registered_days_ago=30)
    calls = _subscriptions(monkeypatch, "Free Trial")
    pairs, error = s._subscription_records()
    assert error == "" and calls["get"] == 1 and pairs[0][1] is not None


def test_a_remembered_home_region_is_used_without_asking():
    from app.oci_client import TenantSession

    def boom(*_a, **_k):
        raise AssertionError("记下的主区够用了，不该再问 Oracle")

    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(home_region="ap-tokyo-1", region="ap-osaka-1", tenancy_ocid="t")
    s._identity = SimpleNamespace(list_region_subscriptions=boom)
    s._seed_home_region()
    assert s.home_region_confirmed() == "ap-tokyo-1"


@pytest.mark.parametrize("value", ["", "evil", "AP-TOKYO-1", "ap-tokyo-1/../x", "ap-tokyo-1 x"])
def test_a_malformed_home_region_is_not_trusted(value):
    """库被手改过之类：格式不对就不当「已确认」用，退回现问。"""
    from app.oci_client import TenantSession

    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(home_region=value)
    s._seed_home_region()
    assert getattr(s, "_home_region_name", None) is None
    assert not getattr(s, "_home_region_resolved", False)


def test_a_new_session_starts_with_the_remembered_home_region():
    """真的建一个会话：TenantConfig.home_region 一进来就是已确认的主区。"""
    pytest.importorskip("oci")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.config_store import TenantConfig
    from app.oci_client import TenantSession

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                       serialization.NoEncryption())
        .decode()
    )
    cfg = TenantConfig(
        id="t1", name="T", user_ocid="ocid1.user.oc1..".ljust(40, "a"),
        tenancy_ocid="ocid1.tenancy.oc1..".ljust(40, "a"),
        fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
        region="ap-osaka-1", private_key_pem=pem, home_region="ap-tokyo-1",
    )
    session = TenantSession(cfg)
    try:
        assert session._home_region_name == "ap-tokyo-1"
        assert session._home_region_resolved is True
    finally:
        session.close()


def test_with_a_remembered_home_region_the_first_query_starts_everything_at_once(monkeypatch):
    """以前每个新会话的第一次查询都要先问一轮主区（面板重启、账号等级一变都会重建会话）。"""
    import threading

    s = _session(monkeypatch, registered_days_ago=30)
    s.tenant.home_region = "ap-tokyo-1"
    s._seed_home_region()
    _gate(monkeypatch, s, threading.Barrier(4, timeout=5))
    assert _all_read(s.get_tier_status().data)


def test_only_a_confirmed_home_region_is_reported_for_caching(monkeypatch):
    """区域订阅读失败时 _home_region() 会退回租户自己填的 region —— 那是猜的，不能记进库。"""
    ok = _session(monkeypatch, registered_days_ago=30, region="ap-osaka-1", home="ap-tokyo-1")
    assert ok.get_tier_status().data["home_region_confirmed"] == "ap-tokyo-1"
    failed = _session(monkeypatch, registered_days_ago=30, identity_fails=True)
    assert failed.get_tier_status().data["home_region_confirmed"] == ""


def test_the_tier_route_remembers_the_home_region_for_the_whole_account(monkeypatch):
    from web.backend.models import Tenant
    from web.backend.routers import instances as r

    db = _db()
    _route(monkeypatch, db, "c1", "get_tier_status", {"home_region_confirmed": "ap-tokyo-1"})
    assert [db.get(Tenant, t).home_region for t in ("p", "c1", "c2")] == ["ap-tokyo-1"] * 3
    assert db.get(Tenant, "other").home_region == ""
    for bad in ("", "evil", "ap-tokyo-1\n"):
        assert "home_region" not in r._account_state_fields({"home_region_confirmed": bad})
