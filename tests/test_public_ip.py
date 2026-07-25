from types import SimpleNamespace

from app.oci_client import PrimaryNetworkInfo, TenantSession


class FakeNetwork:
    def __init__(self):
        self.deleted = []
        self.created = []

    def get_subnet(self, _subnet_id):
        return SimpleNamespace(data=SimpleNamespace(prohibit_public_ip_on_vnic=False))

    def delete_public_ip(self, public_ip_id):
        self.deleted.append(public_ip_id)

    def create_public_ip(self, details):
        self.created.append(details)
        return SimpleNamespace(data=SimpleNamespace(id="new-id", ip_address="203.0.113.10"))


def make_session(info):
    session = TenantSession.__new__(TenantSession)
    session._network = FakeNetwork()
    session.resolve_primary_network = lambda *_args, **_kwargs: info
    return session


def test_reserved_public_ip_is_never_deleted():
    info = PrimaryNetworkInfo(
        subnet_id="subnet",
        private_ip_id="private",
        private_ip_compartment_id="compartment",
        public_ip_id="reserved",
        public_ipv4="203.0.113.5",
        public_ip_lifetime="RESERVED",
    )
    session = make_session(info)
    result = session.replace_ephemeral_public_ip("instance", "compartment")
    assert not result.ok
    assert session._network.deleted == []
    assert session._network.created == []


def test_missing_public_ip_gets_new_ephemeral_address():
    info = PrimaryNetworkInfo(
        subnet_id="subnet",
        private_ip_id="private",
        private_ip_compartment_id="compartment",
    )
    session = make_session(info)
    result = session.replace_ephemeral_public_ip("instance", "compartment")
    assert result.ok
    assert result.data["new_ip"] == "203.0.113.10"
    assert session._network.created[0].lifetime == "EPHEMERAL"
    assert session._network.created[0].private_ip_id == "private"
