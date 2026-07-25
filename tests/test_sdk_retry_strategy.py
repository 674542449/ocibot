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
    assert default is oci.retry.DEFAULT_RETRY_STRATEGY
    assert isinstance(none, oci.retry.NoneRetryStrategy)
    # Default strategy retries; None strategy is a single-shot wrapper.
    assert hasattr(default, "make_retrying_call")
    assert none.make_retrying_call(lambda: 42) == 42


def test_default_and_no_retry_are_distinct():
    if not OCI_AVAILABLE:
        return
    assert sdk_default_retry_strategy() is not sdk_no_retry_strategy()
