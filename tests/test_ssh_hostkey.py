"""SSH host key verification (trust on first use).

Every guest-SSH path previously passed known_hosts=None, so anything answering on
the instance's address could impersonate it and harvest the SSH credentials the
user typed. These tests pin the TOFU behaviour and, critically, that the check
happens before any credential is transmitted.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_hostkey_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'hk.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "hostkey-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "hostkey-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")
pytest.importorskip("asyncssh")

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import SshHostKey, Tenant, User  # noqa: E402
from web.backend.ssh_hostkey import (  # noqa: E402
    LEARNED,
    MISMATCH,
    TRUSTED,
    fingerprint_of,
    forget_host_key,
    known_hosts_for,
    verify_host_key,
)


class _FakeKey:
    """Stands in for an asyncssh SSHKey."""

    def __init__(self, fingerprint: str, algorithm: bytes = b"ssh-ed25519"):
        self._fp = fingerprint
        self.algorithm = algorithm

    def get_fingerprint(self) -> str:
        return self._fp


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(SshHostKey).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _seed() -> tuple[str, str]:
    with SessionLocal() as db:
        user = User(username="hk", password_hash="x")
        db.add(user)
        db.flush()
        tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
        db.add(tenant)
        db.commit()
        return user.id, tenant.id


def _verify(owner_id: str, tenant_id: str, fp: str, *, instance="ocid1.instance.oc1..a", host="1.2.3.4", port=22):
    with SessionLocal() as db:
        return verify_host_key(
            db,
            owner_id=owner_id,
            instance_id=instance,
            port=port,
            server_key=_FakeKey(fp),
            host=host,
            tenant_id=tenant_id,
        )


def test_first_connection_learns_the_key():
    owner_id, tenant_id = _seed()
    result = _verify(owner_id, tenant_id, "SHA256:AAAA")
    assert result.verdict == LEARNED
    assert result.ok is True
    assert "SHA256:AAAA" in result.message()
    with SessionLocal() as db:
        row = db.query(SshHostKey).one()
        assert row.fingerprint == "SHA256:AAAA"
        assert row.key_type == "ssh-ed25519"


def test_second_connection_with_same_key_is_trusted():
    owner_id, tenant_id = _seed()
    _verify(owner_id, tenant_id, "SHA256:AAAA")
    result = _verify(owner_id, tenant_id, "SHA256:AAAA")
    assert result.verdict == TRUSTED
    assert result.ok is True
    assert result.message() == ""
    with SessionLocal() as db:
        assert db.query(SshHostKey).count() == 1


def test_changed_key_is_refused():
    """The MITM case: refuse rather than hand over credentials."""
    owner_id, tenant_id = _seed()
    _verify(owner_id, tenant_id, "SHA256:AAAA")
    result = _verify(owner_id, tenant_id, "SHA256:EVIL")
    assert result.verdict == MISMATCH
    assert result.ok is False
    assert "SHA256:AAAA" in result.message() and "SHA256:EVIL" in result.message()
    assert "中间人" in result.message()


def test_ip_change_alone_does_not_trip_the_check():
    """Keyed on instance OCID, so routine IP rotation is not treated as an attack.

    This is the whole reason verification was originally skipped.
    """
    owner_id, tenant_id = _seed()
    _verify(owner_id, tenant_id, "SHA256:AAAA", host="1.2.3.4")
    result = _verify(owner_id, tenant_id, "SHA256:AAAA", host="9.9.9.9")
    assert result.verdict == TRUSTED
    with SessionLocal() as db:
        assert db.query(SshHostKey).one().last_host == "9.9.9.9"


def test_records_are_per_user():
    """One user's remembered key must not authorize another user's connection."""
    owner_a, tenant_a = _seed()
    with SessionLocal() as db:
        other = User(username="hk2", password_hash="x")
        db.add(other)
        db.commit()
        owner_b = other.id
    _verify(owner_a, tenant_a, "SHA256:AAAA")
    result = _verify(owner_b, "", "SHA256:BBBB")
    assert result.verdict == LEARNED  # independent record, not a mismatch
    with SessionLocal() as db:
        assert db.query(SshHostKey).count() == 2


def test_different_ports_tracked_separately():
    owner_id, tenant_id = _seed()
    _verify(owner_id, tenant_id, "SHA256:AAAA", port=22)
    result = _verify(owner_id, tenant_id, "SHA256:CCCC", port=2222)
    assert result.verdict == LEARNED
    with SessionLocal() as db:
        assert db.query(SshHostKey).count() == 2


def test_unreadable_key_is_not_trusted():
    """No fingerprint means no basis for trust — fail closed."""
    owner_id, tenant_id = _seed()
    with SessionLocal() as db:
        result = verify_host_key(
            db,
            owner_id=owner_id,
            instance_id="ocid1.instance.oc1..a",
            port=22,
            server_key=None,
            host="1.2.3.4",
            tenant_id=tenant_id,
        )
    assert result.ok is False
    assert result.verdict == MISMATCH


def test_forget_allows_relearning_after_rebuild():
    """A legitimate OS reinstall changes the key; the user must not be locked out."""
    owner_id, tenant_id = _seed()
    _verify(owner_id, tenant_id, "SHA256:AAAA")
    assert _verify(owner_id, tenant_id, "SHA256:NEW").verdict == MISMATCH
    with SessionLocal() as db:
        removed = forget_host_key(db, owner_id=owner_id, instance_id="ocid1.instance.oc1..a")
    assert removed == 1
    assert _verify(owner_id, tenant_id, "SHA256:NEW").verdict == LEARNED


def test_known_hosts_pins_only_the_verified_key():
    key = _FakeKey("SHA256:AAAA")
    trusted, cas, revoked = known_hosts_for(key)
    assert trusted == [key]
    assert cas == [] and revoked == []
    # None must not become "trust everything".
    assert known_hosts_for(None) == ([], [], [])


def test_fingerprint_of_reads_algorithm_and_fingerprint():
    fp, kind = fingerprint_of(_FakeKey("SHA256:ZZZZ", algorithm=b"ecdsa-sha2-nistp256"))
    assert fp == "SHA256:ZZZZ"
    assert kind == "ecdsa-sha2-nistp256"
    assert fingerprint_of(None) == ("", "")


def test_probe_does_not_authenticate():
    """probe_host_key must use the KEX-only helper, never a full connect().

    Guards the ordering property the whole design rests on: if the host key were
    inspected after connect(), the credentials would already have been sent to a
    possible impostor.
    """
    import inspect

    import web.backend.ssh_hostkey as mod

    src = inspect.getsource(mod.probe_host_key)
    assert "get_server_host_key" in src
    assert "asyncssh.connect" not in src
