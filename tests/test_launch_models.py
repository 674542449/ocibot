from types import SimpleNamespace

from app.oci_client import ROOT_PASSWORD_TAG, TenantSession, generate_root_password


class FakeCompute:
    def __init__(self):
        self.details = None

    def launch_instance(self, details, **_kwargs):
        self.details = details
        return SimpleNamespace(
            data=SimpleNamespace(
                id="ocid1.instance.test",
                display_name="test",
                lifecycle_state="PROVISIONING",
            ),
            headers={},
        )


def test_launch_maps_ipv6_nsg_vpu_and_root_metadata():
    session = TenantSession.__new__(TenantSession)
    session._compute = FakeCompute()
    result = session.launch_instance(
        display_name="test",
        compartment_id="ocid1.compartment.test",
        availability_domain="AD-1",
        shape="VM.Standard.A1.Flex",
        image_id="ocid1.image.test",
        subnet_id="ocid1.subnet.test",
        ssh_public_key="ssh-ed25519 AAAATEST user",
        auth_mode="key",
        ocpus=1,
        memory_in_gbs=6,
        assign_public_ip=True,
        assign_ipv6_ip=True,
        boot_volume_size_in_gbs=50,
        boot_volume_vpus_per_gb=20,
        nsg_ids=["ocid1.networksecuritygroup.test"],
    )
    assert result.ok, result.message
    details = session._compute.details
    assert details.create_vnic_details.assign_public_ip is True
    assert details.create_vnic_details.assign_ipv6_ip is True
    assert details.create_vnic_details.nsg_ids == ["ocid1.networksecuritygroup.test"]
    assert details.source_details.boot_volume_vpus_per_gb == 20
    assert details.freeform_tags["ocibot_ssh_user"] == "root"
    assert ROOT_PASSWORD_TAG not in details.freeform_tags
    assert "user_data" in details.metadata


def _launch(session, **overrides):
    args = dict(
        display_name="t",
        compartment_id="ocid1.compartment.test",
        availability_domain="AD-1",
        shape="VM.Standard.A1.Flex",
        image_id="ocid1.image.test",
        subnet_id="ocid1.subnet.test",
        ssh_public_key="ssh-ed25519 AAAATEST user",
        auth_mode="key",
        ocpus=1,
        memory_in_gbs=6,
    )
    args.update(overrides)
    return session.launch_instance(**args)


def test_launch_size_is_passed_through_untouched():
    # VPU is applied post-launch via resize_boot_volume, so launch keeps the user's
    # size as-is (no auto-pinning).
    session = TenantSession.__new__(TenantSession)
    session._compute = FakeCompute()
    result = _launch(session, boot_volume_size_in_gbs=None, boot_volume_vpus_per_gb=120)
    assert result.ok, result.message
    assert session._compute.details.source_details.boot_volume_size_in_gbs is None
    assert session._compute.details.source_details.boot_volume_vpus_per_gb == 120


def test_explicit_size_is_passed_through():
    session = TenantSession.__new__(TenantSession)
    session._compute = FakeCompute()
    result = _launch(session, boot_volume_size_in_gbs=200, boot_volume_vpus_per_gb=120)
    assert result.ok, result.message
    assert session._compute.details.source_details.boot_volume_size_in_gbs == 200


def test_generate_root_password_strength():
    pwd = generate_root_password(16)
    assert len(pwd) == 16
    assert any(c.isupper() for c in pwd)
    assert any(c.islower() for c in pwd)
    assert any(c.isdigit() for c in pwd)
    assert any(c in "!@#%^*-_=+" for c in pwd)
    # Distinct calls should almost always differ.
    assert generate_root_password(16) != generate_root_password(16)


def test_password_launch_writes_root_password_tag():
    session = TenantSession.__new__(TenantSession)
    session._compute = FakeCompute()
    password = "CorrectHorse!234"
    result = session.launch_instance(
        display_name="pw-host",
        compartment_id="ocid1.compartment.test",
        availability_domain="AD-1",
        shape="VM.Standard.E2.1.Micro",
        image_id="ocid1.image.test",
        subnet_id="ocid1.subnet.test",
        auth_mode="password",
        root_password=password,
    )
    assert result.ok, result.message
    tags = session._compute.details.freeform_tags
    assert tags[ROOT_PASSWORD_TAG] == password
    assert tags["ocibot_ssh_user"] == "root"
    # Plaintext password must not appear in cloud-init user_data.
    assert password not in (session._compute.details.metadata.get("user_data") or "")
