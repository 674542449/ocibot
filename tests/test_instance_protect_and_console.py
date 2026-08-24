"""Termination protection, and the serial-console (boot log) capture.

Termination is the one irreversible action in the panel, and the last audit
found a class of bug where the detail page rendered instance A while every
button targeted instance B. No amount of careful confirm wording fixes that —
the dialog and the request were asking about different objects. So the gate
lives on the server and reads the *instance's own* tag: whatever the UI sends,
a protected instance cannot be terminated.

The flag is an OCI freeform tag rather than a panel-local column so it survives
a panel reinstall or a database restore, and so it is visible in the Oracle
console instead of being a fact only this panel knows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.oci_client import TERMINATE_PROTECT_TAG, TenantSession


class _Resp:
    """Enough of an oci Response for `oci.pagination.list_call_get_all_results`.

    The pagination helper reads `.headers` (for the opc-next-page token),
    `.status` and `.request` off the response, not just `.data`. A bare
    `.data`-only stub makes it raise AttributeError — which the cleanup path
    swallows by design, so the test would silently assert nothing.
    """

    def __init__(self, data, next_page=None):
        self.data = data
        self.status = 200
        self.request = None
        self.headers = {} if next_page is None else {"opc-next-page": next_page}
        self.next_page = next_page
        self.has_next_page = next_page is not None


class _Compute:
    def __init__(self, tags=None):
        self.instance = SimpleNamespace(
            id="ocid1.instance.oc1..i",
            display_name="web-01",
            compartment_id="ocid1.compartment.oc1..c",
            freeform_tags=dict(tags or {}),
        )
        self.updated: list[dict] = []

    def get_instance(self, iid):
        return _Resp(self.instance)

    def update_instance(self, iid, details):
        self.updated.append(dict(details.freeform_tags or {}))


def _session(compute) -> TenantSession:
    s = object.__new__(TenantSession)
    s._compute = compute
    s.resolve_compartment = lambda: "ocid1.compartment.oc1..c"  # type: ignore[method-assign]
    return s


# ---------------------------------------------------------------------------
# the tag write
# ---------------------------------------------------------------------------

def test_enabling_protection_sets_the_tag():
    c = _Compute()
    r = _session(c).set_instance_protected("ocid1.instance.oc1..i", True)

    assert r.ok, r.message
    assert c.updated[-1][TERMINATE_PROTECT_TAG] == "true"


def test_disabling_protection_removes_the_tag():
    c = _Compute({TERMINATE_PROTECT_TAG: "true"})
    r = _session(c).set_instance_protected("ocid1.instance.oc1..i", False)

    assert r.ok, r.message
    assert TERMINATE_PROTECT_TAG not in c.updated[-1]


def test_other_tags_survive_the_write():
    """Read-merge-write, not overwrite.

    UpdateInstanceDetails carrying only this key would delete every other tag on
    the instance — including `ocibot_root_password`, which the list view reads to
    show the recorded root password, and `ocibot_managed`, which other features
    key off. The same hazard is called out on the root-password note helper.
    """
    c = _Compute({"ocibot_root_password": "hunter2", "managed_by": "oci-console-helper"})

    _session(c).set_instance_protected("ocid1.instance.oc1..i", True)

    written = c.updated[-1]
    assert written["ocibot_root_password"] == "hunter2"
    assert written["managed_by"] == "oci-console-helper"
    assert written[TERMINATE_PROTECT_TAG] == "true"


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------

def _terminate_blocked(tags: dict) -> bool:
    """Mirror of the guard in routers/instances.py::terminate_instance.

    Kept as an explicit predicate so the truthiness rules below are pinned
    independently of FastAPI wiring — a tag store is free text, and "protected"
    must mean exactly one thing.
    """
    return str((tags or {}).get(TERMINATE_PROTECT_TAG, "")).strip().lower() == "true"


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "  true  "])
def test_protected_instance_is_blocked(value: str):
    assert _terminate_blocked({TERMINATE_PROTECT_TAG: value})


@pytest.mark.parametrize(
    "tags",
    [
        {},
        {TERMINATE_PROTECT_TAG: "false"},
        {TERMINATE_PROTECT_TAG: ""},
        {TERMINATE_PROTECT_TAG: "1"},
        {TERMINATE_PROTECT_TAG: "yes"},
        {"ocibot_root_password": "x"},
    ],
)
def test_unprotected_instance_is_not_blocked(tags: dict):
    """Only the exact string means protected.

    Deliberately strict: "1"/"yes" are NOT honoured. A tag can be edited by hand
    in the Oracle console, and a value the panel silently treats as protection
    but never writes itself would make the flag unpredictable in both directions.
    The panel writes "true" and reads "true".
    """
    assert not _terminate_blocked(tags)


# ---------------------------------------------------------------------------
# console output
# ---------------------------------------------------------------------------

class _ConsoleCompute(_Compute):
    def __init__(self, states, content="[    0.000000] Linux version 6.8", histories=None):
        super().__init__()
        self._states = list(states)
        self._content = content
        self.captured = 0
        self.deleted: list[str] = []
        self._histories = histories or []

    def list_console_histories(self, compartment_id, instance_id=None, **kw):
        return _Resp(list(self._histories))

    def delete_console_history(self, hid):
        self.deleted.append(hid)

    def capture_console_history(self, details):
        self.captured += 1
        return _Resp(SimpleNamespace(id="h-new", lifecycle_state=self._states[0]))

    def get_console_history(self, hid):
        if len(self._states) > 1:
            self._states.pop(0)
        return _Resp(SimpleNamespace(id=hid, lifecycle_state=self._states[0]))

    def get_console_history_content(self, hid, length=None):
        return _Resp(SimpleNamespace(value=self._content))


def test_capture_returns_the_boot_log():
    c = _ConsoleCompute(["SUCCEEDED"], content="fstab: mount failed")
    r = _session(c).capture_console_output("ocid1.instance.oc1..i")

    assert r.ok, r.message
    assert r.data["content"] == "fstab: mount failed"
    assert c.captured == 1


def test_capture_polls_until_ready():
    """OCI models this as a resource that goes REQUESTED -> SUCCEEDED; reading
    content before it is ready returns nothing useful."""
    c = _ConsoleCompute(["REQUESTED", "REQUESTED", "SUCCEEDED"])
    assert _session(c).capture_console_output("ocid1.instance.oc1..i", timeout=30).ok


def test_capture_reports_failure_instead_of_empty_output():
    c = _ConsoleCompute(["FAILED"])
    r = _session(c).capture_console_output("ocid1.instance.oc1..i")

    assert not r.ok
    # An empty log and a failed capture look identical to the operator unless
    # this says so — and "no output" would be read as "the machine printed
    # nothing", which is a completely different diagnosis.
    assert "失败" in r.message


def test_old_captures_are_cleaned_up_first():
    """ConsoleHistory records persist and count against a per-instance limit.
    A panel that only ever created them would eventually fail with a quota error
    naming nothing the operator recognises."""
    old = [
        SimpleNamespace(id="h-old-1", lifecycle_state="SUCCEEDED"),
        SimpleNamespace(id="h-old-2", lifecycle_state="FAILED"),
        SimpleNamespace(id="h-inflight", lifecycle_state="REQUESTED"),
    ]
    c = _ConsoleCompute(["SUCCEEDED"], histories=old)

    assert _session(c).capture_console_output("ocid1.instance.oc1..i").ok
    assert set(c.deleted) == {"h-old-1", "h-old-2"}, c.deleted
    # An in-flight capture must not be deleted out from under whoever started it.
    assert "h-inflight" not in c.deleted


def test_timeout_is_reported_as_a_timeout():
    c = _ConsoleCompute(["REQUESTED"])
    r = _session(c).capture_console_output("ocid1.instance.oc1..i", timeout=15)

    assert not r.ok
    assert "超时" in r.message


def test_blank_instance_id_is_refused_without_calling_oci():
    c = _ConsoleCompute(["SUCCEEDED"])
    assert not _session(c).capture_console_output("  ").ok
    assert c.captured == 0
