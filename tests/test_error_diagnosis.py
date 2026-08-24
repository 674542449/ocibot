"""The panel must not send the operator to fix something that is not broken.

A user hit intermittent `[404] NotAuthorizedOrNotFound` on the instance list and
on launch metadata. The panel appended:

    提示：请检查 API Key、Fingerprint、Tenancy/User OCID 是否匹配。

Every one of those was fine — 测试连接 said 连接成功, because it only called
`get_user()`, which succeeds with almost any valid key regardless of IAM policy.
The real cause is policy *scope*: the key cannot read the compartment the panel
is querying. Regenerating the key changes nothing.

`NotAuthorizedOrNotFound` is Oracle's deliberately ambiguous 404 — "no
permission OR does not exist". The old branch order made it match the
credentials hint first, so the genuinely useful 404 branch below it was
unreachable.
"""

from __future__ import annotations

import pytest

from app.oci_client import _format_service_error


class _Err(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _hint(status, code, message="") -> str:
    return _format_service_error(_Err(status, code, message))


def test_not_authorized_or_not_found_does_not_blame_the_key():
    """The regression that started this."""
    text = _hint(404, "NotAuthorizedOrNotFound", "Authorization failed or requested resource not found.")

    assert "不是密钥错误" in text
    # The old, wrong advice must be gone: it sends the operator to regenerate a
    # key that 测试连接 has already proven good.
    assert "Fingerprint" not in text
    # And it must name the thing that IS actionable.
    assert "Compartment" in text


def test_real_authentication_failure_still_points_at_credentials():
    """401 is the case where the key genuinely is the problem — the signature
    did not verify. That advice must survive."""
    text = _hint(401, "NotAuthenticated", "The required information to complete authentication was not provided.")

    assert "Fingerprint" in text
    assert "不是密钥错误" not in text


def test_forbidden_says_the_credentials_are_fine():
    text = _hint(403, "NotAllowed", "Operation not permitted")
    assert "凭据有效" in text


@pytest.mark.parametrize(
    "code",
    ["NotAuthorizedOrNotFound", "notauthorizedornotfound", "NOTAUTHORIZEDORNOTFOUND"],
)
def test_code_matching_is_case_insensitive(code: str):
    assert "不是密钥错误" in _hint(404, code, "whatever")


def test_plain_404_keeps_its_own_hint():
    """A 404 that is not the ambiguous authz code is an ordinary missing
    resource; it must not claim to be a permissions problem."""
    text = _hint(404, "InstanceNotFound", "instance does not exist")
    assert "无权限" in text
    assert "不是密钥错误" not in text


def test_rate_limit_hint_is_unaffected():
    text = _hint(429, "TooManyRequests", "too many requests")
    assert "限流" in text
    assert "Fingerprint" not in text


def test_the_status_and_code_still_lead_the_message():
    """The raw Oracle code has to stay visible — it is what an operator pastes
    into a search or a support ticket."""
    text = _hint(404, "NotAuthorizedOrNotFound", "Authorization failed")
    assert text.startswith("[404] NotAuthorizedOrNotFound")
