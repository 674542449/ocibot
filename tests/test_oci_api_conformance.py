"""按甲骨文官方文档逐参数校验 OCI 调用（0.4.95 修掉的那批）。

每条都有文档原文支撑 —— 这一轮的规则是「没有原文就不算数」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.oci_client import TenantSession, _vpus_or_default

oci = pytest.importorskip("oci")


# ---------------------------------------------------------------------------
# 1. ListCompartments：compartment_id_in_subtree 只在租户根上有效
# ---------------------------------------------------------------------------
#
# SDK docstring（内容即 API spec 原文）：
#   "With the exception of the tenancy (root compartment), the ListCompartments
#    operation returns only the first-level child compartments in the parent
#    compartment specified in compartmentId. The list does not include any
#    subcompartments of the child compartments (grandchildren)."
#   ":param bool compartment_id_in_subtree: Default is false. Can only be set to
#    true when performing ListCompartments on the tenancy (root compartment)."


def _tree_session(tree: dict, tenancy="ocid1.tenancy.oc1..T", root="A"):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        tenancy_ocid=tenancy, compartment_ocid=root, region="ap-tokyo-1", id="t1"
    )
    s.calls = []

    def children(cid, *, in_subtree=False):
        s.calls.append((cid, in_subtree))
        return [
            SimpleNamespace(id=i, name=i, description="", lifecycle_state=st)
            for i, st in tree.get(cid, [])
        ]

    s._compartment_children = children
    return s


def test_a_grandchild_compartment_is_reached():
    """这就是被修掉的漏数：对非根传 subtree=True 会被**静默忽略**（HTTP 200、
    只回一层），孙层里的实例/卷全部计为 0，而 read_incomplete 是 False ——
    配额守卫拿着一份少算的用量放行一台**计费**机器。"""
    s = _tree_session({"A": [("A1", "ACTIVE")], "A1": [("A1x", "ACTIVE")]})

    out, truncated = s._walk_compartment_subtree("A")

    assert [c.id for c in out] == ["A1", "A1x"], "孙层必须被走到"
    assert truncated is False


def test_deleting_compartments_are_skipped_and_not_descended():
    """文档：删 compartment 前必须先清空所有资源（含子 compartment），
    所以 DELETING 里没有可计数的东西；删除失败还会回到 ACTIVE，下次自然收回来。

    注意 CREATING / INACTIVE **不能**跳过 —— 它们可以持有资源。
    """
    s = _tree_session(
        {"A": [("B", "DELETING"), ("C", "CREATING")], "B": [("Bx", "ACTIVE")]}
    )

    out, _ = s._walk_compartment_subtree("A")

    ids = [c.id for c in out]
    assert "C" in ids, "CREATING 可以持有资源，不能跳过"
    assert "B" not in ids and "Bx" not in ids, "DELETING 跳过且不下钻"
    assert ("B", False) not in s.calls, "不该为 DELETING 的 compartment 再发一次调用"


def test_a_cycle_terminates_instead_of_looping_forever():
    """OCI 的 compartment 是树，但我没找到一句官方文档保证「不能移到自己的后代下」。
    所以不依赖无环 —— seen 集合 + 深度上限让任何环结构都必然终止。"""
    s = _tree_session({"X": [("X", "ACTIVE")]}, root="X")

    out, truncated = s._walk_compartment_subtree("X")

    assert out == []
    assert len(s.calls) == 1


def test_depth_is_bounded_and_reports_truncation():
    """Oracle: "Maximum nested compartment hierarchy levels: 6"。

    最关键的是 truncated 这一位：撞上限必须上报成「没读全」，
    **绝不能**当成读全了 —— 那正是这个 bug 的本体。
    """
    deep = {f"L{i}": [(f"L{i + 1}", "ACTIVE")] for i in range(20)}
    s = _tree_session(deep, root="L0")

    out, truncated = s._walk_compartment_subtree("L0")

    assert truncated is True, "走不完必须上报"
    assert len(out) == TenantSession._MAX_COMPARTMENT_DEPTH


def test_the_root_path_still_uses_one_call():
    """租户根上 subtree=True 是**合法**的，保留单次调用的快路径，行为不变。"""
    s = _tree_session({"ocid1.tenancy.oc1..T": [("A", "ACTIVE")]})

    s.list_compartments(parent_id="ocid1.tenancy.oc1..T", subtree=True)

    assert s.calls == [("ocid1.tenancy.oc1..T", True)], s.calls


def test_a_non_root_never_passes_the_subtree_flag():
    """非根传这个参数是文档明说无效的用法 —— 不该再发出去。"""
    s = _tree_session({"A": [("A1", "ACTIVE")]})

    s.list_compartments(parent_id="A", subtree=True)

    assert all(in_subtree is False for _cid, in_subtree in s.calls), s.calls


# ---------------------------------------------------------------------------
# 2. Usage API：空结果是免费账号的常态，不是错误
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, data):
        self.data = data


def _usage_session(items):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        region="us-phoenix-1", tenancy_ocid="ocid1.tenancy.oc1..t",
        compartment_ocid="", name="T", id="t1",
    )
    s._home_region_name = "us-phoenix-1"
    s._home_region_resolved = True
    agg = oci.usage_api.models.UsageAggregation(group_by=["service", "currency"], items=items)
    s._usage = SimpleNamespace(request_summarized_usages=lambda d, **k: _Resp(agg))
    return s


def test_an_empty_usage_result_is_ok_not_a_crash():
    """原来那行写成 `list(data.items ...) or list(data or [])`：items 为空时第一段
    得到 []（假值），`or` 于是去求值第二段 —— 而 UsageAggregation 不可迭代，
    直接 TypeError。函数自己 docstring 里那句「Free accounts often have no usage
    data … returns ok with empty series」那条分支**永远走不到**。"""
    r = _usage_session([]).get_usage_summary(days=30)

    assert r.ok is True
    assert "暂无账单数据" in r.message


def test_unit_price_is_never_counted_as_cost():
    """SDK 字段说明：computed_amount = "The computed cost."，
    unit_price = "The price per unit." —— 两者差一个用量系数。

    computedAmount 为 null 最典型的场景恰恰是「免费额度内的用量」，
    拿单价顶上会让账单页凭空多出一笔钱。
    """
    row = oci.usage_api.models.UsageSummary(
        service="Compute", currency="USD",
        computed_amount=None, attributed_cost=None, unit_price=0.0255,
        time_usage_started=None, time_usage_ended=None,
    )

    r = _usage_session([row]).get_usage_summary(days=30)

    assert (r.data or {}).get("total") == 0.0


# ---------------------------------------------------------------------------
# 3. ListBuckets 不返回 publicAccessType —— 不能替它下结论
# ---------------------------------------------------------------------------


def test_bucket_summary_really_lacks_the_access_field():
    """先钉住事实本身，否则下面那条断言看不出意义。"""
    summary = oci.object_storage.models.BucketSummary()
    full = oci.object_storage.models.Bucket()

    assert not hasattr(summary, "public_access_type"), "列表接口返回的是 BucketSummary"
    assert hasattr(full, "public_access_type"), "只有单个 GetBucket 才有"


def test_the_bucket_list_does_not_assert_access_level():
    """以前取到空串、前端再 `|| 'NoPublicAccess'` 兜底，于是**每一个桶都被断言成
    「不公开」** —— 包括真正对公网开放读取的那些，而且断言的方向恰好是让人放心那边。"""
    import inspect

    src = inspect.getsource(TenantSession.list_buckets)

    assert '"public_access_type": None' in src
    assert 'getattr(b, "public_access_type"' not in src


# ---------------------------------------------------------------------------
# 4. vpus_per_gb：0 是合法值（Lower Cost 档）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expect",
    [
        (0, 0),        # Lower Cost —— 以前被 `0 or 10` 变成 10
        (10, 10),
        (20, 20),
        (120, 120),
        (None, 10),    # 字段真的没返回才退回默认
        ("", 10),
        ("x", 10),
    ],
)
def test_vpus_zero_survives(raw, expect):
    assert _vpus_or_default(raw) == expect


def test_zero_is_accepted_by_the_resize_validator():
    """0（低成本）是 Oracle 的合法档次，以前这条校验直接把它拒掉。"""
    import inspect

    src = inspect.getsource(TenantSession.resize_boot_volume)
    assert "(0, 10, 20)" in src


# ---------------------------------------------------------------------------
# 5. 主区解析失败不该被永久缓存
# ---------------------------------------------------------------------------


def test_a_failed_home_region_read_is_not_cached_forever():
    """0.4.93 加 resolved 标志时把它和 region 一起无条件缓存了，于是一次瞬时的
    读取失败会被这个 session 永久记住 —— 副区闸门也就一直退回 DB hint。"""
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        region="ap-tokyo-1", tenancy_ocid="ocid1.tenancy.oc1..t", id="t1"
    )
    state = {"fail": True}

    def _list(_tenancy):
        if state["fail"]:
            raise RuntimeError("throttled")
        return SimpleNamespace(
            data=[SimpleNamespace(is_home_region=True, region_name="us-ashburn-1")]
        )

    s._identity = SimpleNamespace(list_region_subscriptions=_list)

    assert s.home_region_confirmed() == "", "读不到就是读不到"

    # 抖动过去了 —— 下一次必须重新读，而不是记着那次失败。
    state["fail"] = False
    assert s.home_region_confirmed() == "us-ashburn-1"


# ---------------------------------------------------------------------------
# 6. 区域订阅：记录存在 ≠ 可以用了
# ---------------------------------------------------------------------------


def test_an_in_progress_subscription_is_not_reported_as_ready():
    """status 为 IN_PROGRESS 时资源还建不出来。把它当成已开通，用户会拿着一个
    未就绪的区域去创建，然后收到一个和区域订阅毫无关系的报错。"""
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        tenancy_ocid="ocid1.tenancy.oc1..t", region="ap-tokyo-1", id="t1"
    )
    subs = [
        SimpleNamespace(region_name="ap-osaka-1", region_key="KIX",
                        status="IN_PROGRESS", is_home_region=False),
    ]
    s._identity = SimpleNamespace(
        list_region_subscriptions=lambda _t: SimpleNamespace(data=subs)
    )

    rows = (s.list_subscribed_regions().data or {}).get("regions") or []

    assert rows and rows[0]["status"] == "IN_PROGRESS"
    assert rows[0]["ready"] is False, "只有 READY 才是真的可用"


def test_a_ready_subscription_is_marked_ready():
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        tenancy_ocid="ocid1.tenancy.oc1..t", region="ap-tokyo-1", id="t1"
    )
    subs = [
        SimpleNamespace(region_name="ap-tokyo-1", region_key="NRT",
                        status="READY", is_home_region=True),
    ]
    s._identity = SimpleNamespace(
        list_region_subscriptions=lambda _t: SimpleNamespace(data=subs)
    )

    rows = (s.list_subscribed_regions().data or {}).get("regions") or []
    assert rows[0]["ready"] is True
