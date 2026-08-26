"""Deleting and renaming a boot volume.

Boot volumes had no operable action at all: the storage page listed them and
that was it, while `free_quota.summarize_storage` already counted the detached
ones (`orphan_boot_count`) and the quota panel displayed the number. Terminating
an instance with "preserve boot volume" — the default in the OCI console —
leaves one behind, and it keeps consuming the tenancy's 200 GB Always Free
block-storage allowance with no way to reclaim it.

The delete guard is the part worth pinning hard: getting "is this still
attached?" wrong destroys a running machine's system disk. `delete_block_volume`
carries a comment about exactly this going silently wrong once, because the AD
was passed positionally and the resulting `TypeError` was swallowed by the
`except Exception` around the guard.
"""

from __future__ import annotations

import pytest

import oci

from app.oci_client import TenantSession


def _Resp(data, next_page=None):
    """返回一个**真的** oci.Response，不要自己捏一个。

    分页助手 list_call_get_all_results 会依次读 next_page / has_next_page /
    status / request / headers …… 自己捏的桩每给一处调用加分页就少一个属性，
    只能不停地补 —— 而每一次「补属性」都是在猜 SDK 的形状。用真类，形状永远是对的。
    """
    resp = oci.Response(200, {}, data, None)
    resp.next_page = next_page
    return resp


class _Vol:
    def __init__(self, **kw):
        self.id = kw.get("id", "ocid1.bootvolume.oc1..bv")
        self.display_name = kw.get("display_name", "web-01 (Boot Volume)")
        self.size_in_gbs = kw.get("size_in_gbs", 47)
        self.lifecycle_state = kw.get("lifecycle_state", "AVAILABLE")
        self.availability_domain = kw.get("availability_domain", "kZpB:AP-TOKYO-1-AD-1")
        self.compartment_id = kw.get("compartment_id", "ocid1.compartment.oc1..c")


class _Att:
    def __init__(self, state="ATTACHED", instance_id="ocid1.instance.oc1..i"):
        self.lifecycle_state = state
        self.instance_id = instance_id


class _BlockStorage:
    def __init__(self, vol=None):
        self._vol = vol if vol is not None else _Vol()
        self.deleted: list[str] = []
        self.updated: list[tuple] = []

    def get_boot_volume(self, vid):
        return _Resp(self._vol)

    def delete_boot_volume(self, vid):
        self.deleted.append(vid)

    def update_boot_volume(self, vid, details):
        self.updated.append((vid, details.display_name))


class _Compute:
    def __init__(self, attachments=None, raises=None):
        self._att = attachments or []
        self._raises = raises

    # **kwargs 是必须的：真实签名是 (availability_domain, compartment_id, **kwargs)，
    # retry_strategy / limit / page 都从那里进。
    def list_boot_volume_attachments(self, ad, cid, boot_volume_id=None, **kwargs):
        if self._raises:
            raise self._raises
        return _Resp(list(self._att))


def _session(blockstorage, compute) -> TenantSession:
    """Build a TenantSession without touching OCI.

    `blockstorage` / `compute` are read-only properties over lazily-built
    clients, so the fakes go into the private backing attributes the properties
    read. Constructing via __new__ keeps the real methods under test while
    skipping _build() entirely — no config, no key, no network.
    """
    s = object.__new__(TenantSession)
    s._blockstorage = blockstorage
    s._compute = compute
    s.resolve_compartment = lambda: "ocid1.compartment.oc1..c"  # type: ignore[method-assign]
    return s


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------

def test_detached_volume_is_deleted_and_reports_reclaimed_space():
    bs = _BlockStorage(_Vol(size_in_gbs=47, display_name="old-web (Boot Volume)"))
    s = _session(bs, _Compute(attachments=[]))

    r = s.delete_boot_volume("ocid1.bootvolume.oc1..bv")

    assert r.ok, r.message
    assert bs.deleted == ["ocid1.bootvolume.oc1..bv"]
    # The operator is reclaiming quota; the message and the audit payload must
    # both say how much, since that is the whole reason for the action.
    assert "47" in r.message
    assert r.data["size_in_gbs"] == 47


def test_attached_volume_is_refused_and_never_deleted():
    bs = _BlockStorage()
    s = _session(bs, _Compute(attachments=[_Att("ATTACHED")]))

    r = s.delete_boot_volume("ocid1.bootvolume.oc1..bv")

    assert not r.ok
    assert bs.deleted == [], "an attached boot volume was deleted"
    assert "挂载" in r.message


@pytest.mark.parametrize("state", ["DETACHED", "DETACHING", ""])
def test_dead_attachment_records_do_not_block_deletion(state: str):
    """A detached leftover record must not make an orphan undeletable."""
    bs = _BlockStorage()
    s = _session(bs, _Compute(attachments=[_Att(state)]))

    assert s.delete_boot_volume("ocid1.bootvolume.oc1..bv").ok
    assert bs.deleted


def test_unreadable_attachment_list_fails_closed():
    """The load-bearing one.

    If the attachment list cannot be read we cannot prove the volume is
    detached, and the cost of being wrong is wiping a live machine's disk. It
    must refuse, not fall through to the delete.
    """
    bs = _BlockStorage()
    s = _session(bs, _Compute(raises=RuntimeError("429 TooManyRequests")))

    r = s.delete_boot_volume("ocid1.bootvolume.oc1..bv")

    assert not r.ok
    assert bs.deleted == [], "deleted a boot volume without confirming it was detached"
    assert "无法确认" in r.message


def test_typeerror_from_a_wrong_signature_also_fails_closed():
    """Regression for the exact way this went wrong on the block-volume path.

    There, the AD was passed positionally, the SDK raised TypeError, and a bare
    `except Exception` around the guard swallowed it — so "still attached?" was
    silently answered "no" and the delete proceeded.
    """
    bs = _BlockStorage()
    s = _session(bs, _Compute(raises=TypeError("unexpected keyword argument")))

    assert not s.delete_boot_volume("ocid1.bootvolume.oc1..bv").ok
    assert bs.deleted == []


@pytest.mark.parametrize("state", ["PROVISIONING", "TERMINATING", "RESTORING"])
def test_volume_in_a_transient_state_is_refused(state: str):
    bs = _BlockStorage(_Vol(lifecycle_state=state))
    s = _session(bs, _Compute(attachments=[]))

    r = s.delete_boot_volume("ocid1.bootvolume.oc1..bv")

    assert not r.ok
    assert state in r.message
    assert bs.deleted == []


def test_blank_id_is_refused_without_calling_oci():
    bs = _BlockStorage()
    s = _session(bs, _Compute(attachments=[]))
    assert not s.delete_boot_volume("   ").ok
    assert bs.deleted == []


# --------------------------------------------------------------------------
# rename
# --------------------------------------------------------------------------

def test_rename_sets_the_display_name():
    bs = _BlockStorage()
    s = _session(bs, _Compute())

    r = s.rename_boot_volume("ocid1.bootvolume.oc1..bv", "  保留-旧站点  ")

    assert r.ok, r.message
    assert bs.updated == [("ocid1.bootvolume.oc1..bv", "保留-旧站点")]


def test_rename_rejects_an_empty_name():
    bs = _BlockStorage()
    s = _session(bs, _Compute())
    assert not s.rename_boot_volume("ocid1.bootvolume.oc1..bv", "   ").ok
    assert bs.updated == []


def test_rename_truncates_at_the_oci_limit():
    """255 is OCI's cap. Truncating beats letting the API reject the whole call
    over a detail the operator cannot see in the form."""
    bs = _BlockStorage()
    s = _session(bs, _Compute())

    assert s.rename_boot_volume("ocid1.bootvolume.oc1..bv", "x" * 400).ok
    assert len(bs.updated[0][1]) == 255
