import pytest

from app.oci_client import FirewallRuleSpec, sanitize_launch_payload, shape_display_label


BASE = {
    "compartment_id": "ocid1.compartment.test",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.test",
    "subnet_id": "ocid1.subnet.test",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAATEST user",
}


def test_shape_display_label_is_model_only():
    # Launch form shows the shape name alone — no OCPU / memory suffix.
    assert shape_display_label("VM.Standard.A1.Flex", ocpus=4, memory=24) == "VM.Standard.A1.Flex"
    assert shape_display_label("VM.Standard.E2.1.Micro", ocpus=1, memory=1) == "VM.Standard.E2.1.Micro"
    assert shape_display_label("") == "—"


def test_safe_payload_defaults_and_vpu_validation():
    clean = sanitize_launch_payload(BASE, for_retry=True)
    assert clean["assign_public_ip"] is True
    assert clean["boot_volume_vpus_per_gb"] == 10
    with pytest.raises(ValueError):
        sanitize_launch_payload({**BASE, "boot_volume_size_in_gbs": 49})
    with pytest.raises(ValueError):
        sanitize_launch_payload({**BASE, "boot_volume_size_in_gbs": 32769})
    with pytest.raises(ValueError):
        sanitize_launch_payload({**BASE, "boot_volume_vpus_per_gb": 25})


def test_password_cannot_be_retried_or_persisted():
    with pytest.raises(ValueError):
        sanitize_launch_payload({**BASE, "auth_mode": "password"}, for_retry=True)
    with pytest.raises(ValueError):
        sanitize_launch_payload({**BASE, "root_password": "secret"})


def test_firewall_rule_validation():
    FirewallRuleSpec("INGRESS", "6", "0.0.0.0/0", 22, 22).validate()
    FirewallRuleSpec("EGRESS", "all", "::/0").validate()
    with pytest.raises(ValueError):
        FirewallRuleSpec("INGRESS", "58", "0.0.0.0/0").validate()
    with pytest.raises(ValueError):
        FirewallRuleSpec("INGRESS", "6", "0.0.0.0/0", 0, 22).validate()
