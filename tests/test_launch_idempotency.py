"""Launching twice because the first response was lost must not create two VMs.

A Cloudflare 520 (or any dropped connection) tells the operator nothing about
whether Oracle acted. Without a retry token the only way to find out is to look,
and looking races the retry — which is how one intended instance becomes two, on
an account whose free allowance is exactly one.

`opc-retry-token` makes Oracle replay the original outcome for 24 hours instead
of creating a second machine.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.oci_client import _clean_retry_token  # noqa: E402


class _Compute:
    def __init__(self):
        self.kwargs: list[dict] = []

    def launch_instance(self, details, **kwargs):
        self.kwargs.append(kwargs)
        return types.SimpleNamespace(
            data=types.SimpleNamespace(id="ocid1.instance.oc1..new", display_name="i"),
            headers={},
        )


# --------------------------------------------------------------- token hygiene


def test_token_is_capped_at_the_oracle_limit():
    """opc-retry-token is limited to 64 characters; a longer one is rejected by
    the service, which would turn the safety net into a failed launch."""
    assert len(_clean_retry_token("x" * 200)) == 64


def test_token_strips_characters_oracle_rejects():
    assert _clean_retry_token("ab/cd ef:gh") == "abcdefgh"
    assert _clean_retry_token("keep-these_123") == "keep-these_123"


def test_unusable_token_degrades_to_none_rather_than_failing():
    """Sanitising instead of raising: the token is a protection, and refusing the
    whole launch because the key looked odd would be a worse outcome than
    launching without it."""
    assert _clean_retry_token("!!!") == ""
    assert _clean_retry_token("") == ""
    assert _clean_retry_token(None) == ""  # type: ignore[arg-type]


# ------------------------------------------------------------ wiring to the SDK


def _session_with_compute(compute):
    from app.oci_client import TenantSession

    s = TenantSession.__new__(TenantSession)
    s._compute = compute
    s.tenant = types.SimpleNamespace(
        id="t1", name="T", tenancy_ocid="ocid1.tenancy.oc1..t", region="ap-tokyo-1"
    )
    return s


def test_no_token_supplied_means_no_header(monkeypatch):
    """Preserves the previous behaviour for callers that do not pass one."""
    from app import oci_client

    compute = _Compute()
    s = _session_with_compute(compute)
    monkeypatch.setattr(oci_client, "sanitize_launch_payload", lambda p, **k: dict(p))
    s.launch_instance(
        display_name="i", compartment_id="c", availability_domain="AD-1",
        shape="VM.Standard.E2.1.Micro", image_id="img", subnet_id="sub",
        ssh_public_key="ssh-ed25519 AAAA", auth_mode="key",
    )
    assert "opc_retry_token" not in compute.kwargs[0]


def test_token_is_forwarded_to_the_sdk(monkeypatch):
    from app import oci_client

    compute = _Compute()
    s = _session_with_compute(compute)
    monkeypatch.setattr(oci_client, "sanitize_launch_payload", lambda p, **k: dict(p))
    s.launch_instance(
        display_name="i", compartment_id="c", availability_domain="AD-1",
        shape="VM.Standard.E2.1.Micro", image_id="img", subnet_id="sub",
        ssh_public_key="ssh-ed25519 AAAA", auth_mode="key",
        idempotency_key="abc123",
    )
    assert compute.kwargs[0]["opc_retry_token"] == "abc123"


def test_retry_strategy_is_still_disabled(monkeypatch):
    """The token must not be mistaken for re-enabling SDK retries: capacity and
    429 still have to surface once so the retry job owns the backoff."""
    from app import oci_client

    compute = _Compute()
    s = _session_with_compute(compute)
    monkeypatch.setattr(oci_client, "sanitize_launch_payload", lambda p, **k: dict(p))
    s.launch_instance(
        display_name="i", compartment_id="c", availability_domain="AD-1",
        shape="VM.Standard.E2.1.Micro", image_id="img", subnet_id="sub",
        ssh_public_key="ssh-ed25519 AAAA", auth_mode="key", idempotency_key="k",
    )
    assert "retry_strategy" in compute.kwargs[0]


# ------------------------------------------------------------------ batch rule


def test_batch_items_get_distinct_tokens():
    """THE trap. A retry token means "this is the same request as before", so one
    shared key across a batch of 5 would create the first machine and then return
    that same machine four more times — the page reports five created and one
    exists. Derived per index in the route.
    """
    key = "submission-xyz"
    tokens = [f"{key}-{i}" for i in range(5)]
    assert len(set(tokens)) == 5
    for t in tokens:
        assert _clean_retry_token(t) == t, "index suffix must survive sanitising"


def test_route_derives_per_item_tokens():
    """Pin the route's derivation, since the batch trap above is invisible in
    any single-instance test."""
    src = (
        Path(__file__).resolve().parents[1]
        / "web" / "backend" / "routers" / "instances.py"
    ).read_text(encoding="utf-8")
    assert 'item_key = f"{idempotency_key}-{index}" if idempotency_key else ""' in src
    assert "idempotency_key=item_key," in src


def _launch_view() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "web" / "frontend" / "src" / "views" / "LaunchView.vue"
    ).read_text(encoding="utf-8")


def test_frontend_reuses_the_key_across_retries_and_resets_after_success():
    """The whole scheme depends on the browser keeping the key STABLE while a
    submission is being retried and minting a NEW one once it succeeded —
    otherwise a retry looks like a fresh request (no protection) or a deliberate
    second launch is silently swallowed as a duplicate."""
    src = _launch_view()
    assert "idempotencyKey" in src
    assert "idempotency_key" in src, "must actually be sent to the API"
    # Retired on success only; keeping it on failure IS the protection.
    assert "idempotencyKey.value = ''" in src


def test_key_is_reminted_when_the_request_changes():
    """Reacting to a failure by changing the shape and pressing 创建 again is a
    DIFFERENT request. Sending the previous token with it asks Oracle to treat two
    different launches as one — replaying the earlier instance, or rejecting the
    mismatch. The key is therefore bound to the payload it was minted for."""
    src = _launch_view()
    assert "const signature = JSON.stringify(body)" in src
    assert "if (signature !== idempotencyOf.value)" in src
    assert "idempotencyOf.value = signature" in src
