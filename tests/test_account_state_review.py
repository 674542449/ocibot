"""0.4.138 / 0.4.139（升级状态、准备中）检查出的问题，各一条回归测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests._keys import TEST_PEM
from tests.test_account_setup import _session as _setup_session
from tests.test_upgrade_status import _call_account, _db


def test_the_tier_is_cached_on_the_whole_account_too():
    """升级状态记到整个账号，等级却只记点了按钮的那一行：在副区行点「等级查询」后，
    主租户标签显示「升级」（来自 upgrade_state=UPGRADED），自己的 account_tier 却还是
    free —— 多出口 IP 说「仅升级账号」，主区开机也照免费上限拦。"""
    import pytest

    from web.backend.models import Tenant

    db = _db()
    for t in db.query(Tenant).all():
        t.account_tier = "free"
    db.commit()
    mp = pytest.MonkeyPatch()
    try:
        from web.backend.routers import instances as r

        data = {"tier_code": "paid", "upgrade": {"upgrade_state": "UPGRADED"}, "setup": None}
        mp.setattr(r, "_tenant_or_404", lambda db_, uid, tid: db_.get(Tenant, tid))
        mp.setattr(r, "get_session_for_row", lambda row: SimpleNamespace(
            get_account_status=lambda: SimpleNamespace(ok=True, message="", data=data)))
        r.account_status("c1", SimpleNamespace(id="u1"), db)
    finally:
        mp.undo()
    tiers = {t.id: t.account_tier for t in db.query(Tenant).all()}
    assert tiers == {"p": "paid", "c1": "paid", "c2": "paid", "other": "free", "foreign": "free"}


def _gpu_heavy_limits(a1_value):
    """A1 有配额，另有一堆名字里带 a1 的 GPU（A10 / A100）限额 —— 免费号上都是 0。"""
    ads = ("AD-1", "AD-2", "AD-3")
    names = ["bm-gpu-a10-4-count", "bm-gpu-a100-v2-8-count", "gpu-a10-count", "gpu-a100-count"]
    values = [SimpleNamespace(name=n, availability_domain=ad, scope_type="AD", value=0)
              for n in names for ad in ads]
    values += [SimpleNamespace(name=n, availability_domain=ad, scope_type="AD", value=a1_value)
               for n in ("standard-a1-core-count", "standard-a1-memory-count") for ad in ads]
    return values


def test_gpu_limits_are_not_a1_limits(monkeypatch):
    """按子串 "a1" 过滤会把 GPU A10 / A100 的限额也收进来；排序后它们在前，截到 12 条时
    A1 自己那几条被挤掉 —— 配额表里只剩 GPU，「配额全是 0」的判断也就把一个正常的新号
    说成了「准备中」。"""
    s = _setup_session(monkeypatch, registered_days_ago=1)
    values = _gpu_heavy_limits(4)
    s._limits = SimpleNamespace(list_limit_values=lambda t, **_kw: SimpleNamespace(data=values))
    data = s.get_account_status().data
    assert {r["name"] for r in data["limits"]} == {"standard-a1-core-count", "standard-a1-memory-count"}
    assert data["setup"]["state"] == "READY"


def test_unknown_limit_values_are_not_zero(monkeypatch):
    """Oracle 没给数值的限额不能当 0 —— 那会把「不知道」算成「配额没下发」。"""
    s = _setup_session(monkeypatch, registered_days_ago=1)
    values = [SimpleNamespace(name="standard-a1-core-count", availability_domain="AD-1",
                              scope_type="AD", value=None)]
    s._limits = SimpleNamespace(list_limit_values=lambda t, **_kw: SimpleNamespace(data=values))
    assert s.get_account_status().data["setup"]["state"] != "PROVISIONING"


def test_a_sub_region_does_not_overrule_the_home_region_verdict(monkeypatch):
    """新号的「配额全是 0」只能在主区看。在副区行上读，判断做不了 —— 以前照样报「已就绪」，
    而状态是记到整个账号的，于是先点主区是「准备中」、再点副区就变回「已就绪」。"""
    s = _setup_session(monkeypatch, limits=(0.0, 0.0), registered_days_ago=1,
                       region="us-ashburn-1", home="ap-tokyo-1")
    setup = s.get_account_status().data["setup"]
    assert setup["state"] == ""
    assert "主区" in setup["note"], "说清楚为什么判断不了，而不是笼统的「读不到」"
    # 老账号不受影响：过了 7 天这条依据本来就不用。
    s = _setup_session(monkeypatch, limits=(0.0, 0.0), registered_days_ago=30,
                       region="us-ashburn-1", home="ap-tokyo-1")
    assert s.get_account_status().data["setup"]["state"] == "READY"


def test_pointing_a_tenant_at_another_account_forgets_the_cached_states(monkeypatch):
    """编辑租户换了 Tenancy OCID，就是换了一个 Oracle 账号；上一个账号的等级、升级状态、
    「准备中」都不再适用 —— 「准备中」排在标签最前面，留着会一直挂在一个正常的号上。"""
    from web.backend.crypto_util import encrypt_text
    from web.backend.db import Base
    from web.backend.models import Tenant, User
    from web.backend.routers import tenants as tr
    from web.backend.schemas import TenantUpdate

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(User(id="u1", username="u1", password_hash="x"))
    creds = dict(
        user_ocid="ocid1.user.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
        tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
        fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
        private_key_encrypted=encrypt_text(TEST_PEM),
        account_tier="paid", upgrade_state="UPGRADED", setup_state="PROVISIONING",
        registered_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        home_region="ap-tokyo-1",
    )
    now = datetime.now(timezone.utc)
    db.add(Tenant(id="p", owner_id="u1", name="p", region="ap-tokyo-1", created_at=now, updated_at=now, **creds))
    db.add(Tenant(id="c", owner_id="u1", name="c", region="ap-osaka-1", parent_tenant_id="p",
                  created_at=now, updated_at=now, **creds))
    db.commit()
    monkeypatch.setattr(tr, "drop_session", lambda _id: None)
    monkeypatch.setattr(tr, "clear_launch_meta_cache", lambda _id: None)
    user = SimpleNamespace(id="u1")

    # 只改名字：缓存照留。
    tr.update_tenant("p", TenantUpdate(name="p2"), user, db)
    assert db.get(Tenant, "p").setup_state == "PROVISIONING"
    assert db.get(Tenant, "p").home_region == "ap-tokyo-1"

    new_tenancy = "ocid1.tenancy.oc1..zzzzaaaabbbbccccddddeeeeffffgggghhhh"
    out = tr.update_tenant("p", TenantUpdate(tenancy_ocid=new_tenancy), user, db)
    for tid in ("p", "c"):
        row = db.get(Tenant, tid)
        assert (row.account_tier, row.upgrade_state, row.setup_state) == ("", "", ""), tid
        assert row.registered_at is None, tid
        # 主区也是上一个账号的：不清的话新会话会把它当「已确认的主区」用（0.4.168）。
        assert row.home_region == "", tid
    assert out.setup_state == "" and out.trial_expires_at is None
