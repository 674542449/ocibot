"""0.4.86 全功能逻辑审计修复的回归测试。

每一条都对应一个「代码跑得通、但结论是错的」的缺陷 —— 类型检查和冒烟测试都
抓不到这一类，只能靠断言业务结论本身。
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app.oci_client import build_root_cloud_init, is_transient_error
from web.backend.quota_guard import enforce_secondary_region


# ---------------------------------------------------------------------------
# 1. 副区判定：一次成功的读取必须压过 DB 的 secondary_hint
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, current: str, home: str) -> None:
        self.tenant = SimpleNamespace(region=current)
        self._home = home

    def home_region(self) -> str:
        return self._home


def test_a_child_row_whose_region_is_the_home_region_is_not_treated_as_secondary():
    """曾经的 CRITICAL：主区被加成「副区」行后，免费额度守卫在**唯一存在
    Always Free 的区域**里被整段跳过。

    症状是一条自相矛盾的提示「副区「ap-tokyo-1」…（主区 ap-tokyo-1）」，
    而 POST /launch 在额度已经 100% 用完的账号上照样返回 200。
    """
    session = _Session("ap-tokyo-1", "ap-tokyo-1")

    # secondary_hint=True 模拟 DB 里那条带 parent_tenant_id 的子行。
    assert enforce_secondary_region(session, free_only_mode=False, secondary_hint=True) == ""


def test_the_db_hint_still_covers_a_failed_region_read():
    """hint 的正当职责没有被削掉：读不出区域时它仍然生效。"""

    class _Broken:
        tenant = SimpleNamespace(region="")

        def home_region(self) -> str:
            raise RuntimeError("no permission")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        enforce_secondary_region(_Broken(), free_only_mode=True, secondary_hint=True)
    assert excinfo.value.status_code == 400

    # free_only 关掉时降级成一句警告，而不是放行得无声无息。
    warning = enforce_secondary_region(_Broken(), free_only_mode=False, secondary_hint=True)
    assert warning


def test_a_genuine_secondary_region_is_still_detected():
    from fastapi import HTTPException

    session = _Session("us-ashburn-1", "ap-tokyo-1")
    with pytest.raises(HTTPException):
        enforce_secondary_region(session, free_only_mode=True, secondary_hint=False)
    assert enforce_secondary_region(session, free_only_mode=False, secondary_hint=False)


# ---------------------------------------------------------------------------
# 2. 抢机错误分类：传输层抖动 ≠ 永久失败
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[503] ServiceUnavailable",
        "[500] InternalServerError",
        "HTTPSConnectionPool(...): Read timed out.",
        "Connection aborted, RemoteDisconnected",
        "Temporary failure in name resolution",
    ],
)
def test_transport_failures_are_transient(text: str):
    assert is_transient_error(None, text), text


@pytest.mark.parametrize(
    "text",
    [
        "[400] InvalidParameter: shape does not support 5 ocpus",
        "[404] NotAuthorizedOrNotFound",
        "[401] NotAuthenticated",
        "LimitExceeded: you have reached your service limit",
    ],
)
def test_configuration_failures_are_not_transient(text: str):
    """认不出来的一律按永久错误：拿着错参数无限重发比停下来更糟。"""
    assert not is_transient_error(None, text), text


def test_a_5xx_status_on_the_exception_counts():
    assert is_transient_error(SimpleNamespace(status=503, code="", message=""), "")
    assert not is_transient_error(SimpleNamespace(status=400, code="", message=""), "")


# ---------------------------------------------------------------------------
# 3. cloud-init：公钥必须是带引号的 YAML 标量
# ---------------------------------------------------------------------------

_BLOB = "AAAAB3NzaC1yc2EAAAADAQABAAABgQC" + "x" * 60


def _cloud_config(pubkey: str):
    """build_root_cloud_init 返回的是 base64，解出来再按 YAML 解析。"""
    yaml = pytest.importorskip("yaml")
    raw = build_root_cloud_init(auth_mode="key", ssh_public_key=pubkey)
    return yaml.safe_load(base64.b64decode(raw).decode("utf-8"))


def test_a_key_comment_containing_a_colon_cannot_break_the_cloud_config():
    """裸标量下 `- ssh-rsa AAA foo: bar` 会被 YAML 读成一个映射，
    整份 cloud-config 装不上，结果是一台连不上去的机器。"""
    doc = _cloud_config(f"ssh-rsa {_BLOB} note: not a mapping")

    root = next(u for u in doc["users"] if isinstance(u, dict) and u.get("name") == "root")
    keys = root["ssh_authorized_keys"]
    assert keys == [f"ssh-rsa {_BLOB} note: not a mapping"], keys


def test_a_key_comment_containing_a_hash_is_not_truncated():
    doc = _cloud_config(f"ssh-rsa {_BLOB} me@host #1")

    root = next(u for u in doc["users"] if isinstance(u, dict) and u.get("name") == "root")
    assert root["ssh_authorized_keys"] == [f"ssh-rsa {_BLOB} me@host #1"]


def test_an_apostrophe_in_the_comment_survives():
    doc = _cloud_config(f"ssh-rsa {_BLOB} o'brien@host")

    root = next(u for u in doc["users"] if isinstance(u, dict) and u.get("name") == "root")
    assert root["ssh_authorized_keys"] == [f"ssh-rsa {_BLOB} o'brien@host"]


# ---------------------------------------------------------------------------
# 4. 超出免费额度的 soft 桶不该说「接近上限」
# ---------------------------------------------------------------------------


def test_a_soft_bucket_over_its_cap_reports_over_not_critical():
    """前端把 critical 显示成「接近上限」。一个 3/2 个公网 IP 的桶，
    徽章写「接近上限」而紧挨着的数字写着 3 / 2 —— 互相矛盾。"""
    from app.free_quota import build_quota_snapshot

    # 15000 GB 对 10240 GB 的免费出网额度 —— 超出部分是**要计费**的。
    snap = build_quota_snapshot(
        instances=[], volumes=[], egress_usage={"egress_gb": 15000}
    )
    assert snap["buckets"]["egress_gb"]["status"] == "over"
    # soft 桶依然不参与硬性 overall —— 降级的正当目的由汇总循环独立承担。
    assert snap["overall_status"] != "over"


# ---------------------------------------------------------------------------
# 5. Server酱：200 + code!=0 是失败
# ---------------------------------------------------------------------------


def test_serverchan_reports_a_nonzero_code_as_a_failure(monkeypatch):
    """SendKey 过期、当天次数用完都是 `200 + {"code":4000x}`。
    以前一律报「已发送」，「测试」按钮跟着报绿。"""
    import web.backend.notify as notify

    monkeypatch.setattr(
        notify,
        "_post",
        lambda *a, **k: (200, json.dumps({"code": 40001, "message": "bad pushtoken"})),
    )
    ok, detail = notify._send_serverchan({"send_key": "SCTxxx"}, "t", "b")
    assert ok is False
    assert "40001" in detail


def test_serverchan_still_accepts_a_zero_code(monkeypatch):
    import web.backend.notify as notify

    monkeypatch.setattr(
        notify, "_post", lambda *a, **k: (200, json.dumps({"code": 0, "data": {}}))
    )
    assert notify._send_serverchan({"send_key": "SCTxxx"}, "t", "b") == (True, "sent")


def test_serverchan_unparseable_body_keeps_the_old_benefit_of_the_doubt(monkeypatch):
    """网关插了一页 HTML 不该把一个能用的渠道判死。"""
    import web.backend.notify as notify

    monkeypatch.setattr(notify, "_post", lambda *a, **k: (200, "<html>ok</html>"))
    assert notify._send_serverchan({"send_key": "SCTxxx"}, "t", "b") == (True, "sent")
