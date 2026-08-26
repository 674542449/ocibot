"""实例**详情**页刷新的间歇性 404 —— 以及为什么上一轮的修复没盖到它。

用户报的原话：详情页右上角点「刷新」，报
`[404] NotAuthorizedOrNotFound`，而「我能保证 API 信息是正确可用的」。

## 先把范围收死：这个红框只可能来自一个调用

`refreshAll()` = `loadInstance()` + `loadCurrentTab()`。而 `loadCurrentTab` 的
每一个分支（loadMetrics / loadConsole / loadFirewall / loadReservedIps /
loadBoot / loadBackups）在前端**各自 catch**，只写自己那一块的局部消息。
唯一没有自己 catch、异常会冒泡到 `refreshAll` 的 catch 并写进 `error.value`
（页面顶部那个红框）的，只有 `loadInstance()`。

`loadInstance` → `GET /tenants/{t}/instances/{i}` → `session.get_instance()`
→ `compute.get_instance(ocid)`。

所以这条 404 出自**按 OCID 直读实例**。这条路径上没有任何可疑之处：
OCID 来自 URL，region 绑在租户行上（副区是独立的租户行，不是 per-request 覆盖），
`resolve_compartment()` 不发网络请求、也没有「失败就退回 root」的分支，
`_enrich_instances_parallel` 的失败全被 `except Exception: pass` 吞掉、到不了红框。

## 所以根因在 Oracle 那边，而我们的缺陷是「说不清」

错误里带着 `opc-request-id`，说明那是一次**真实的 HTTP 往返**拿回来的服务端 404，
不是本地熔断器异常（CircuitBreakerError 没有 request id）。满权限 + 间歇性，
剩下能解释的只有 Oracle 侧的瞬时授权失败 —— 而 Oracle 自己的文档说 IAM 策略生效
有「several minutes」的传播延迟。

面板这边真正的 bug 是：
  1. 错误不说是**哪个调用**失败的（operation_name / 端点主机名本来就在
     ServiceError 上放着，一直被丢掉）；
  2. 整个 app/oci_client.py **一行日志都没有**，运维事后查不到；
  3. 列表路径在 0.4.96 拿到了「404 复读一次取证据」，详情路径没有。
"""

from __future__ import annotations

import re

import pytest

from app.oci_client import (
    _is_ambiguous_404,
    _scrub_ocids,
    persistent_404_note,
    read_with_404_evidence,
    safe_error_text,
    transient_404_note,
)

oci = pytest.importorskip("oci")
from oci.exceptions import ServiceError  # noqa: E402


_FULL_OCID = "ocid1.compartment.oc1..aaaaaaaaSECRETTAILabcdefgh"


# ------------------------------------------------------------------ 可归因性


def test_the_error_says_which_call_failed():
    """用户贴出来的那条错误里没有任何东西指向失败的调用：没有操作名、没有服务端点。
    于是连面板自己都只能靠反推调用链来猜 —— 这就是这一轮花掉的大部分时间。"""
    from app.oci_client import _format_service_error

    exc = ServiceError(
        404,
        "NotAuthorizedOrNotFound",
        {"opc-request-id": "ABC/DEF/GHI"},
        "Authorization failed or requested resource not found.",
        operation_name="get_instance",
        request_endpoint=f"GET https://iaas.us-phoenix-1.oraclecloud.com/20160918/instances?compartmentId={_FULL_OCID}",
    )
    text = _format_service_error(exc)

    assert "get_instance" in text, "看不出是哪个调用失败的"
    assert "iaas.us-phoenix-1.oraclecloud.com" in text, "看不出打的是哪个服务端点"
    assert "opc-request-id: ABC/DEF/GHI" in text
    # 端点**只能**取主机名 —— request_endpoint 原文里带着未脱敏的完整 compartment OCID。
    assert "SECRETTAIL" not in text, "完整 compartment OCID 泄漏进了用户可见的文本"


def test_every_oci_failure_leaves_a_server_side_trace(caplog):
    """在这之前 app/oci_client.py 一行日志都没有 —— 一次失败只要没冒泡到界面
    就彻底消失。用户报错时，那条错误唯一存在的地方是他浏览器里的红框。"""
    import logging

    from app.oci_client import _format_service_error

    exc = ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope", operation_name="get_instance")
    with caplog.at_level(logging.WARNING, logger="ocibot.oci"):
        _format_service_error(exc)

    assert any("get_instance" in r.getMessage() for r in caplog.records), caplog.text


# ------------------------------------------------------------------ 复读取证


def test_the_detail_read_re_reads_a_404_and_says_so():
    """0.4.96 只给列表路径加了复读。详情页是同一个用户、同一种症状，却没有。"""
    from app.oci_client import TenantSession

    src = __import__("inspect").getsource(TenantSession.get_instance)
    assert "read_with_404_evidence" in src
    assert "transient_404_note" in src
    assert "persistent_404_note" in src


def test_the_re_read_is_off_by_default_so_one_refresh_does_not_triple_its_requests():
    """同一次刷新里 get_instance 会被调好几遍（详情路由一次、监控路由为了拿
    compartment_id 又一次、引导卷页更多）。全都复读的话，一次失败的刷新要打三倍
    请求、多等十几秒 —— 而其中只有**一次**的错误是用户看得见的，其余几次的异常
    在前端就被各自的 catch 吃掉了。给看不见的失败付重试代价没有意义。"""
    import inspect

    from app.oci_client import TenantSession

    sig = inspect.signature(TenantSession.get_instance)
    assert sig.parameters["reread_on_404"].default is False

    # 而那一次「失败会直接进红框」的读必须显式打开。
    import pathlib

    routes = pathlib.Path("web/backend/routers/instances.py").read_text(encoding="utf-8")
    assert "reread_on_404=True" in routes
    assert routes.count("reread_on_404=True") == 1, "只有详情路由那一次该开"


def test_a_transient_recovery_is_reported_not_silently_swallowed():
    """复读成功后页面就正常了 —— 如果什么都不说，用户的困惑（「满权限为什么时好
    时坏」）原封不动，这次修复对他就是隐形的。"""
    note = transient_404_note("[404] NotAuthorizedOrNotFound", 1)
    assert "瞬时" in note and "无关" in note

    import pathlib

    routes = pathlib.Path("web/backend/routers/instances.py").read_text(encoding="utf-8")
    assert "X-Ocibot-Reread" in routes
    main = pathlib.Path("web/backend/main.py").read_text(encoding="utf-8")
    assert "X-Ocibot-Reread" in main, "自定义响应头不 expose，跨源部署下前端读不到"
    ui = pathlib.Path("web/frontend/src/views/InstanceDetailView.vue").read_text(encoding="utf-8")
    assert "x-ocibot-reread" in ui


def test_the_note_lives_on_the_result_not_on_the_shared_session():
    """TenantSession 是**进程级缓存**的（web/backend/oci_bridge.py 的 SessionManager），
    同一个租户的并发请求拿到同一个对象。把这条说明写在 session 上，A 请求的
    「本次是瞬时故障」会被 B 请求覆盖，或者更糟 —— B 把 A 的提示挂在一个根本
    没出错的页面上。"""
    from app.oci_client import InstanceInfo, TenantSession

    assert "read_note" in InstanceInfo.__dataclass_fields__
    src = __import__("inspect").getsource(TenantSession.get_instance)
    assert "info.read_note" in src
    assert "self._last_read_note" not in src


def test_the_persistent_note_does_not_over_claim():
    """复读窗口只有几秒，而 Oracle 的授权最终一致性窗口是「several minutes」。
    据此断言「这不是瞬时故障」就是又一次替 Oracle 下它没给我们依据的结论 ——
    而且会和同一条错误里第 4 条提示（「这个码不保证是永久的」）直接打架。"""
    note = persistent_404_note(2)
    assert "这不是瞬时故障" not in note
    assert "不足以排除" in note


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (404, "NotAuthorizedOrNotFound", True),
        (404, "NotFound", True),
        (429, "TooManyRequests", False),
        (401, "NotAuthenticated", False),
        (500, "InternalServerError", False),
    ],
)
def test_only_the_ambiguous_404_is_treated_as_worth_re_reading(status, code, expected):
    exc = ServiceError(status, code, {}, "x")
    assert _is_ambiguous_404(exc) is expected


def test_the_re_read_counts_calls_not_truthiness_of_the_result():
    """一个**成功但为空**的读不是失败。列表路径原来写的是 `if again:`，于是
    「这个 compartment 读得到、只是一台实例都没有」会被判成复读失败，照样抛 404。"""
    calls = []
    ok, value, err, rereads = read_with_404_evidence(lambda: calls.append(1) or [])
    assert ok is True and value == [] and err is None and rereads == 0


# ------------------------------------------------------------------ 泄漏与熔断


def test_a_tripped_breaker_does_not_leak_the_full_compartment_ocid():
    """CircuitBreakerError 不是 ServiceError 的子类，所以各处 `except ServiceError`
    都接不住它，最后落到 `except Exception -> str(exc)`。而它的 __str__ 是

        'Circuit "%s" OPEN until %s (... ) (last_failure: %r)'

    那个 %r 是被熔断的 ServiceError 的 repr，而 ServiceError.__init__ 把
    request_endpoint 整条塞进了 args —— 里面带着未脱敏的完整 compartment OCID。
    面板到处只显示 compartment[-16:] 正是为了防这件事。
    """
    from circuitbreaker import CircuitBreaker, CircuitBreakerError

    inner = ServiceError(
        404,
        "NotAuthorizedOrNotFound",
        {},
        "nope",
        request_endpoint=f"GET https://iaas.us-phoenix-1.oraclecloud.com/20160918/instances?compartmentId={_FULL_OCID}",
    )
    breaker = CircuitBreaker(name="probe")
    breaker._last_failure = inner
    err = CircuitBreakerError(breaker)

    # 先证明这个坑是真的：裸 str() 确实把完整 OCID 吐出来。
    assert "SECRETTAIL" in str(err), "前提变了，这条测试要重新写"

    safe = safe_error_text(err)
    assert "SECRETTAIL" not in safe
    # 而且换成一句说人话、且**正确**的解释。
    assert "熔断" in safe
    assert "IAM" in safe and "无关" in safe


def test_scrubbing_keeps_enough_to_identify_the_resource():
    """脱敏不能脱到没法排查 —— 类型和末几位要留着。"""
    out = _scrub_ocids(f"compartmentId={_FULL_OCID}")
    assert "ocid1.compartment" in out
    assert "abcdefgh" in out
    assert "SECRETTAIL" not in out


def test_routers_do_not_hand_raw_exception_text_to_the_browser():
    """43 处 `HTTPException(502, detail=str(exc))` 全都绕过了脱敏。"""
    import pathlib

    for name in ("instances", "instance_ops", "storage", "tenants"):
        src = pathlib.Path(f"web/backend/routers/{name}.py").read_text(encoding="utf-8")
        assert "detail=str(exc)" not in src, f"{name}.py 还在直接外发裸异常文本"


# ------------------------------------------------------------------ 监控页诚实


def test_a_failed_metrics_read_is_not_reported_as_missing_plugin():
    """监控是详情页的**默认**标签页。四条查询原来一律 `except: series[key] = []`
    然后无条件 ok=True、消息写死成「暂无监控数据（实例需启用计算代理/监控插件）」。
    于是一次 404、一次限流、或者熔断器打开，给用户的结论都是「去实例里装监控插件」
    —— 又一件把人指向一个本来没坏的东西的事。"""
    import inspect

    from app.oci_client import TenantSession

    src = inspect.getsource(TenantSession.get_instance_metrics)
    assert "read_errors" in src
    assert "监控数据读取失败" in src
    # 「确实没数据」那句话必须还在 —— 它在真的没数据时是对的。
    assert "暂无监控数据" in src


# ------------------------------------------------------------------ 分页


def test_no_pagination_call_passes_retry_strategy_twice():
    """`f(**kwargs, retry_strategy=...)` 在 kwargs 已经含 retry_strategy 时是
    `TypeError: got multiple values` —— 而且只在运行时炸。这一轮加分页限流时
    真的踩了一次（_compartment_children 的 kwargs 里本来就有）。"""
    import pathlib

    src = pathlib.Path("app/oci_client.py").read_text(encoding="utf-8")

    def _close(s: str, start: int) -> int:
        depth, i = 0, start
        while i < len(s):
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        raise AssertionError("unbalanced")

    for m in re.finditer(r"oci\.pagination\.list_call_get_all_results\(", src):
        call = src[m.start() : _close(src, m.end() - 1) + 1]
        line = src[: m.start()].count("\n") + 1
        kw_names = re.findall(r"\*\*(\w+)", call)
        if "retry_strategy=" in call and kw_names:
            # 显式传了，同时又展开了一个 dict：那个 dict 不能也含 retry_strategy。
            for kw in kw_names:
                assert f'{kw}["retry_strategy"]' not in src and (
                    f'"retry_strategy": ' not in src.split(f"{kw} = ", 1)[-1][:600]
                ), f"line {line}: {kw} 里可能已经有 retry_strategy"


def test_every_pagination_call_is_bounded():
    """`oci.pagination.list_call_get_all_results` 自己**硬编码**又套了一层
    DEFAULT_RETRY_STRATEGY，和 client 层那个相乘：最坏 8×8=64 次真实调用、
    ~600 秒卡在一个 HTTP 请求里，而且在跟抢机重试循环抢同一个限流额度。"""
    import pathlib

    src = pathlib.Path("app/oci_client.py").read_text(encoding="utf-8")

    def _close(s: str, start: int) -> int:
        depth, i = 0, start
        while i < len(s):
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        raise AssertionError("unbalanced")

    missing = []
    for m in re.finditer(r"oci\.pagination\.list_call_get_all_results\(", src):
        call = src[m.start() : _close(src, m.end() - 1) + 1]
        line = src[: m.start()].count("\n") + 1
        if "sdk_bounded_paged_retry_strategy" in call:
            continue
        # 也可能是通过一个提前建好的 kwargs 传进去的。
        kw_names = re.findall(r"\*\*(\w+)", call)
        if kw_names and "sdk_bounded_paged_retry_strategy" in src:
            tail = src[: m.start()]
            block = tail[max(0, len(tail) - 1200) :]
            if "sdk_bounded_paged_retry_strategy" in block:
                continue
        missing.append(line)
    assert not missing, f"这些分页调用仍是双层重试：{missing}"


# ------------------------------------------------------------------ 失效的闸门


def test_reading_the_region_list_marks_the_home_region_as_confirmed():
    """`list_subscribed_regions()` 会顺手把主区写进 `_home_region_name` 缓存，
    但**没有**同时把 `_home_region_resolved` 置 True。而 `_home_region()` 开头是
    `if cached: return cached` —— 于是它永远走不到设置 resolved 的那几行，
    `home_region_confirmed()` 对这个 session 永远返回 ""。

    后果不是显示问题：quota_guard.region_pair 因此拿到 ("", "")，
    resolve_secondary 退回 DB 的 parent_tenant_id hint，而手工添加的副区租户
    没有那个字段 —— 副区闸门静默失效，一台**计费**机器会被当成免费的放行。
    """
    import inspect

    from app.oci_client import TenantSession

    src = inspect.getsource(TenantSession.list_subscribed_regions)
    assert "_home_region_name = home" in src
    assert "_home_region_resolved = True" in src, (
        "只写了缓存没写「问出来了」标志 —— home_region_confirmed() 会永远返回空"
    )
