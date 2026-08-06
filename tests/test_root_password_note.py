"""Editing the root password remembered against an instance.

It is stored in the OCI freeform tag ``ocibot_root_password``, written once at
launch. Change the password on the box over SSH and the panel keeps showing the
old one forever, with nothing marking it stale — so it has to be editable.

The dangerous part is not the edit, it is the write: UpdateInstanceDetails
REPLACES the whole freeform-tag map. Sending just this one key would silently
delete every other tag on the instance, and nothing would report an error.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.oci_client import ROOT_PASSWORD_TAG, TenantSession  # noqa: E402


class _Compute:
    def __init__(self, tags):
        self.tags = dict(tags)
        self.updated: list[object] = []

    def get_instance(self, instance_id):
        return types.SimpleNamespace(
            data=types.SimpleNamespace(freeform_tags=dict(self.tags))
        )

    def update_instance(self, instance_id, details):
        self.updated.append(details)
        self.tags = dict(details.freeform_tags or {})
        return types.SimpleNamespace(data=None)


def _session(tags):
    """Session wired to a stub compute client.

    Sets `_compute`, which the real `compute` property reads. An earlier version
    of this helper assigned a replacement property onto the CLASS — that works,
    and it also permanently rebinds TenantSession.compute for every other test in
    the same process. Per-instance state only.
    """
    s = TenantSession.__new__(TenantSession)
    compute = _Compute(tags)
    s._compute = compute
    return s, compute


def test_other_tags_survive_the_update():
    """The whole reason this reads before it writes."""
    s, compute = _session({"ocibot_managed": "true", "team": "ops"})
    res = s.set_root_password_note("ocid1.instance.oc1..i1", "NewPass123!")

    assert res.ok, res.message
    assert compute.tags["ocibot_managed"] == "true", "unrelated tag was destroyed"
    assert compute.tags["team"] == "ops"
    assert compute.tags[ROOT_PASSWORD_TAG] == "NewPass123!"


def test_updates_an_existing_note():
    s, compute = _session({ROOT_PASSWORD_TAG: "OldPass", "keep": "1"})
    s.set_root_password_note("i1", "BrandNew")
    assert compute.tags[ROOT_PASSWORD_TAG] == "BrandNew"
    assert compute.tags["keep"] == "1"


def test_adds_a_note_to_an_instance_that_never_had_one():
    """Key-mode launches carry no password tag; recording one later must work."""
    s, compute = _session({})
    res = s.set_root_password_note("i1", "Recorded123")
    assert res.ok
    assert compute.tags[ROOT_PASSWORD_TAG] == "Recorded123"


def test_empty_removes_the_key_rather_than_storing_a_blank():
    """A blank value would render as an empty cell that looks like a bug; the
    list shows "—" only when the key is absent."""
    s, compute = _session({ROOT_PASSWORD_TAG: "OldPass", "keep": "1"})
    res = s.set_root_password_note("i1", "   ")
    assert res.ok
    assert ROOT_PASSWORD_TAG not in compute.tags
    assert compute.tags["keep"] == "1"


@pytest.mark.parametrize("bad", ["has\nnewline", "has\rcarriage"])
def test_newlines_are_rejected(bad):
    s, compute = _session({ROOT_PASSWORD_TAG: "OldPass"})
    res = s.set_root_password_note("i1", bad)
    assert res.ok is False
    assert not compute.updated, "must not write when validation failed"
    assert compute.tags[ROOT_PASSWORD_TAG] == "OldPass", "old value must survive"


def test_overlong_value_is_rejected_before_writing():
    """OCI caps a tag value at 256 chars. Truncating would store a password that
    is not the password."""
    s, compute = _session({})
    res = s.set_root_password_note("i1", "x" * 300)
    assert res.ok is False
    assert not compute.updated


def test_no_complexity_rule_is_applied():
    """This records an existing password, it does not set one. Refusing a short
    value would leave the panel showing the previous password — the exact
    staleness this feature exists to fix."""
    s, compute = _session({})
    res = s.set_root_password_note("i1", "short")
    assert res.ok, res.message
    assert compute.tags[ROOT_PASSWORD_TAG] == "short"


def test_service_error_leaves_the_tag_untouched():
    class _Boom(_Compute):
        def update_instance(self, instance_id, details):
            raise RuntimeError("oracle said no")

    s = TenantSession.__new__(TenantSession)
    compute = _Boom({ROOT_PASSWORD_TAG: "OldPass"})
    s._compute = compute

    res = s.set_root_password_note("i1", "NewPass")
    assert res.ok is False
    assert "oracle said no" in res.message
    assert compute.tags[ROOT_PASSWORD_TAG] == "OldPass"
