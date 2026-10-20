"""Root password memo on the instance's freeform tags.

Password-mode launches write the generated password to the ocibot_root_password
freeform tag. Since 0.4.150 the list / detail payloads only say WHETHER one is
recorded (``has_root_password``); the plaintext is fetched on demand from
GET .../root-password (audited) instead of riding along with every list refresh.
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


def test_a_recorded_password_is_flagged_but_not_sent():
    info = _info(freeform_tags={"ocibot_managed": "true", ROOT_PASSWORD_TAG: "Hunter2-Hunter2"})
    out = instance_to_dict(info)
    assert out["has_root_password"] is True
    assert "Hunter2-Hunter2" not in repr(out)


def test_key_mode_instance_has_no_password():
    """Key-mode launches never write the tag; another tag must not count as one."""
    info = _info(freeform_tags={"ocibot_managed": "true", "ocibot_ssh_user": "root"})
    assert instance_to_dict(info)["has_root_password"] is False


def test_instance_without_any_tags_does_not_raise():
    assert instance_to_dict(_info(freeform_tags={}))["has_root_password"] is False


def test_non_string_tag_value_is_coerced():
    """Tags come back from the SDK; a surprising type must not break the list."""
    info = _info(freeform_tags={ROOT_PASSWORD_TAG: 12345})
    assert instance_to_dict(info)["has_root_password"] is True


def test_a_blank_tag_is_not_a_password():
    assert instance_to_dict(_info(freeform_tags={ROOT_PASSWORD_TAG: "  "}))["has_root_password"] is False
