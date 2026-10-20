"""用户端看不到后台的敏感信息（0.4.150）。

  * 接口的错误详情一律经 safe_error_text：OCID 打码，数据库报错（带 SQL、表名列名、
    绑定参数）只说「数据库操作失败」—— 原文进服务器日志。以前有 12 处直接
    ``detail=f"...: {exc}"``，绕开了这道闸；
  * 实例列表 / 详情不带 root 密码明文（只说有没有记），点「显示 / 复制」时单独取并记审计；
  * 系统状态里的部署细节（Worker 主机标识、提交号、配置项名）只给管理员；
  * 不发 ``server: uvicorn`` 响应头。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
OCID = "ocid1.compartment.oc1..aaaaaaaabbbbbbbbccccccccddddddddeeeeeeeeffffffffgggg"


def test_no_route_puts_raw_exception_text_into_a_response():
    """守住「绕开 safe_error_text」这种写法别再回来。"""
    # 只认异常变量名（exc / e / err）。OperationResult.message 之类的字符串已经由
    # TenantSession 格式化过，不在此列。
    pattern = re.compile(r'detail=f"[^"]*\{(exc|e|err)\}')
    hits = []
    for path in (ROOT / "web" / "backend").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "错误详情要经 safe_error_text：\n" + "\n".join(hits)


def test_a_failing_instance_list_does_not_leak_an_ocid(monkeypatch):
    from fastapi import HTTPException

    from web.backend.routers import instances as r

    def boom(**_kw):
        raise RuntimeError(f"ListInstances failed for compartment {OCID}")

    monkeypatch.setattr(r, "_tenant_or_404", lambda db, uid, tid: SimpleNamespace(id=tid, enabled=True, name="t"))
    monkeypatch.setattr(r, "get_session_for_row", lambda row: SimpleNamespace(list_instances_tree=boom))
    with pytest.raises(HTTPException) as exc:
        r.list_instances("t1", SimpleNamespace(id="u1"), None, include_subcompartments=True,
                         resolve_ips=False, include_terminated=False, response=None)
    assert OCID not in exc.value.detail
    assert "ListInstances failed" in exc.value.detail, "报错的意思要留着，只打码 OCID"


def test_database_errors_are_not_shown_but_are_logged(caplog):
    from sqlalchemy.exc import IntegrityError

    from app.oci_client import safe_error_text

    err = IntegrityError(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)", ("alice", "$2b$12$secret"), Exception("UNIQUE")
    )
    with caplog.at_level(logging.ERROR, logger="ocibot.errors"):
        text = safe_error_text(err)
    assert text == "数据库操作失败，详情见服务器日志"
    assert "INSERT" in caplog.text, "原文要进服务器日志，否则哪儿都查不到"


# ---------------------------------------------------------------- root 密码


def test_instance_listings_carry_no_root_password():
    from app.oci_client import ROOT_PASSWORD_TAG, InstanceInfo
    from web.backend.oci_bridge import instance_to_dict

    info = InstanceInfo(
        id="i1", display_name="vm", lifecycle_state="RUNNING", region="ap-tokyo-1",
        availability_domain="AD-1", fault_domain="", shape="VM.Standard.A1.Flex", ocpus=1,
        memory_gb=6, time_created="", compartment_id="c", image_id="img",
        freeform_tags={ROOT_PASSWORD_TAG: "S3cret-pass!"}, defined_tags={}, tenant_id="t", tenant_name="t",
    )
    out = instance_to_dict(info)
    assert out["has_root_password"] is True
    assert "root_password" not in out
    assert "S3cret-pass!" not in repr(out)


def test_the_password_is_fetched_on_demand_and_audited(monkeypatch):
    from app.oci_client import ROOT_PASSWORD_TAG
    from web.backend.routers import instances as r

    audits = []
    monkeypatch.setattr(r, "_tenant_or_404", lambda db, uid, tid: SimpleNamespace(id=tid, name="t"))
    monkeypatch.setattr(r, "get_session_for_row", lambda row: SimpleNamespace(
        get_instance=lambda iid, resolve_ips=True: SimpleNamespace(freeform_tags={ROOT_PASSWORD_TAG: "S3cret"})))
    monkeypatch.setattr(r, "write_audit", lambda db, **kw: audits.append(kw))
    out = r.get_root_password_note("t1", "i1", SimpleNamespace(id="u1"), None)
    assert out == {"root_password": "S3cret"}
    assert audits and audits[0]["action"] == "instance.root_password_view"
    assert "S3cret" not in repr(audits[0]), "审计里只记查看过，不记密码本身"


# ---------------------------------------------------------------- 系统状态 / 响应头


def test_deployment_details_are_for_admins_only(monkeypatch):
    from web.backend.routers import system as s

    monkeypatch.setattr(s, "get_meta", lambda db, key: "")
    plain = s.system_status(SimpleNamespace(id="u", is_admin=False), None)
    assert not {"worker_id", "git_sha", "background_oci_env"} & set(plain)
    assert "app_version" in plain and "worker_alive" in plain
    admin = s.system_status(SimpleNamespace(id="a", is_admin=True), None)
    assert {"worker_id", "git_sha", "background_oci_env"} <= set(admin)


def test_the_server_does_not_announce_its_software(monkeypatch):
    import uvicorn

    from web.backend import run

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    run.main()
    assert seen.get("server_header") is False
