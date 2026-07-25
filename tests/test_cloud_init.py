import base64

import pytest

from app.oci_client import build_root_cloud_init


def decode(**kwargs):
    return base64.b64decode(build_root_cloud_init(**kwargs)).decode("utf-8")


def test_key_cloud_init_keeps_default_and_opens_guest_firewall():
    raw = decode(auth_mode="key", ssh_public_key="ssh-ed25519 AAAATEST user")
    assert "  - default" in raw
    assert "PermitRootLogin prohibit-password" in raw
    assert "PasswordAuthentication no" in raw
    assert "ufw --force disable" in raw


def test_cloud_init_grows_root_filesystem_regardless_of_firewall():
    # A resized Boot Volume must be usable in-OS even when the guest firewall stays on.
    raw = decode(auth_mode="key", ssh_public_key="ssh-ed25519 AAAATEST user", open_guest_firewall=False)
    assert "growpart /dev/sda 1" in raw
    assert "resize2fs" in raw


def test_password_is_hashed_and_retry_secret_never_plaintext():
    pytest.importorskip("passlib")
    password = "correct-horse-battery-staple"
    raw = decode(auth_mode="password", root_password=password, open_guest_firewall=False)
    assert password not in raw
    assert "passwd: '$6$rounds=" in raw
    assert "PasswordAuthentication yes" in raw
    assert "iptables -F" not in raw


def test_password_dropin_wins_over_cloudimg_defaults():
    # Our sshd drop-in must sort before Ubuntu's 50/60-cloudimg files (first value
    # wins in sshd), and a fixup script must force password auth on regardless.
    raw = decode(auth_mode="password", root_password="correct-horse-1234")
    assert "/etc/ssh/sshd_config.d/00-ocibot-root.conf" in raw
    assert "99-ocibot-root.conf" not in raw
    assert "/var/lib/ocibot-sshfix.sh" in raw
    assert "PermitRootLogin yes" in raw


def test_password_is_set_at_runtime_via_chpasswd_encrypted():
    # The reliable path: set root's password directly with chpasswd -e using the
    # pre-hashed value (never plaintext), and restart sshd in the fixup script.
    password = "correct-horse-1234"
    raw = decode(auth_mode="password", root_password=password)
    assert "chpasswd -e" in raw
    assert "echo 'root:$6$rounds=" in raw
    assert password not in raw
    assert "systemctl restart ssh" in raw


def test_key_mode_has_no_password_fixup_script():
    raw = decode(auth_mode="key", ssh_public_key="ssh-ed25519 AAAATEST user")
    assert "ocibot-sshfix.sh" not in raw
    assert "/etc/ssh/sshd_config.d/00-ocibot-root.conf" in raw
