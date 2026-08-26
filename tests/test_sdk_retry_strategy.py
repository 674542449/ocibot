"""SDK-level retry strategy helpers (client default vs LaunchInstance no-retry)."""

from __future__ import annotations

from app.oci_client import (
    OCI_AVAILABLE,
    sdk_default_retry_strategy,
    sdk_no_retry_strategy,
)


def test_sdk_retry_helpers_return_strategy_objects_when_oci_present():
    if not OCI_AVAILABLE:
        assert sdk_default_retry_strategy() is None
        assert sdk_no_retry_strategy() is None
        return

    import oci

    default = sdk_default_retry_strategy()
    none = sdk_no_retry_strategy()
    # **每次新建一个**，不再返回 oci.retry.DEFAULT_RETRY_STRATEGY 那个模块级单例：
    # SDK 每次调用前都会往策略对象上写 add_circuit_breaker_callback(...)，返回单例
    # 等于让所有租户、所有 client、所有线程往同一个可变对象上写。
    assert default is not oci.retry.DEFAULT_RETRY_STRATEGY
    assert default is not sdk_default_retry_strategy()
    assert isinstance(none, oci.retry.NoneRetryStrategy)
    # 行为要和原来的默认策略一致：429 和任意 5xx 重试，404 绝不重试。
    configs = [
        vars(c).get("service_error_retry_config")
        for c in default.checkers.checkers
        if "service_error_retry_config" in vars(c)
    ]
    assert configs and 429 in configs[0] and 404 not in configs[0], configs
    # 而且**不能**污染那个全局表。
    from oci.retry.retry_checkers import RETRYABLE_STATUSES_AND_CODES

    assert 404 not in RETRYABLE_STATUSES_AND_CODES
    # Default strategy retries; None strategy is a single-shot wrapper.
    assert hasattr(default, "make_retrying_call")
    assert none.make_retrying_call(lambda: 42) == 42


def test_a_tripped_circuit_breaker_fails_fast_instead_of_backing_off_eight_times():
    """SDK 自己的 checker 里有一条 `elif isinstance(exception, CircuitBreakerError):
    ... return True` —— 熔断器一打开，调用方线程不会马上拿到错误，而是按
    1.4/2.9/4.1/8.3/16.9/30/30 秒退避重试八次，把一个线程占住三十几秒到一分钟。
    而熔断本来的意义恰恰是快速失败。FastAPI 的同步路由跑在 anyio 那个默认 40 线程
    的池里，这类调用会把池坐满，面板整体像卡死。"""
    if not OCI_AVAILABLE:
        return
    import oci

    from circuitbreaker import CircuitBreaker, CircuitBreakerError

    import time

    err = CircuitBreakerError(CircuitBreaker(name="probe"))
    strategy = sdk_default_retry_strategy()

    # **必须走容器、必须按位置传参。**
    # RetryCheckerContainer.should_retry 的实现是
    #     for c in self.checkers:
    #         if not c.should_retry(exception, response, **kwargs):
    # —— 两个**位置**参数。用关键字调用单个 checker 测不出签名不兼容：包装函数写成
    # (exception=None, **kwargs) 时关键字调用一切正常，而真实的位置调用会抛
    # "takes from 0 to 1 positional arguments but 2 were given"，然后这个 TypeError
    # 会顶替掉本该发生的那个 OCI 异常。这条测试就是为了钉住那次真实的翻车。
    assert strategy.checkers.should_retry(err, None, current_attempt=1) is False

    # 而普通的 429 必须照常重试 —— 别把退避一起关掉了。
    throttle = oci.exceptions.ServiceError(429, "TooManyRequests", {}, "slow down")
    assert strategy.checkers.should_retry(throttle, None, current_attempt=1) is True

    # 端到端：429 真的会重试到成功，熔断器异常真的一次就放弃。
    calls = []

    def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise oci.exceptions.ServiceError(429, "TooManyRequests", {}, "slow down")
        return "ok"

    assert strategy.make_retrying_call(_flaky) == "ok"
    assert len(calls) == 3, f"429 没有被重试：{len(calls)} 次"

    calls.clear()

    def _tripped():
        calls.append(1)
        raise err

    t0 = time.monotonic()
    try:
        sdk_default_retry_strategy().make_retrying_call(_tripped)
    except Exception:  # noqa: BLE001
        pass
    assert len(calls) == 1, f"熔断器异常被重试了 {len(calls)} 次"
    assert time.monotonic() - t0 < 2, "熔断器异常应该立刻失败，而不是退避几十秒"


def test_default_and_no_retry_are_distinct():
    if not OCI_AVAILABLE:
        return
    assert sdk_default_retry_strategy() is not sdk_no_retry_strategy()
