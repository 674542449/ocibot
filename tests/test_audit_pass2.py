"""Regressions for the second (parallel) audit pass.

Each test pins one confirmed finding from that pass so it cannot silently return.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_audit2_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "audit2-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "audit2-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from app.oci_client import build_root_cloud_init, sanitize_launch_payload  # noqa: E402
from web.backend.launch_service import normalize_fallback_configs  # noqa: E402
from web.backend.quota_guard import free_only_for_tier  # noqa: E402
from web.backend.tenant_import import extract_private_key_pem, strip_private_key_pem  # noqa: E402

_VALID_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfakekeymaterial"
_PAYLOAD = {
    "display_name": "i",
    "compartment_id": "ocid1.compartment.oc1..c",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.oc1..i",
    "subnet_id": "ocid1.subnet.oc1..s",
    "auth_mode": "key",
    "ssh_public_key": _VALID_KEY,
}


# ---------------------------------------------------------------------------
# PEM scanning must stay linear (ReDoS)
# ---------------------------------------------------------------------------


def test_pem_scan_is_linear_on_hostile_input():
    """Many BEGIN markers and no END marker used to backtrack quadratically.

    Measured before the fix: 108KB took ~2.3s of GIL-held CPU, growing 4x per
    doubling, which stalled every other request in the process.
    """
    hostile = "-----BEGIN PRIVATE KEY-----" * (200 * 1024 // 27)  # ~200KB
    start = time.perf_counter()
    assert extract_private_key_pem(hostile) == ""
    assert "PRIVATE KEY" in strip_private_key_pem(hostile)  # nothing to strip
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"PEM scan took {elapsed:.3f}s on 200KB — quadratic again?"


def test_pem_extraction_still_works():
    text = (
        "user=ocid1.user.oc1..aaaa\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n"
        "-----END PRIVATE KEY-----\n"
    )
    got = extract_private_key_pem(text)
    assert got.startswith("-----BEGIN PRIVATE KEY-----")
    assert got.endswith("-----END PRIVATE KEY-----")
    assert "PRIVATE KEY" not in strip_private_key_pem(text)


def test_strip_removes_every_pem_block():
    text = (
        "a\n-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n"
        "b\n-----BEGIN RSA PRIVATE KEY-----\nBBB\n-----END RSA PRIVATE KEY-----\nc"
    )
    out = strip_private_key_pem(text)
    assert "PRIVATE KEY" not in out
    assert "a" in out and "b" in out and "c" in out


# ---------------------------------------------------------------------------
# cloud-init / ssh key injection via non-\n line breaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sep", ["\r", "\x0b", "\x0c", "\x1c", "\x85", " ", " "])
def test_ssh_key_rejects_alternate_line_separators(sep):
    """A bare "\\n" check let these through, and YAML treats several as breaks."""
    payload = dict(_PAYLOAD)
    payload["ssh_public_key"] = f"{_VALID_KEY}{sep}runcmd:{sep}  - id"
    with pytest.raises(ValueError, match="SSH"):
        sanitize_launch_payload(payload)


def test_ssh_key_normal_value_still_accepted():
    clean = sanitize_launch_payload(dict(_PAYLOAD))
    assert clean["ssh_public_key"] == _VALID_KEY


@pytest.mark.parametrize("sep", [" ", " ", "\x85"])
def test_cloud_init_normalizes_unicode_line_separators(sep):
    """The user script must stay inside its YAML block scalar."""
    script = f"echo hi{sep}runcmd:{sep}  - id"
    import base64

    raw = base64.b64decode(build_root_cloud_init(
        auth_mode="key", ssh_public_key=_VALID_KEY, custom_boot_script=script
    )).decode()
    # Every injected line must be indented inside the block, never at column 0.
    body = raw.split("content: |", 1)[1]
    for line in body.splitlines():
        if line.strip() == "runcmd:":
            assert line.startswith("      "), f"escaped the block scalar: {line!r}"
            break


def test_cloud_init_rejects_control_characters():
    with pytest.raises(ValueError, match="控制字符"):
        build_root_cloud_init(
            auth_mode="key", ssh_public_key=_VALID_KEY, custom_boot_script="echo \x00 hi"
        )


def test_cloud_init_plain_script_still_embedded():
    import base64

    raw = base64.b64decode(build_root_cloud_init(
        auth_mode="key", ssh_public_key=_VALID_KEY, custom_boot_script="echo hello"
    )).decode()
    assert "      echo hello" in raw


# ---------------------------------------------------------------------------
# Quota tier default
# ---------------------------------------------------------------------------


def test_only_explicit_paid_disables_free_caps():
    """An unrecognized tier used to switch the Always-Free hard caps off."""
    assert free_only_for_tier("paid") is False
    assert free_only_for_tier("PAID") is False
    for tier in ("", "free", "unknown", "Free", "trial", "garbage", "付费", None):
        assert free_only_for_tier(tier) is True, f"{tier!r} must stay hard-capped"


# ---------------------------------------------------------------------------
# Fallback config numeric handling
# ---------------------------------------------------------------------------


def test_absurd_numeric_is_rejected_not_crashed():
    """float(10**400) raises OverflowError, which escaped as a 500."""
    with pytest.raises(ValueError, match="数字"):
        normalize_fallback_configs(
            [{"ocpus": 10**400, "memory_in_gbs": 12}], is_flex=True, as_retry=True
        )


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_non_finite_numeric_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_fallback_configs(
            [{"ocpus": bad, "memory_in_gbs": 12}], is_flex=True, as_retry=True
        )


def test_valid_fallbacks_pass():
    out = normalize_fallback_configs(
        [{"ocpus": 2, "memory_in_gbs": 12}], is_flex=True, as_retry=True
    )
    assert out == [{"ocpus": 2.0, "memory_in_gbs": 12.0}]


# ---------------------------------------------------------------------------
# Rate-limit key store is bounded
# ---------------------------------------------------------------------------


def test_rate_limiter_evicts_expired_buckets():
    from web.backend.rate_limit import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(max_hits=5, window_sec=0.01)
    for i in range(2000):
        limiter.check(f"user-{i}")
    time.sleep(0.05)
    limiter.check("trigger-sweep")
    assert len(limiter._hits) < 2000, "expired buckets were never evicted"


def test_rate_limiter_still_limits():
    from web.backend.rate_limit import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(max_hits=3, window_sec=60)
    assert [limiter.check("k")[0] for _ in range(5)] == [True, True, True, False, False]


# ---------------------------------------------------------------------------
# No plaintext key files on disk
# ---------------------------------------------------------------------------


def test_tenant_session_keeps_private_key_out_of_temp_files():
    """The decrypted OCI API key used to be written to the system temp dir."""
    pytest.importorskip("oci")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.config_store import TenantConfig
    from app.oci_client import TenantSession

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        .decode()
    )
    cfg = TenantConfig(
        id="t1",
        name="T",
        user_ocid="ocid1.user.oc1..".ljust(40, "a"),
        tenancy_ocid="ocid1.tenancy.oc1..".ljust(40, "a"),
        fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
        region="ap-tokyo-1",
        private_key_pem=pem,
    )
    tmpdir = Path(tempfile.gettempdir())
    before = set(tmpdir.glob("ocibot_key_*"))
    session = TenantSession(cfg)
    try:
        assert set(tmpdir.glob("ocibot_key_*")) == before, "wrote a plaintext key file"
        assert session._key_file is None
        assert "key_file" not in session._config
        assert session._config["key_content"].strip() == pem.strip()
    finally:
        session.close()
