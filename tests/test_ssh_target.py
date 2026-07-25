"""SSH target allowlist / username validation."""

from types import SimpleNamespace

import pytest

from web.backend.ssh_bridge import (
    SshTarget,
    assert_host_allowed,
    resolve_instance_ssh_target,
    validate_ssh_auth,
    validate_ssh_username,
)


def test_validate_username():
    assert validate_ssh_username("ubuntu") == "ubuntu"
    with pytest.raises(ValueError):
        validate_ssh_username("bad;user")
    with pytest.raises(ValueError):
        validate_ssh_username("")


def test_validate_ssh_auth_xor():
    a = validate_ssh_auth(username="ubuntu", private_key_pem="KEY", password=None)
    assert a["auth_mode"] == "key"
    b = validate_ssh_auth(username="ubuntu", private_key_pem=None, password="x")
    assert b["auth_mode"] == "password"
    with pytest.raises(ValueError):
        validate_ssh_auth(username="ubuntu", private_key_pem="K", password="p")
    with pytest.raises(ValueError):
        validate_ssh_auth(username="ubuntu")


def test_assert_host_allowed():
    t = SshTarget(host="1.2.3.4", public_ip="1.2.3.4", private_ip="10.0.0.5", allowed_ips={"1.2.3.4", "10.0.0.5"})
    assert_host_allowed("1.2.3.4", t)
    with pytest.raises(ValueError):
        assert_host_allowed("8.8.8.8", t)


def test_resolve_instance_ssh_target():
    class Sess:
        def get_instance(self, iid, resolve_ips=True):
            return SimpleNamespace(
                id=iid,
                display_name="vm",
                lifecycle_state="RUNNING",
                public_ip="203.0.113.9",
                private_ip="10.0.0.9",
                ipv6_addresses=[],
            )

    target = resolve_instance_ssh_target(Sess(), "ocid1.instance..x")
    assert target.host == "203.0.113.9"
    assert "10.0.0.9" in target.allowed_ips
