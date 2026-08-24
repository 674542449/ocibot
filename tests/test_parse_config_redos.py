"""`parse_oci_api_text` must stay linear in input length.

The INI sniff used to be ``re.search(r"^\\s*\\[.+\\]\\s*$", raw, re.M)``. ``\\s``
matches ``\\n``, so under ``re.M`` the engine restarts ``\\s*`` at every line
start and backtracks to end-of-string each time — quadratic in the number of
blank lines.

``TenantPasteImport.api_text`` allows 64 000 characters, which is also the worst
case: ~13 s of pure CPU that does not release the GIL. With the default two API
workers, a handful of concurrent ``POST /api/tenants/parse`` calls (no rate limit
on that route) wedge the whole panel.

This test pins the complexity, not a wall-clock number, so it does not go flaky
on a slow or loaded CI box: quadratic growth would make 4x the input ~16x the
time, and the assertion allows a very generous linear-ish factor.
"""

from __future__ import annotations

import time

from app.config_store import parse_oci_api_text

# The shape that triggered it: blank lines, no [section] header, and a leading /
# trailing non-space char so .strip() cannot shorten it.
def _payload(n: int) -> str:
    return "x" + ("\n" * (n - 2)) + "x"


def _timed(text: str) -> float:
    start = time.perf_counter()
    parse_oci_api_text(text)
    return time.perf_counter() - start


def test_blank_line_input_is_not_quadratic():
    small = _payload(8_000)
    large = _payload(32_000)  # 4x the input

    # Warm up so import/compile costs do not land in the measurement.
    parse_oci_api_text(small)

    t_small = max(_timed(small) for _ in range(3))
    t_large = max(_timed(large) for _ in range(3))

    # Quadratic would be ~16x. Linear is ~4x. Allow a wide margin for a noisy
    # machine but still fail decisively on O(n^2).
    assert t_large < max(t_small * 8.0, 0.5), (
        f"4x input took {t_large / max(t_small, 1e-9):.1f}x the time "
        f"({t_small:.4f}s -> {t_large:.4f}s) — the sniff looks quadratic again"
    )


def test_worst_case_at_the_schema_limit_is_fast():
    """64 000 chars is the maximum api_text; it must not take seconds."""
    assert _timed(_payload(64_000)) < 1.0


def test_section_detection_still_works():
    """The rewrite must not change what the sniff actually decides."""
    with_section = parse_oci_api_text(
        "[DEFAULT]\nuser=ocid1.user.oc1..abc\nregion=ap-tokyo-1\n"
    )
    assert with_section.get("user_ocid") == "ocid1.user.oc1..abc"
    assert with_section.get("region") == "ap-tokyo-1"

    # Indented header, which the old \s* prefix also accepted.
    indented = parse_oci_api_text("   [PROFILE]   \nregion=us-ashburn-1\n")
    assert indented.get("region") == "us-ashburn-1"

    # No header at all -> DEFAULT is injected and the keys still parse.
    headerless = parse_oci_api_text("user=ocid1.user.oc1..xyz\nregion=eu-frankfurt-1\n")
    assert headerless.get("user_ocid") == "ocid1.user.oc1..xyz"
    assert headerless.get("region") == "eu-frankfurt-1"

    assert parse_oci_api_text("") == {}
