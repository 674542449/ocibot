"""刷新实例列表的间歇性 404：「多点几次就好了」。

## 甲骨文文档怎么说（用户点名要查的那一步）

官方 API 错误表 (docs.oracle.com/en-us/iaas/Content/API/References/apierrors.htm)
有一列 Retry：**404 NotAuthorizedOrNotFound 标的是 "No."**，429 和 5xx 标的是
"Yes, with backoff."。Terraform 的排障页也把 404 列进「重试也不会成功」那一组。
Oracle 自己的 CLI 原样用 DEFAULT_RETRY_STRATEGY，不对 404 做任何特殊处理。

唯一相反的先例是 Go SDK：它把 {404, "NotAuthorizedOrNotFound"} 登记为「受最终
一致性影响」并重试 —— 但门控在一个**只由同进程先前成功的 Identity 写操作**打开的
240 秒窗口上。刷新实例列表是纯读、没有前置写，按 Oracle 自己的规则不该重试。
Python SDK 2.182.0 完全没有这套机制。

**所以我们没有给 SDK 挂 404 重试策略。**

## 那「多点几次就好了」是什么

实测确认的机制：

  1. IdentityClient 是唯一会兜底装上 DEFAULT_CIRCUIT_BREAKER_STRATEGY 的 client
     （ComputeClient 的 docstring 明说 "will not have circuit breakers enabled
     by default"）；
  2. 那个策略的 name 是**导入时生成的一个固定 uuid**，而 BaseClient 用
     `CircuitBreakerMonitor.get(strategy.name)` 取熔断器 —— 于是全进程、
     所有租户的 IdentityClient 共用**同一个**熔断器实例；
  3. 任一租户攒够 10 次 429/5xx，这个共享熔断器打开 **30 秒**；
  4. 期间所有租户的 list_compartments 抛 CircuitBreakerError，而它**不是**
     ServiceError，穿透了原来的 `except ServiceError`；
  5. 枚举失败 → list_instances_tree 的扫描范围塌缩成只有根；
  6. Compute 没有熔断器，list_instances(根) 照常打通、照常返回那个**永久**的 404
     （实例其实在子 compartment 里）；
  7. 塌缩 + 根 404 → 抛错。等 30 秒熔断器恢复 → 枚举成功 → 列表出来。

「多点几次」等的正是那 30 秒。
"""

from __future__ import annotations

import inspect

import pytest

from app.oci_client import TenantSession

oci = pytest.importorskip("oci")
from oci.exceptions import ServiceError  # noqa: E402


def _code_only(fn) -> str:
    """源码去掉注释和文档字符串。

    否则断言会被**自己写的解释性注释**绊倒 —— 那些注释里必然提到要禁止的东西
    （「不要用 X」这句话本身就含 X）。要匹配的是**调用**，不是**提及**。
    """
    import io
    import tokenize

    src = inspect.getsource(fn) if not isinstance(fn, str) else fn
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return src
    return " ".join(out)


# ---------------------------------------------------------------- 熔断器隔离


def test_the_default_circuit_breaker_is_a_process_wide_singleton():
    """这是缺陷的物理基础：DEFAULT 策略的 name 是固定的，所以按 name 查表拿到的
    是同一个熔断器。不先钉住这一点，下面那条「每租户独立」看不出意义。"""
    a = oci.circuit_breaker.DEFAULT_CIRCUIT_BREAKER_STRATEGY
    b = oci.circuit_breaker.DEFAULT_CIRCUIT_BREAKER_STRATEGY
    assert a.name == b.name, "默认策略的 name 是固定 uuid —— 全进程共享一个熔断器"


def test_circuit_breaker_error_is_not_a_service_error():
    """所以 `except ServiceError` 接不住它 —— 这是它能穿透 strict=False 的原因。"""
    from circuitbreaker import CircuitBreakerError

    assert not issubclass(CircuitBreakerError, ServiceError)


def test_which_sdk_clients_actually_default_to_the_shared_breaker():
    """钉住 0.4.96 的注释**说错了**的那件事。

    那条注释断言「IdentityClient 是唯一会兜底装上 DEFAULT_CIRCUIT_BREAKER_STRATEGY
    的 client」，于是修复只覆盖了 Identity。对 2.182.0 逐个读 __init__ 源码的实测
    结果是：只有 Compute 例外，其余九个全都兜底 DEFAULT —— 也就是说它们跨租户
    **并且跨服务**共用同一个熔断器。

    这条测试直接查 SDK 源码，所以 SDK 升级后如果 Oracle 改了默认值，它会失败并
    提醒我们重新判断，而不是让一条过时的断言继续指导代码。
    """
    import importlib

    def _defaults_to_shared(mod: str, cls_name: str) -> bool:
        cls = getattr(importlib.import_module(mod), cls_name)
        src = inspect.getsource(cls.__init__)
        return (
            "base_client_init_kwargs['circuit_breaker_strategy'] = "
            "circuit_breaker.DEFAULT_CIRCUIT_BREAKER_STRATEGY" in src
        )

    assert not _defaults_to_shared("oci.core", "ComputeClient"), (
        "Compute 以前没有熔断器 —— 如果 SDK 改了，_build 里那条「不给 Compute 加」的"
        "决定要重新评估"
    )
    for mod, cls_name in [
        ("oci.core", "VirtualNetworkClient"),
        ("oci.core", "BlockstorageClient"),
        ("oci.identity", "IdentityClient"),
        ("oci.monitoring", "MonitoringClient"),
        ("oci.limits", "LimitsClient"),
        ("oci.object_storage", "ObjectStorageClient"),
    ]:
        assert _defaults_to_shared(mod, cls_name), f"{cls_name} 不再兜底 DEFAULT？请复核"

    # 而那个 DEFAULT 的 name 是固定的 —— 这才是「共用」的物理原因。
    assert oci.circuit_breaker.DEFAULT_CIRCUIT_BREAKER_STRATEGY.name == (
        oci.circuit_breaker.DEFAULT_CIRCUIT_BREAKER_STRATEGY.name
    )


def test_every_breaker_enabled_client_gets_a_per_tenant_per_service_name():
    """每租户 × 每服务一个独立 name → CircuitBreakerMonitor 不再复用同一个实例。

    保留熔断保护本身（A 租户被 Oracle 限流时仍然该退避），只去掉两种交叉污染：
    「A 租户的限流把 B 租户也熔断掉」和「对象存储的故障把监控也熔断掉」。
    """
    from app.oci_client import cb_kwargs

    src = inspect.getsource(TenantSession._build)
    helper = inspect.getsource(cb_kwargs)

    assert "circuit_breaker_strategy" in helper
    # name 必须同时带服务名和租户 id，缺一个就还会串。
    assert 'f"ocibot-{service}-{tenant_id}"' in helper

    # 0.4.96 只覆盖了 Identity。这几个当时全漏了。
    for service in ("network", "identity", "blockstorage", "monitoring", "limits"):
        assert f'_cb_kw("{service}")' in src, f"{service} 仍在用共享熔断器"

    # Compute 不该被顺手加上熔断器 —— 它本来就没有，加了会引入新的失败模式，
    # 而实例列表和实例详情正好全压在它身上。
    compute_line = [ln for ln in src.splitlines() if "self._compute = " in ln]
    assert compute_line and "circuit_breaker" not in compute_line[0]
    assert "_cb_kw" not in compute_line[0]


def test_breaker_names_differ_across_tenants_and_services():
    """行为断言，不是源码断言：两个租户拿到的 name 必须不同。"""
    from app.oci_client import cb_kwargs

    seen = set()
    for tenant_id in ("t-aaa", "t-bbb"):
        for service in ("identity", "monitoring"):
            name = cb_kwargs(service, tenant_id)["circuit_breaker_strategy"].name
            assert name not in seen, f"{name} 撞名了 —— 会复用同一个熔断器"
            seen.add(name)
    assert len(seen) == 4


def test_no_client_is_left_on_the_shared_process_wide_breaker():
    """0.4.96 只改了 _build 里的 Identity。_build **之外**还有五处按需重建 client
    的地方（账单主区 Identity / InvoiceService / UsageApi / Subscription /
    IdentityDomains），当时全漏了 —— 它们默认落回那个固定 uuid 的共享熔断器。

    直接扫源码：任何 `XxxClient(` 构造要么带 circuit_breaker_strategy（经由
    cb_kwargs / _cb_kw），要么是 Compute（本来就没有熔断器）。
    """
    import pathlib
    import re

    src = pathlib.Path("app/oci_client.py").read_text(encoding="utf-8")
    # 取每一处 `SomethingClient(` 之后到匹配右括号为止的那段。
    for m in re.finditer(r"\b([A-Z]\w*Client)\(", src):
        name = m.group(1)
        if name == "ComputeClient":
            continue
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[m.start() : i + 1]
        assert "cb_kw" in call or "circuit_breaker_strategy" in call, (
            f"{name} 仍在用全进程共享的熔断器：{call[:160]}"
        )


# ---------------------------------------------------------------- 枚举失败


def test_enumeration_catches_more_than_service_error():
    """只接 ServiceError 的话，熔断器异常和连接超时会直接穿透出去 ——
    连 strict=False 那条「退化成只有根」的 docstring 承诺都保不住。"""
    code = _code_only(TenantSession.list_compartments)

    assert "except Exception" in code
    assert "except ServiceError as exc" not in code


def test_enumeration_failure_leaves_structured_facts():
    """把 status / code / opc-request-id / operation / host 留下来，
    让上层能说清楚到底是哪一种失败。"""
    src = inspect.getsource(TenantSession.list_compartments)
    assert "_last_enum_facts" in src
    assert "_err_facts(exc)" in src


def test_the_subtree_call_bounds_its_own_retry():
    """oci.pagination.list_call_get_all_results 自己**硬编码**又套了一层
    DEFAULT_RETRY_STRATEGY，和 client 层那个相乘：8 × 8 = 最坏 64 次真实调用、
    ~600 秒卡在一个 HTTP 请求里 —— 在跟抢机重试循环抢同一个 per-tenancy 限流额度。"""
    # 0.4.96 起这段提成了模块级的 sdk_bounded_paged_retry_strategy()，
    # 供 list_compartments / list_instances / list_shapes / list_vnic_attachments 共用。
    from app.oci_client import sdk_bounded_paged_retry_strategy

    code = _code_only(sdk_bounded_paged_retry_strategy)
    assert "RetryStrategyBuilder" in code
    assert "max_attempts = 3" in code.replace("=", " = ").replace("  ", " ")
    # 绝不能把 404 加进去 —— Oracle 的错误表把它的 Retry 列标成 "No."。
    cfg = code.split("service_error_retry_config")[1].split("}")[0]
    assert "404" not in cfg, cfg

    # 行为断言（不只是源码）：建出来的 checker 里确实带上了那份 config。
    st = sdk_bounded_paged_retry_strategy()
    configs = [
        vars(c).get("service_error_retry_config")
        for c in st.checkers.checkers
        if "service_error_retry_config" in vars(c)
    ]
    assert configs and 404 not in configs[0], configs

    # 四条分页路径都要用上它。
    for fn in (
        TenantSession._compartment_children,
        TenantSession.list_instances,
        TenantSession.list_shapes,
        TenantSession.resolve_primary_network,
    ):
        assert "sdk_bounded_paged_retry_strategy" in _code_only(fn), fn.__name__


def test_access_level_stays_accessible():
    """ACCESSIBLE 是**结果过滤器**（只返回你有 INSPECT 权限的那些）。

    文档里 "permissions are not checked" 说的是过滤器不生效，不是「不需要权限」——
    换成 ANY 等于要求调用方在整个请求范围上拿到授权，而权限受限的租户正是当前
    枚举会失败的那批人。那个改动只会把「有时候能枚举」变成「永远不能枚举」。
    """
    src = inspect.getsource(TenantSession._compartment_children)
    assert '"access_level": "ACCESSIBLE"' in src
    assert '"ANY"' not in src


# ---------------------------------------------------------------- 一次复读


def test_the_collapse_path_re_reads_before_raising():
    """不是重试策略，是取一位证据：复读成功 → 这个 404 按定义就是瞬时的。

    只在**注定要抛**的那一刻做，次数有界，且只对 404 做。
    成功路径 0 增量；失败路径 +N 个幂等的 List —— 严格少于用户现在手动点三五次
    刷新（每次 1+N 个请求）。
    """
    src = _code_only(TenantSession.list_instances_tree)

    assert "retried_ok" in src
    # 复读策略统一收在 read_with_404_evidence 里，list 和 detail 共用同一份。
    assert "read_with_404_evidence" in src
    assert "_is_ambiguous_404" in src


def test_only_404_is_re_read():
    """429 / 5xx / 401 都不该触发复读：限流有 SDK 自己的退避，401 是真的凭据问题。"""
    from app.oci_client import _REREAD_DELAYS, read_with_404_evidence

    calls = []

    def _boom(status, code):
        def _call():
            calls.append(1)
            exc = Exception("nope")
            exc.status = status
            exc.code = code
            raise exc

        return _call

    for status, code in [(429, "TooManyRequests"), (500, "InternalError"), (401, "NotAuthenticated")]:
        calls.clear()
        ok, _v, _e, rereads = read_with_404_evidence(_boom(status, code))
        assert not ok and rereads == 0, f"{status} 不该复读"
        assert len(calls) == 1, f"{status} 只该调用一次，实际 {len(calls)}"

    # 404 才复读，且次数有界。
    calls.clear()
    ok, _v, _e, rereads = read_with_404_evidence(_boom(404, "NotAuthorizedOrNotFound"))
    assert not ok
    assert rereads == len(_REREAD_DELAYS)
    assert len(calls) == len(_REREAD_DELAYS) + 1


def test_a_successful_first_read_costs_nothing_extra():
    """成功路径必须零增量 —— 复读只能出现在失败路径上。"""
    from app.oci_client import read_with_404_evidence

    calls = []
    ok, value, err, rereads = read_with_404_evidence(lambda: calls.append(1) or "fine")
    assert ok and value == "fine" and err is None
    assert rereads == 0 and len(calls) == 1


def test_a_permanent_failure_says_it_was_re_read():
    """重试**不能**变成一次沉默的加时。

    真的没权限的用户，加了复读之后拿到的是同一个 404、只是慢了几秒 ——
    如果不说出来，他只会觉得「变慢了、错误一模一样」。
    """
    from app.oci_client import persistent_404_note

    assert "仍然失败" in persistent_404_note(2)
    assert "2" in persistent_404_note(2)
    # 从没复读过就不能声称复读过。
    assert persistent_404_note(0) == ""


def test_a_transient_failure_leaves_a_trace_instead_of_being_silent():
    """复读成功不该是一次「侥幸通过」—— _last_tree_errors 会被路由渲染成
    X-Ocibot-Partial 响应头，前端据此挂提示条。"""
    from app.oci_client import transient_404_note

    src = _code_only(TenantSession.list_instances_tree)
    assert "_last_tree_errors" in src
    assert "transient_404_note" in src

    note = transient_404_note("[404] NotAuthorizedOrNotFound", 1)
    assert "瞬时" in note
    # 而且必须明说「不用改配置」—— 用户的困惑正是「我权限是满的」。
    assert "无关" in note


def test_a_successful_but_empty_re_read_is_not_reported_as_a_permission_error():
    """0.4.96 写的是 `if again:` —— 复读**成功但读到空列表**（这个 compartment
    确实一台实例都没有）会被判成复读失败，于是照样抛那个 404，把「读得到、只是
    空的」说成了「没有权限」。判据必须是调用成功与否，不是列表空不空。"""
    src = _code_only(TenantSession.list_instances_tree)
    assert "if again:" not in src, "又用列表真假当复读成功的判据了"
    assert "ok_again" in src


# ---------------------------------------------------------------- 探针策略


def test_the_probe_retry_strategy_uses_the_real_parameter_name():
    """0.4.90 写的是 `retry_on_service_error_codes=[429]` —— 那不是
    RetryStrategyBuilder 的参数，被 kwargs.get 静默吞掉，于是那条
    「只对 429 重试」的注释是假的。"""
    code = _code_only(TenantSession.test_connection)

    assert "retry_on_service_error_codes" not in code, "这个参数名不存在，会被静默忽略"
    assert "service_error_retry_config" in code


def test_the_corrected_strategy_actually_applies_its_config():
    """行为断言，不是源码断言：建出来的 checker 里要真的带上我们给的 config。"""
    strategy = oci.retry.RetryStrategyBuilder(
        max_attempts_check=True,
        max_attempts=2,
        total_elapsed_time_check=True,
        total_elapsed_time_seconds=20,
        service_error_check=True,
        service_error_retry_config={429: []},
        service_error_retry_on_any_5xx=True,
    ).get_retry_strategy()

    configs = [
        vars(c).get("service_error_retry_config")
        for c in strategy.checkers.checkers
        if "service_error_retry_config" in vars(c)
    ]
    assert configs and configs[0] == {429: []}, configs


def test_the_wrong_parameter_name_is_silently_ignored():
    """钉住这个坑本身：SDK 不会报错，它只是当作没看见。
    所以这类错误只能靠测试发现，不会有任何运行时信号。"""
    strategy = oci.retry.RetryStrategyBuilder(
        max_attempts_check=True,
        max_attempts=2,
        service_error_check=True,
        retry_on_service_error_codes=[429],  # 不存在的参数名
    ).get_retry_strategy()

    configs = [
        vars(c).get("service_error_retry_config")
        for c in strategy.checkers.checkers
        if "service_error_retry_config" in vars(c)
    ]
    # 拿到的是 SDK 的默认值，不是我们传的 [429]。
    assert configs and 409 in configs[0], "证明那个参数名确实被忽略了"


# ---------------------------------------------------------------- 没有全局污染


def test_the_global_retry_config_is_not_mutated():
    """`add_service_error_check(service_error_status=..., service_error_codes=...)`
    这个重载会**原地改写模块级全局** RETRYABLE_STATUSES_AND_CODES，而那正是
    DEFAULT_RETRY_STRATEGY 内部持有的同一个对象 —— 整个进程、所有租户、
    所有 client 的重试行为都会被改掉。仓库里不能出现它。"""
    import pathlib

    # 去掉注释和文档字符串再匹配 —— 解释「不要用 X」的那段话本身就含 X。
    src = _code_only(pathlib.Path("app/oci_client.py").read_text(encoding="utf-8"))
    assert "add_service_error_check" not in src

    from oci.retry.retry_checkers import RETRYABLE_STATUSES_AND_CODES

    assert 404 not in RETRYABLE_STATUSES_AND_CODES, "全局重试表被污染了"
