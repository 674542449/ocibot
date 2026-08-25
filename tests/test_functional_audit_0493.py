"""第四轮功能审计（0.4.93）修掉的缺陷。

每条都是「代码跑得通、不报错，但结论是错的」那一类 —— 类型检查和冒烟测试都抓不到，
只能靠断言业务结论本身。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.oci_client import TenantSession
from web.backend.quota_guard import region_pair, resolve_secondary


# ---------------------------------------------------------------------------
# 1. 取消勾选「允许外网直接访问」时，托管 NSG 不能再是全开放
# ---------------------------------------------------------------------------


def test_the_managed_nsg_is_ssh_only_when_the_box_is_unchecked():
    """预期：不勾选时不放宽云端安全组（表单 hint 和确认框都是这么写的）。

    实际（修复前）：open_guest_firewall 只被送去决定要不要在**客体内**关掉
    ufw/iptables，创建托管 NSG 那段完全不看它，无条件写入
    INGRESS all 0.0.0.0/0。而 NSG 与 Security List 在 OCI 是并集，
    挂上之后子网上原有的收紧规则形同虚设。
    """
    specs = TenantSession._ssh_only_specs(include_ipv6=False)
    for spec in specs:
        spec.validate()

    ingress = [s for s in specs if s.direction == "INGRESS"]
    assert len(ingress) == 1
    assert ingress[0].protocol == "6", "只放 TCP"
    assert (ingress[0].port_min, ingress[0].port_max) == (22, 22), "只放 22"
    # 出站不限：机器要能下载软件包。
    assert any(s.direction == "EGRESS" and s.protocol == "all" for s in specs)


def test_the_open_all_rules_are_unchanged_when_the_box_is_checked():
    """勾选时行为不能变 —— 这是绝大多数用户的现状。"""
    specs = TenantSession._open_all_specs(include_ipv6=False)
    ingress = [s for s in specs if s.direction == "INGRESS"]
    assert len(ingress) == 1
    assert ingress[0].protocol == "all"
    assert ingress[0].cidr == "0.0.0.0/0"


def test_ipv6_variants_exist_for_both_rule_sets():
    for fn in (TenantSession._ssh_only_specs, TenantSession._open_all_specs):
        v6 = [s for s in fn(include_ipv6=True) if ":" in s.cidr]
        assert v6, fn.__name__
        for spec in v6:
            spec.validate()


def test_prepare_launch_network_defaults_to_open_when_the_key_is_absent():
    """老抢机任务存下的 payload 可能还没有这个键。

    用 False 兜底会把它们静默收窄成只放 SSH —— 用户的 web 服务某天重试成功后
    连不上，而没有任何地方说过规则变了。上游 build_launch_request 的默认值是 True，
    这里必须一致。
    """
    import inspect

    from web.backend import launch_service

    src = inspect.getsource(launch_service.prepare_launch_network)
    assert 'payload.get("open_guest_firewall", True)' in src, (
        "缺省必须是 True，和 build_launch_request 的 body.get(..., True) 一致"
    )


# ---------------------------------------------------------------------------
# 2. 区域订阅读失败不能被伪装成「当前就是主区」
# ---------------------------------------------------------------------------


class _Tenant:
    def __init__(self, region="ap-tokyo-1"):
        self.region = region
        self.tenancy_ocid = "ocid1.tenancy.oc1..t"
        self.compartment_ocid = ""
        self.account_tier = ""
        self.user_ocid = "ocid1.user.oc1..u"
        self.fingerprint = "aa:bb"
        self.private_key_pem = ""
        self.id = "t1"


def _session_with_subscription(*, raises: bool, home="ap-tokyo-1", current="us-ashburn-1"):
    s = TenantSession.__new__(TenantSession)
    s.tenant = _Tenant(region=current)

    def _list(_tenancy):
        if raises:
            raise RuntimeError("NotAuthorizedOrNotFound")
        return SimpleNamespace(
            data=[SimpleNamespace(is_home_region=True, region_name=home)]
        )

    s._identity = SimpleNamespace(list_region_subscriptions=_list)
    return s


def test_a_failed_subscription_read_does_not_claim_current_is_home():
    """预期：读不到主区时 region_pair 返回 ("", "")，让 resolve_secondary 退回
    DB 的 parent_tenant_id（这正是它 docstring 承诺的优先级）。

    实际（修复前）：_home_region() 读失败时回退到租户自己填的 region，于是
    current == home ——「读不到」被伪装成「当前就是主区」。副区闸门因此在最需要它的
    时候整段失效：一台**计费**机器被当成免费的放行。
    """
    session = _session_with_subscription(raises=True, current="us-ashburn-1")

    current, home = region_pair(session)

    assert (current, home) == ("", ""), "读失败必须表达成「读不到」"

    # 退回 DB hint：带 parent_tenant_id 的行仍然被认成副区。
    child = SimpleNamespace(parent_tenant_id="parent-1")
    assert resolve_secondary(session, child) is True
    plain = SimpleNamespace(parent_tenant_id="")
    assert resolve_secondary(session, plain) is False


def test_a_successful_subscription_read_is_used_normally():
    session = _session_with_subscription(raises=False, home="ap-tokyo-1", current="us-ashburn-1")

    current, home = region_pair(session)

    assert (current, home) == ("us-ashburn-1", "ap-tokyo-1")
    assert resolve_secondary(session, SimpleNamespace(parent_tenant_id="")) is True


def test_home_region_still_falls_back_for_callers_that_want_a_guess():
    """home_region() 的兜底行为不能改 —— 账单/用量那几个调用方要一个能用的值。
    只有 home_region_confirmed() 才严格。"""
    session = _session_with_subscription(raises=True, current="us-ashburn-1")

    assert session.home_region() == "us-ashburn-1", "兜底仍在"
    assert session.home_region_confirmed() == "", "但严格版必须承认读不到"


# ---------------------------------------------------------------------------
# 3. 副区判定只能有一处实现
# ---------------------------------------------------------------------------


def test_no_route_reimplements_the_secondary_check_inline():
    """预检和创建路径给出相反结论，是因为路由里内联了一份 `(读到且不同) or DB hint`。

    那个 `or` 让 DB hint 排在一次**成功**的读取之后仍能翻盘 —— 一个 region 恰好
    等于主区的子行会被判成副区：额度页显示「不适用 Always Free」，用户在自己的
    主区看不到任何数字；预检放行、真提交却 400。
    """
    import inspect

    from web.backend.routers import instances as inst

    for fn in (inst.free_quota, inst.launch_quota_check):
        src = inspect.getsource(fn)
        assert "resolve_secondary(" in src, fn.__name__
        assert "or tenant_is_secondary(row)" not in src, (
            f"{fn.__name__} 里还留着内联的副区判定"
        )


# ---------------------------------------------------------------------------
# 4. 会话缓存键必须包含 account_tier
# ---------------------------------------------------------------------------


def test_changing_account_tier_rebuilds_the_session():
    """预期：把 row.account_tier 从 free 改成 paid 之后，守卫按 paid 处理。

    实际（修复前）：_fp 不含 account_tier → 不重建会话 → get_free_quota_usage 从
    冻结的 self.tenant 读到旧等级并写进快照 → 守卫按旧等级判。两个方向都出事：
    free→paid 把付费租户硬拦（抢机任务被判成永久 failed，不是重试）；
    paid→free 在降级/试用到期后**静默放行**超额创建。
    """
    from app.oci_client import SessionManager

    free = _Tenant()
    free.account_tier = "free"
    paid = _Tenant()
    paid.account_tier = "paid"

    assert SessionManager._fp(free) != SessionManager._fp(paid)


def test_the_database_row_wins_over_the_snapshot_for_tier():
    """快照只是兜底。顺序反过来会让会话里冻结的旧等级压过刚从数据库读到的新等级。"""
    import inspect

    from web.backend import quota_guard

    for fn in (quota_guard.check_launch_quota, quota_guard.enforce_shape_resize_quota):
        src = inspect.getsource(fn)
        assert 'str(tier or usage.get("account_tier")' in src, fn.__name__


# ---------------------------------------------------------------------------
# 5. 开关 2FA 必须顶掉其它会话
# ---------------------------------------------------------------------------


def test_toggling_totp_bumps_token_version():
    """这个功能最常见的动机就是「有人已经进来了，我要把他赶走」。

    不 bump 的话 2FA 只挡新的登录，挡不住已经握在对方手里的那个 token。
    routers/audit.py 顶部的注释一直断言它会 bump —— 那句话在修复前是假的。
    """
    import inspect

    from web.backend.routers import auth

    for fn in (auth.totp_enable, auth.totp_disable):
        src = inspect.getsource(fn)
        assert "token_version" in src, f"{fn.__name__} 没有 bump token_version"


# ---------------------------------------------------------------------------
# 6. WebSSH 必须周期性重验凭据
# ---------------------------------------------------------------------------


def test_webssh_revalidates_credentials_during_the_session():
    """预期：logout-all / 改密码 / 管理员禁用账号之后，正在跑的 root 终端被切断。

    实际（修复前）：token_version 和 is_active 只在握手时读一次。此后唯一的时限是
    30 分钟**空闲**超时 —— 只要终端里有输出（top / tail -f）就永远不空闲，
    会话时长没有上界。管理员禁用一个被盗账号之后，那个账号的 root shell 继续存活。
    """
    import inspect

    from web.backend.routers import webssh

    src = inspect.getsource(webssh.webssh_endpoint)
    assert "_idle_watch" in src
    # 看门狗里要真的去查库，而不是只看空闲时间。
    idle = src[src.index("async def _idle_watch"):]
    idle = idle[: idle.index("async def _write_stdin")]
    assert "SessionLocal" in idle, "看门狗里没有重新读用户"
    assert "token_version" in idle
    assert "is_active" in idle


# ---------------------------------------------------------------------------
# 7. 登录限流键必须用归一化后的用户名
# ---------------------------------------------------------------------------


def test_the_login_rate_limit_key_is_normalized():
    """查找侧有 NFKC 回退（全角 ａdmin 能命中 admin），限流键却用原始串 ——
    换一种等价写法就是一个新桶，10 次/5 分钟被放大成任意次。"""
    import inspect

    from web.backend.routers import auth

    src = inspect.getsource(auth.login)
    assert "normalize_username(username)" in src, "限流键没有归一化"


def test_normalization_actually_collapses_the_variants():
    from web.backend.routers.auth import normalize_username

    assert normalize_username("ａdmin").lower() == "admin"
    assert normalize_username("  admin  ").lower() == "admin"


# ---------------------------------------------------------------------------
# 8. 抢机丢掉自定义 cloud-init 时必须留痕
# ---------------------------------------------------------------------------


def test_a_lost_user_data_script_is_reported_not_swallowed():
    """预期：user_data 解不开时，不能开出一台没跑过用户脚本的机器还宣布成功。

    实际（修复前）：except 只 log 一行，custom_user_data 保持 ""，然后照常 launch。
    「这段数据还原不回来」和「用户没填」在后续代码里完全不可区分。
    任务是绿的、尝试日志是 ok、通知里只字不提。
    """
    import inspect

    from web.backend import worker

    src = inspect.getsource(worker.Worker._run_capacity_once)
    assert "user_data_lost" in src
    # 三处都要出现：赋值、尝试日志、通知正文。
    assert src.count("user_data_lost") >= 3, "留痕不完整"
    assert "自定义启动脚本无法解密" in src


# ---------------------------------------------------------------------------
# 9. 对象存储读失败要置 read_incomplete
# ---------------------------------------------------------------------------


def test_a_failed_object_storage_read_marks_the_snapshot_incomplete():
    """读不到不能等于「用了 0」。read_incomplete 会一路传到前端显示
    「读取不完整」，而不是让人看着一个权威的错数字。
    同时那条 note 以前会被 append 两次。"""
    import inspect

    from app.oci_client import TenantSession as TS

    src = inspect.getsource(TS.get_free_quota_usage)
    block = src[src.index("object_usage"):]
    block = block[: block.index("egress_usage")]
    assert "read_incomplete = True" in block, "对象存储读失败没有标记快照不完整"
    assert block.count("notes.append(est.message)") == 1, "同一条 note 被追加了两次"


# ---------------------------------------------------------------------------
# 10. 被删掉的路由不能变成白屏
# ---------------------------------------------------------------------------


def test_the_router_has_a_catch_all():
    """vue-router 4 对匹配不到的路径既不抛错也不回退首页 —— 它完成一次 matched
    为空的导航，<router-view/> 渲染 null，整页只剩一个注释节点：连导航栏都没有。

    0.4.92 删掉 /radar 之后，书签、开着的标签页、以及登录跳转的 redirect
    里仍然带着那个 URL。
    """
    import pathlib

    src = pathlib.Path("web/frontend/src/router/index.ts").read_text(encoding="utf-8")
    assert "pathMatch(.*)" in src, "没有 catch-all 路由"
    idx_catch = src.index("pathMatch(.*)")
    idx_children = src.index("children:")
    assert idx_catch > idx_children, "catch-all 必须在顶层数组，不能在 children 里"
