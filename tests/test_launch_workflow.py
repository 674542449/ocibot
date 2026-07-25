from types import SimpleNamespace

from app.config_store import TenantConfig
from app.gui import OCIBotApp
from app.oci_client import OperationResult


class FakeSession:
    def __init__(self, ipv6_result=None):
        self.calls = []
        self.ipv6_result = ipv6_result or OperationResult(ok=True, message="ipv6 ready", data={})

    def ensure_subnet_ipv6(self, subnet_id, compartment_id):
        self.calls.append(("ipv6", subnet_id, compartment_id))
        return self.ipv6_result

    def create_managed_nsg(self, **kwargs):
        self.calls.append(("nsg", kwargs))
        return OperationResult(ok=True, message="nsg ready", data={"nsg_id": "nsg-1"})

    def launch_from_payload(self, payload, root_password=""):
        self.calls.append(("launch", dict(payload), root_password))
        return OperationResult(ok=True, message="launched", data={"instance_id": "inst-1"})

    def delete_managed_nsg(self, nsg_id):
        self.calls.append(("delete", nsg_id))
        return OperationResult(ok=True, message="deleted")


class FakeApp:
    _submit_launch = OCIBotApp._submit_launch

    def __init__(self, session):
        self.sessions = SimpleNamespace(get=lambda _tenant: session)
        self.logs = []
        self.statuses = []

    def _run_async(self, work, ok, err):
        try:
            self.worker_result = work()
        except Exception as exc:  # pragma: no cover - defensive parity with GUI
            err(exc)
            return
        self.ok_callback = ok

    def _set_status(self, value):
        self.statuses.append(value)

    def _log(self, message, level="info"):
        self.logs.append((level, message))


def _tenant():
    return TenantConfig(
        id="t1",
        name="tenant",
        user_ocid="ocid1.user.oc1..aaaaaaaa" + "b" * 40,
        tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaa" + "c" * 44,
        fingerprint="12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
        region="ap-tokyo-1",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n",
    )


def _result(assign_ipv6=True):
    return {
        "payload": {
            "display_name": "server",
            "compartment_id": "compute-comp",
            "network_compartment_id": "network-comp",
            "vcn_id": "vcn-1",
            "subnet_id": "subnet-1",
            "shape": "VM.Standard.A1.Flex",
            "auth_mode": "key",
            "assign_ipv6_ip": assign_ipv6,
        },
        "secrets": {"root_password": ""},
        "as_retry": False,
    }


def test_submit_launch_prepares_ipv6_before_nsg_and_launch():
    session = FakeSession()
    app = FakeApp(session)

    app._submit_launch(_tenant(), _result())

    assert [call[0] for call in session.calls] == ["ipv6", "nsg", "launch"]
    assert session.calls[1][1]["include_ipv6"] is True
    launch_payload = session.calls[2][1]
    assert launch_payload["assign_ipv6_ip"] is True
    assert launch_payload["managed_nsg_id"] == "nsg-1"
    assert launch_payload["nsg_ids"] == ["nsg-1"]


def test_submit_launch_stops_before_nsg_when_ipv6_preparation_fails():
    session = FakeSession(OperationResult(ok=False, message="route denied", data={"route_ok": False}))
    app = FakeApp(session)

    app._submit_launch(_tenant(), _result())

    assert [call[0] for call in session.calls] == ["ipv6"]
    assert not app.worker_result.ok
    assert app.worker_result.data["stage"] == "ipv6"
    assert app.worker_result.message == "IPv6 网络准备失败：route denied"


def test_submit_launch_without_ipv6_skips_network_preparation():
    session = FakeSession()
    app = FakeApp(session)

    app._submit_launch(_tenant(), _result(assign_ipv6=False))

    assert [call[0] for call in session.calls] == ["nsg", "launch"]
    assert session.calls[0][1]["include_ipv6"] is False
