"""Root password surfaced from the instance's freeform tags.

Password-mode launches already wrote the generated password to the
ocibot_root_password freeform tag; nothing read it back, so the panel showed it
exactly once at creation and then lost it. The tag rides along with the instance
object, so exposing it costs no extra OCI call.
"""

from __future__ import annotations

from app.oci_client import ROOT_PASSWORD_TAG, InstanceInfo
from web.backend.oci_bridge import instance_to_dict


def _info(**kw) -> InstanceInfo:
    base = dict(
        id="ocid1.instance.oc1..i1",
        display_name="web-1",
        lifecycle_state="RUNNING",
        region="ap-tokyo-1",
        availability_domain="AD-1",
        fault_domain="FD-1",
        shape="VM.Standard.A1.Flex",
        ocpus=1.0,
        memory_gb=6.0,
        time_created="2026-01-01T00:00:00+00:00",
        compartment_id="ocid1.compartment.oc1..c",
        image_id="img",
        freeform_tags={},
        defined_tags={},
        tenant_id="t1",
        tenant_name="T",
    )
    base.update(kw)
    return InstanceInfo(**base)


def test_password_from_the_tag_reaches_the_api_payload():
    info = _info(freeform_tags={"ocibot_managed": "true", ROOT_PASSWORD_TAG: "Hunter2-Hunter2"})
    assert instance_to_dict(info)["root_password"] == "Hunter2-Hunter2"


def test_key_mode_instance_has_no_password():
    """Key-mode launches never write the tag; the column must render empty, not
    show some other tag's value."""
    info = _info(freeform_tags={"ocibot_managed": "true", "ocibot_ssh_user": "root"})
    assert instance_to_dict(info)["root_password"] == ""


def test_instance_without_any_tags_does_not_raise():
    assert instance_to_dict(_info(freeform_tags={}))["root_password"] == ""


def test_non_string_tag_value_is_coerced():
    """Tags come back from the SDK; a surprising type must not break the list."""
    info = _info(freeform_tags={ROOT_PASSWORD_TAG: 12345})
    assert instance_to_dict(info)["root_password"] == "12345"
