"""测试里的假 OCI 客户端要和真 SDK 一样挑剔参数。

0.4.135 的教训：假的 get_subscription 接受 ``entity_version``，真 SDK 不接受 —— 生产里
每次调用都抛 ValueError，被吞掉之后试用积分永远显示「没有」，测试却全绿。
这里直接借用真 SDK 方法开头的那段参数检查，假客户端多收一个参数就和真的一样报错。
"""

from __future__ import annotations

from typing import Any


def check_sdk_kwargs(sdk_method: Any, *args: Any, **kwargs: Any) -> None:
    """Raise the real SDK's ValueError if ``kwargs`` has a name the method rejects.

    The SDK validates kwargs before it touches its HTTP client, so calling the
    unbound method with a bare instance reaches that check and then fails on the
    missing client — which is ignored here; only the kwargs verdict matters.
    """
    owner = sdk_method.__qualname__.split(".")[0]
    cls = sdk_method.__globals__[owner]
    stub = cls.__new__(cls)
    try:
        sdk_method(stub, *args, **kwargs)
    except ValueError as exc:
        if "unknown kwargs" in str(exc):
            raise
    except Exception:  # noqa: BLE001 - got past the kwargs check
        pass
