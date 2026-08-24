"""A refused read must never be presented as an empty result.

Four places did exactly that, each turning "we could not read this" into a
confident, wrong statement:

* an unreadable invoice list said 因此不会产生账单 — a positive financial claim
  derived from a read that failed. If the real cause is a missing billing-read
  policy, that sentence makes an account that IS accruing charges look free.
* a failed cost read returned ``total: 0``, and the page prints 合计：0. For a
  billing figure, 0 and "could not read" are opposite answers. The code comment
  directly above that line already said so about ``month_to_date``, and then
  the next line did the wrong thing.
* the fs-grow helper matched its "check your SSH username/key" hint against the
  REMOTE COMMAND's stderr — output you can only have if SSH already connected
  and authenticated. The script runs growpart/resize2fs without sudo as
  ``ubuntu``, so the most likely genuine failure prints ``Permission denied``
  and was reported as a credentials problem.
* the launch pre-check skipped the incomplete-read block that the launch path
  enforces, so 下一步 opened the confirm dialog and 确认并创建 then 503'd.
"""

from __future__ import annotations

import pytest

from app.oci_client import TenantSession


# ---------------------------------------------------------------------------
# cost: 0 and "unknown" are different answers
# ---------------------------------------------------------------------------

def test_a_failed_cost_read_reports_none_not_zero():
    """`?? '—'` in the template only catches null/undefined; 0 renders as 0."""
    s = object.__new__(TenantSession)
    s._usage = None  # the "Usage API unavailable" branch

    r = s.get_usage_summary(days=30)

    assert not r.ok
    assert r.data["total"] is None, "a cost read that never happened reported 0"
    assert r.data["month_to_date"] is None


# ---------------------------------------------------------------------------
# fs-grow: connection diagnosis must not read the guest's stderr
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, message="", stderr="", ok=False):
        self.ok = ok
        self.message = message
        self.stderr = stderr


def _hint_for(message: str, stderr: str) -> str:
    """Drive the real hint block in ssh_bridge via a stubbed single attempt."""
    from web.backend import ssh_bridge

    res = _Res(message=message, stderr=stderr)
    # ssh_exec_script's hint block runs after the retry loop; call it through the
    # public entry point with retries=1 and a runner that returns our result.
    return ssh_bridge._enrich_hints(res).message  # type: ignore[attr-defined]


def test_guest_permission_denied_is_not_called_an_ssh_auth_failure():
    """The regression. `Permission denied` on the guest's stderr means the
    command lacked root, which is only observable *after* a successful login."""
    msg = _hint_for(message="命令返回非零退出码", stderr="growpart: Permission denied")

    assert "私钥" not in msg, msg
    assert "root" in msg, msg


def test_a_real_authentication_failure_still_says_so():
    msg = _hint_for(message="Permission denied (publickey)", stderr="")
    assert "私钥" in msg


def test_guest_output_mentioning_timeout_does_not_produce_a_network_diagnosis():
    """The connection plainly worked — we have its stdout."""
    msg = _hint_for(message="命令返回非零退出码", stderr="warning: nfs mount timeout in dmesg")
    assert "连接超时" not in msg


@pytest.mark.parametrize("guest", ["operation not permitted", "Permission denied", "not permitted"])
def test_every_guest_permission_shape_is_recognised(guest: str):
    assert "root" in _hint_for(message="失败", stderr=guest)
