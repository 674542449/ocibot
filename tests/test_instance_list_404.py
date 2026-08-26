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


def test_identity_client_gets_a_per_tenant_circuit_breaker():
    """每个租户一个独立 name → CircuitBreakerMonitor 不再复用同一个实例。

    保留熔断保护本身（A 租户被 Oracle 限流时仍然该退避），
    只去掉「A 租户的限流把 B 租户也熔断掉」这个交叉污染。
    """
    src = inspect.getsource(TenantSession._build)

    assert "circuit_breaker_strategy" in src, "Identity 没有自己的熔断器策略"
    assert 'f"ocibot-identity-{self.tenant.id}"' in src, "熔断器 name 必须带租户 id"
    # Compute 不该被顺手加上熔断器 —— 它本来就没有，加了会引入新的失败模式。
    compute_line = [ln for ln in src.splitlines() if "self._compute = " in ln]
    assert compute_line and "circuit_breaker" not in compute_line[0]


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


def test_the_collapse_path_re_reads_once_before_raising():
    """不是重试策略，是取一位证据：复读成功 → 这个 404 按定义就是瞬时的。

    只在**注定要抛**的那一刻做，只做一次，且只对 404 做。
    成功路径 0 增量；失败路径 +1 个幂等的 List —— 严格少于用户现在手动点三五次
    刷新（每次 1+N 个请求）。
    """
    src = inspect.getsource(TenantSession.list_instances_tree)

    assert "retried_ok" in src
    assert "time.sleep(1.5)" in src
    # 只对 404 复读。
    assert '"[404]" in detail or "NotAuthorizedOrNotFound" in detail' in src


def test_a_permanent_failure_says_it_was_re_read():
    """重试**不能**变成一次沉默的加时。

    真的没权限的用户，加了复读之后拿到的是同一个 404、只是慢了 1.5 秒 ——
    如果不说出来，他只会觉得「变慢了、错误一模一样」。
    """
    src = inspect.getsource(TenantSession.list_instances_tree)
    assert "已自动重读 1 次，仍然失败" in src


def test_a_transient_failure_leaves_a_trace_instead_of_being_silent():
    """复读成功不该是一次「侥幸通过」—— _last_tree_errors 会被路由渲染成
    X-Ocibot-Partial 响应头，前端据此挂提示条。"""
    src = inspect.getsource(TenantSession.list_instances_tree)
    assert "_last_tree_errors" in src
    assert "重读成功" in src


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
