"""Drive every backend endpoint once with a stubbed OCI session.

A broad `except Exception -> HTTPException(502, str(exc))` sits on most OCI-facing
routes, so a plain coding error (a missing import, a renamed helper) surfaces as a
generic 502 rather than a crash — invisible to unit tests that never call the route.
This caught exactly that: a missing `quota_guard` import made boot-volume resize
return 502 for every request.

The stub is intentionally shallow. This asserts wiring, not OCI semantics.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

tmp = tempfile.mkdtemp(prefix="ocibot_smoke_")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{Path(tmp,'s.db').as_posix()}"
os.environ["OCIBOT_MASTER_KEY"] = "smoke-master-key-0123456789abcdefghij"
os.environ["OCIBOT_JWT_SECRET"] = "smoke-jwt-secret-0123456789abcdefghij"
sys.path.insert(0, os.path.abspath("."))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app.oci_client import InstanceInfo  # noqa: E402
from web.backend.main import app  # noqa: E402


class R:
    def __init__(self, ok=True, message="", data=None):
        self.ok = ok
        self.message = message
        self.data = data if data is not None else {}
        self.work_request_id = "wr-1"


def _inst(**kw):
    base = dict(
        id="ocid1.instance.oc1..i1",
        display_name="web-1",
        lifecycle_state="RUNNING",
        region="ap-tokyo-1",
        availability_domain="AD-1",
        fault_domain="FD-1",
        shape="VM.Standard.A1.Flex",
        ocpus=2.0,
        memory_gb=12.0,
        time_created="2026-01-01T00:00:00+00:00",
        compartment_id="ocid1.compartment.oc1..c1",
        image_id="ocid1.image.oc1..img",
        freeform_tags={},
        defined_tags={},
        tenant_id="t1",
        tenant_name="T",
    )
    base.update(kw)
    return InstanceInfo(**base)


VOL = {
    "id": "ocid1.bootvolume.oc1..bv1",
    "display_name": "bv-1",
    "size_in_gbs": 50,
    "vpus_per_gb": 10,
    "lifecycle_state": "AVAILABLE",
    "availability_domain": "AD-1",
    "instance_id": "ocid1.instance.oc1..i1",
    "instance_name": "web-1",
    "attachment_id": "ocid1.att.oc1..a1",
    "attachment_state": "ATTACHED",
    "performance_label": "均衡",
}
QUOTA = {
    "account_tier": "free",
    "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0},
    "remaining": {"a1_ocpu": 4.0, "a1_memory_gb": 24.0, "e2_micro_count": 2, "block_storage_gb": 200.0},
    "buckets": {
        "block_storage_gb": {"used": 0, "limit": 200, "remaining": 200, "ratio": 0.0, "status": "ok"},
        "object_storage_gb": {"used": 0, "limit": 20, "remaining": 20, "ratio": 0.0, "status": "ok"},
    },
    "overall_status": "ok",
    "read_incomplete": False,
}


def make_session():
    s = MagicMock()
    s.list_instances_tree.return_value = [_inst(), _inst(id="ocid1.instance.oc1..i2", lifecycle_state="STOPPED")]
    s.list_instances.return_value = s.list_instances_tree.return_value
    s.get_instance.return_value = _inst()
    s.test_connection.return_value = R(True, "ok")
    s.instance_action.return_value = R(True, "已提交")
    s.terminate_instance.return_value = R(True, "已终止")
    s.rename_instance.return_value = R(True, "已重命名")
    s.set_root_password_note.return_value = R(True, "已更新密码备注")
    s.update_instance_shape.return_value = R(True, "已提交")
    s.replace_ephemeral_public_ip.return_value = R(True, "已更换", {"new_ip": "1.2.3.4"})
    s.assign_public_ipv6.return_value = R(True, "ok", {})
    s.get_instance_metrics.return_value = R(True, "", {"cpu": [], "network": []})
    s.get_account_status.return_value = R(True, "", {"tier_code": "free", "tier": "Always Free"})
    s.get_usage_summary.return_value = R(True, "", {"total": 0.0, "currency": "USD", "days": []})
    s.get_free_quota_usage.return_value = R(True, "", dict(QUOTA))
    s.list_boot_volumes.return_value = R(True, "", {"volumes": [dict(VOL)]})
    s.list_block_volumes.return_value = R(True, "", {"volumes": [dict(VOL, kind="block")]})
    s.get_boot_volume_info.return_value = R(True, "", {"size_in_gbs": 50, "vpus_per_gb": 10})
    s.resize_boot_volume.return_value = R(True, "已调整", {})
    s.create_block_volume.return_value = R(True, "已创建", {"id": "ocid1.volume.oc1..v1"})
    s.update_block_volume.return_value = R(True, "已更新", {})
    s.delete_block_volume.return_value = R(True, "已删除")
    s.attach_volume.return_value = R(True, "已挂载", {})
    s.detach_volume.return_value = R(True, "已卸载")
    s.list_volume_attachments.return_value = R(True, "", {"attachments": []})
    s.list_boot_volume_backups.return_value = [{"id": "b1", "display_name": "bk"}]
    s.create_boot_volume_backup.return_value = R(True, "已创建备份", {})
    s.delete_boot_volume_backup.return_value = R(True, "已删除")
    s.list_custom_images.return_value = [{"id": "img1", "display_name": "custom"}]
    s.delete_custom_image.return_value = R(True, "已删除")
    s.get_object_namespace.return_value = R(True, "", {"namespace": "ns"})
    s.list_buckets.return_value = R(True, "", {"buckets": [{"name": "b1"}], "namespace": "ns"})
    s.create_bucket.return_value = R(True, "已创建", {})
    s.delete_bucket.return_value = R(True, "已删除")
    s.list_objects.return_value = R(True, "", {"objects": [{"name": "a/b.txt", "size": 10}]})
    s.put_object.return_value = R(True, "已上传", {})
    s.delete_object.return_value = R(True, "已删除")
    s.list_console_connections.return_value = []
    s.create_console_connection.return_value = R(True, "已创建", {"serial": "ssh -o x", "vnc": "vnc"})
    s.delete_console_connection.return_value = R(True, "已删除")
    s.get_instance_firewall.return_value = R(True, "", {"groups": []})
    s.add_instance_firewall_rule.return_value = R(True, "已添加", {})
    s.delete_nsg_rules.return_value = R(True, "已删除", {})
    s.replace_instance_firewall_with_open_all.return_value = R(True, "已全开放", {})
    s.list_reserved_public_ips.return_value = [{"id": "pip1", "ip_address": "1.1.1.1"}]
    s.create_reserved_public_ip.return_value = R(True, "已创建", {"ip_address": "1.1.1.1"})
    s.delete_reserved_public_ip.return_value = R(True, "已删除", {})
    s.attach_reserved_public_ip.return_value = R(True, "已绑定", {})
    s.detach_reserved_public_ip.return_value = R(True, "已解绑", {})
    s.home_region.return_value = "ap-tokyo-1"
    s.list_subscribed_regions.return_value = R(
        True,
        "",
        {
            "home_region": "ap-tokyo-1",
            "regions": [
                {"region_name": "ap-tokyo-1", "region_key": "nrt", "status": "READY", "is_home_region": True}
            ],
        },
    )
    s.list_all_regions.return_value = R(
        True,
        "",
        {
            "regions": [
                {"region_name": "ap-tokyo-1", "region_key": "nrt"},
                {"region_name": "ap-osaka-1", "region_key": "kix"},
            ]
        },
    )
    s.subscribe_region.return_value = R(
        True, "已提交开通", {"region_name": "ap-osaka-1", "region_key": "kix", "already": False}
    )
    s.list_console_password_policies.return_value = R(True, "", {"policies": []})
    s.get_console_password_status.return_value = R(
        True,
        "",
        {
            "domains": [],
            "policies": [{"id": "p1", "name": "Default", "password_expires_after": None}],
            "user": {"found": True, "user_name": "u", "last_set": "", "cant_expire": False},
            "effective": {"expires": False, "summary": "永不过期（策略未设置有效期）"},
            "errors": [],
        },
    )
    s.disable_console_password_expiry.return_value = R(True, "已关闭", {})
    s.list_invoices.return_value = R(True, "已读取 0 张账单", {"invoices": [], "unavailable": False})
    s.resolve_compartment.return_value = "ocid1.compartment.oc1..c1"
    s.launch_from_payload.return_value = R(True, "创建成功", {"instance_id": "ocid1.instance.oc1..new"})
    s.launch_instance.return_value = s.launch_from_payload.return_value
    s.create_managed_nsg.return_value = R(True, "", {"nsg_id": "nsg-new"})
    s.delete_managed_nsg.return_value = R(True, "")
    s.ensure_default_network.return_value = R(True, "", {"vcn": {"id": "vcn1"}, "subnet": {"id": "sub1"}})
    s.ensure_subnet_ipv6.return_value = R(True, "")
    return s


_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"

SESSION = make_session()

import web.backend.oci_bridge as bridge  # noqa: E402

bridge.get_session_for_row = lambda row: SESSION
for mod in (
    "web.backend.routers.instances",
    "web.backend.routers.instance_ops",
    "web.backend.routers.storage",
    "web.backend.routers.tenants",
    "web.backend.routers.jobs",
    "web.backend.routers.webssh",
):
    __import__(mod)
    m = sys.modules[mod]
    if hasattr(m, "get_session_for_row"):
        m.get_session_for_row = lambda row: SESSION

# Bound to a local, NOT assigned onto launch_service. Overwriting the shared
# module attribute here leaked into every other test module in the session —
# tests/test_launch_meta_shared_cache.py was silently exercising this stub
# instead of the real cache. The route reads its own imported name, patched
# below, so only that binding needs replacing.
_stub_launch_meta = lambda session, *, tenant_id, force=False: {
    "compartments": [{"id": "ocid1.compartment.oc1..c1", "name": "root"}],
    "ads": ["AD-1", "AD-2"],
    "images": [{"id": "ocid1.image.oc1..img", "display_name": "Ubuntu 24.04"}],
    "images_by_os": {"ubuntu": [{"id": "ocid1.image.oc1..img", "display_name": "Ubuntu 24.04"}]},
    "os_families": [{"id": "ubuntu", "operating_system": "Canonical Ubuntu"}],
    "shapes": [{"shape": "VM.Standard.A1.Flex"}],
    "all_shapes": [{"shape": "VM.Standard.A1.Flex"}],
    "vcns": [{"id": "vcn1", "display_name": "vcn"}],
    "subnets_by_vcn": {"vcn1": [{"id": "sub1", "vcn_id": "vcn1", "compartment_id": "ocid1.compartment.oc1..c1"}]},
    "default_compartment": "ocid1.compartment.oc1..c1",
    "preferred_vcn_id": "vcn1",
    "preferred_subnet_id": "sub1",
    "quick_presets": [],
    "boot_vpu_presets": [{"value": 10, "label": "均衡"}],
    "free_tier_shapes": {"VM.Standard.A1.Flex": "Always Free"},
    "defaults": {"retry_interval_sec": 180, "retry_max_attempts": 200, "display_name": "i"},
    "cached": False,
    "cache_age_sec": 0,
}
import web.backend.routers.instances as inst_router  # noqa: E402

inst_router.fetch_launch_meta = _stub_launch_meta
inst_router.prepare_launch_network = lambda session, payload, *, meta=None, for_retry=False: payload
inst_router.schedule_post_launch_adjustments = lambda *a, **k: None


def test_every_endpoint_is_wired() -> None:
    """No endpoint may answer 5xx with a healthy stub."""
    failures: list[str] = []

    def check(method: str, path: str, resp) -> None:
        if resp.status_code >= 500:
            failures.append(f"{resp.status_code} {method} {path} -> {resp.text[:200]}")

    with TestClient(app) as c:
        # Seed + log in directly rather than via /auth/register: other test modules
        # may share this engine's database (DATABASE_URL is set with setdefault at
        # import time), and self-registration closes after the first user.
        from sqlalchemy import select

        from web.backend.auth import hash_password
        from web.backend.db import SessionLocal
        from web.backend.models import User

        with SessionLocal() as db:
            existing = db.scalar(select(User).where(User.username == "smoke"))
            if existing is None:
                db.add(User(username="smoke", password_hash=hash_password("supersecret123")))
            else:
                existing.password_hash = hash_password("supersecret123")
                existing.is_active = True
                existing.totp_enabled = False
            db.commit()
        r = c.post("/api/auth/login", json={"username": "smoke", "password": "supersecret123"})
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"

        r = c.post(
            "/api/tenants",
            json={
                "name": "T",
                "user_ocid": "ocid1.user.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
                "tenancy_ocid": "ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
                "fingerprint": "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                "region": "ap-tokyo-1",
                "private_key_pem": _PEM,
            },
        )
        assert r.status_code < 300, r.text
        tid = r.json()["id"]
        iid = "ocid1.instance.oc1..i1"

        for p in [
            "/api/health", "/api/auth/me", "/api/tenants", "/api/audit", "/api/notifications",
            "/api/system/status", "/api/admin/users", "/api/admin/settings", "/api/admin/update",
            "/api/jobs/capacity",
            f"/api/tenants/{tid}", f"/api/tenants/{tid}/instances",
            f"/api/tenants/{tid}/instances/{iid}", f"/api/tenants/{tid}/account",
            f"/api/tenants/{tid}/usage", f"/api/tenants/{tid}/free-quota",
            f"/api/tenants/{tid}/invoices",
            f"/api/tenants/{tid}/launch-meta", f"/api/tenants/{tid}/boot-volumes",
            f"/api/tenants/{tid}/block-volumes", f"/api/tenants/{tid}/reserved-ips",
            f"/api/tenants/{tid}/boot-volume-backups", f"/api/tenants/{tid}/custom-images",
            f"/api/tenants/{tid}/object-storage/namespace",
            f"/api/tenants/{tid}/object-storage/buckets",
            f"/api/tenants/{tid}/object-storage/buckets/b1/objects",
            f"/api/tenants/{tid}/oci-password-policy",
            f"/api/tenants/{tid}/regions",
            f"/api/tenants/{tid}/instances/{iid}/console",
            f"/api/tenants/{tid}/instances/{iid}/firewall",
            f"/api/tenants/{tid}/instances/{iid}/boot-volume",
            f"/api/tenants/{tid}/instances/{iid}/metrics",
            f"/api/tenants/{tid}/instances/{iid}/volume-attachments",
            f"/api/tenants/{tid}/instances/{iid}/host-key",
        ]:
            check("GET", p, c.get(p))

        # The one PUT in the API. Covered here for the same reason as the rest:
        # a coding error in an OCI-facing route surfaces as a generic 5xx that
        # unit tests calling the function directly never see.
        check(
            "PUT",
            "/api/auth/locked-tenant",
            c.put("/api/auth/locked-tenant", json={"tenant_id": tid}),
        )

        posts = [
            (f"/api/tenants/{tid}/test", {}),
            (f"/api/tenants/{tid}/regions/subscribe",
             {"region": "ap-osaka-1", "confirm": True, "add_tenant": True}),
            (f"/api/tenants/{tid}/instances/{iid}/power", {"action": "SOFTSTOP"}),
            (f"/api/tenants/{tid}/instances/{iid}/rename", {"display_name": "renamed"}),
            (f"/api/tenants/{tid}/instances/{iid}/root-password", {"root_password": "NewPass123!"}),
            (f"/api/tenants/{tid}/instances/{iid}/root-password", {"root_password": ""}),
            (f"/api/tenants/{tid}/instances/{iid}/shape", {"ocpus": 2, "memory_in_gbs": 12}),
            (f"/api/tenants/{tid}/instances/{iid}/public-ip/replace", None),
            (f"/api/tenants/{tid}/instances/{iid}/ipv6", None),
            (f"/api/tenants/{tid}/instances/{iid}/console",
             {"ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfake"}),
            (f"/api/tenants/{tid}/instances/{iid}/firewall/rules",
             {"nsg_id": "nsg1", "direction": "INGRESS", "protocol": "6", "cidr": "0.0.0.0/0",
              "port_min": 80, "port_max": 80}),
            (f"/api/tenants/{tid}/instances/{iid}/firewall/delete-rules",
             {"nsg_id": "nsg1", "rule_ids": ["r1"]}),
            (f"/api/tenants/{tid}/instances/{iid}/firewall/open-all", None),
            # Regression: this returned 502 "name 'quota_guard' is not defined".
            (f"/api/tenants/{tid}/instances/{iid}/boot-volume", {"size_in_gbs": 60}),
            (f"/api/tenants/{tid}/reserved-ips", {"display_name": "pip"}),
            (f"/api/tenants/{tid}/instances/{iid}/reserved-ip/attach", {"public_ip_id": "pip1"}),
            (f"/api/tenants/{tid}/reserved-ips/pip1/detach", None),
            (f"/api/tenants/{tid}/boot-volume-backups",
             {"boot_volume_id": "ocid1.bootvolume.oc1..bv1"}),
            (f"/api/tenants/{tid}/block-volumes", {"availability_domain": "AD-1", "size_in_gbs": 50}),
            (f"/api/tenants/{tid}/block-volumes/v1/update", {"size_in_gbs": 60}),
            (f"/api/tenants/{tid}/block-volumes/v1/attach", {"instance_id": iid}),
            (f"/api/tenants/{tid}/block-volumes/detach", {"attachment_id": "a1"}),
            (f"/api/tenants/{tid}/object-storage/buckets", {"name": "b2"}),
            ("/api/notifications",
             {"kind": "telegram", "name": "tg", "config": {"bot_token": "123:abc", "chat_id": "1"}}),
            (f"/api/tenants/{tid}/launch",
             {"shape": "VM.Standard.A1.Flex", "image_id": "ocid1.image.oc1..img", "auth_mode": "key",
              "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfake", "ocpus": 2,
              "memory_in_gbs": 12}),
        ]
        for p, body in posts:
            check("POST", p, c.post(p, json=body) if body is not None else c.post(p))

        for p in [
            f"/api/tenants/{tid}/instances/{iid}/host-key",
            f"/api/tenants/{tid}/object-storage/buckets/b1/objects/a%2Fb.txt",
            f"/api/tenants/{tid}/object-storage/buckets/b1",
            f"/api/tenants/{tid}/block-volumes/v1",
            f"/api/tenants/{tid}/boot-volume-backups/b1",
            f"/api/tenants/{tid}/custom-images/img1",
            f"/api/tenants/{tid}/reserved-ips/pip1",
        ]:
            check("DELETE", p, c.delete(p))

        # create-image answers 403 by design (CLAUDE.md), but "refuses" and
        # "raises on the way to refusing" are different things and only one of
        # them is acceptable.
        p = f"/api/tenants/{tid}/instances/{iid}/create-image"
        check("POST", p, c.post(p, json={"display_name": "img"}))

        p = f"/api/tenants/{tid}/oci-password-policy/disable-expiry"
        check("POST", p, c.post(p, json={"policy_id": "p1"}))

        # Admin reset targets a SECOND user on purpose: the route refuses to reset
        # the caller's own password, so pointing it at ourselves would exercise the
        # 400 guard instead of the code path that actually does the work.
        with SessionLocal() as db:
            victim = db.scalar(select(User).where(User.username == "smoke-victim"))
            if victim is None:
                victim = User(username="smoke-victim", password_hash=hash_password("throwaway123"))
                db.add(victim)
                db.commit()
            victim_id = victim.id
        p = f"/api/admin/users/{victim_id}/reset-password"
        check("POST", p, c.post(p))

        # --- session-ending routes last: each one invalidates the cookie the
        # --- preceding requests were relying on.
        p = "/api/auth/change-password"
        r = c.post(p, json={"old_password": "supersecret123", "new_password": "supersecret456"})
        check("POST", p, r)
        # Not just "no 5xx": a change-password that returns 200 without actually
        # changing anything looks identical from the status code alone.
        assert r.status_code == 200, f"change-password: {r.status_code} {r.text}"

        check("POST", "/api/auth/logout", c.post("/api/auth/logout"))

        r = c.post("/api/auth/login", json={"username": "smoke", "password": "supersecret456"})
        assert r.status_code == 200, f"new password does not work: {r.status_code} {r.text}"

        check("POST", "/api/auth/logout-all", c.post("/api/auth/logout-all"))

    assert not failures, "endpoints returned 5xx:\n" + "\n".join(failures)
