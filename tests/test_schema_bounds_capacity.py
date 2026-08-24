"""每个会落进 varchar 列的请求字段，都必须在校验层就被卡住。

断言一律打在**校验层**（pydantic 的 max_length / 校验器）和 models.py 的列宽上，
不打在「插进去没报错」上：SQLite 根本不检查 varchar 宽度，所以后一种测试在这台
机器上永远是绿的，哪怕线上的 PostgreSQL 正因为它每次都 500。

被钉住的主 bug：``POST /api/tenants/{id}/launch`` 带 ``as_retry=true`` 时，
``display_name`` 会变成 ``CapacityJob.name`` —— ``f"容量重试 · {display_name}"``
写进 ``String(128)``。122 个字符就溢出了，而 OCI 自己允许 255，所以这是完全合法
的输入。代价不是「500 而不是 422」：

    额度校验 → prepare_launch_network（**真的在 Oracle 建出一个 managed NSG**，
    它把自己的名字截到 100 所以不会失败）→ 写 CapacityJob 行 → db.commit() 💥

那个 commit 在 routers/instances.py 里没有包 try，孤儿 NSG 的清理只挂在 409 那一
个分支上。于是调用方拿到一个空的 500，Oracle 那边留下一个永远不会被删掉的 NSG，
而且用户每重试一次就再多一个。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from pydantic import ValidationError  # noqa: E402

from web.backend.models import CapacityAttempt, CapacityJob  # noqa: E402
from web.backend.schemas import (  # noqa: E402
    CapacityJobCreate,
    LaunchInstanceRequest,
)

# routers/instances.py 组装抢机任务名用的前缀。写死在这里而不是 import，是因为
# 这个测试要证明的恰恰是「schemas 的上限」和「那边拼出来的字符串」对得上。
_JOB_NAME_PREFIX = "容量重试 · "

_PAYLOAD = {
    "display_name": "i",
    "compartment_id": "ocid1.compartment.oc1..c",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.oc1..i",
    "subnet_id": "ocid1.subnet.oc1..s",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfakekeymaterial",
    "ocpus": 1,
    "memory_in_gbs": 6,
    "boot_volume_size_in_gbs": 50,
}


def _col_width(model, column: str) -> int:
    width = model.__table__.c[column].type.length
    assert width, f"{model.__name__}.{column} 不是定宽列了，这个测试要跟着改"
    return int(width)


def _launch(**over):
    body = {"shape": "VM.Standard.A1.Flex", "image_id": "ocid1.image.oc1..i"}
    body.update(over)
    return LaunchInstanceRequest(**body)


def _job_create(**over):
    body = {"tenant_id": "t" * 36, "launch_payload": dict(_PAYLOAD)}
    body.update(over)
    return CapacityJobCreate(**body)


# ---- LaunchInstanceRequest ----


def test_display_name_cap_leaves_room_for_the_capacity_job_name_prefix():
    """最长可接受的 display_name 拼上前缀之后必须还能装进 CapacityJob.name。"""
    width = _col_width(CapacityJob, "name")
    headroom = width - len(_JOB_NAME_PREFIX)

    accepted = _launch(display_name="x" * headroom)
    assert len(_JOB_NAME_PREFIX + accepted.display_name) <= width, (
        "校验层放行的 display_name 拼上前缀之后仍然会溢出 CapacityJob.name —— "
        "在 PostgreSQL 上这是一个空 500 加一个孤儿 NSG"
    )

    with pytest.raises(ValidationError):
        _launch(display_name="x" * (headroom + 1))


def test_display_name_of_a_realistic_length_is_still_accepted():
    """别矫枉过正：正常长度的机器名必须照收。"""
    assert _launch(display_name="my-prod-web-01").display_name == "my-prod-web-01"


@pytest.mark.parametrize(
    "field",
    [
        "availability_domain",
        "shape",
        "image_id",
        "subnet_id",
        "compartment_id",
        "auth_mode",
        "ssh_public_key",
        "root_password",
        "user_data",
    ],
)
def test_launch_string_fields_are_all_bounded(field):
    """这些值都会进 launch_payload / cloud-init，没有一个可以是无上限的。"""
    with pytest.raises(ValidationError):
        _launch(**{field: "x" * 100_000})


def test_absurd_retry_numbers_are_rejected_before_the_clamp_sees_them():
    """clamp_retry_interval 用 int(float(x))，只 catch (TypeError, ValueError)。

    JSON 的整数是任意精度的，``float(10**400)`` 抛的是 OverflowError —— 它会穿过
    clamp 变成一个裸 500。
    """
    with pytest.raises(ValidationError):
        _launch(retry_interval_sec=10**400)
    with pytest.raises(ValidationError):
        _launch(retry_max_attempts=10**400)


# ---- CapacityJobCreate ----


def test_capacity_job_name_is_bounded_to_its_column():
    width = _col_width(CapacityJob, "name")
    assert len(_job_create(name="x" * width).name) == width
    with pytest.raises(ValidationError):
        _job_create(name="x" * (width + 1))


def test_availability_domains_are_bounded_in_count():
    """2000 个 AD 是一条任务行就能写进去的 JSON，worker 每次尝试都要整条读出来。"""
    with pytest.raises(ValidationError):
        _job_create(availability_domains=["AD-1"] * 2000)
    # 一个 OCI 区域最多 3 个 AD，正常输入必须还能过。
    assert len(_job_create(availability_domains=["AD-1", "AD-2", "AD-3"]).availability_domains) == 3


def test_availability_domain_items_fit_the_attempt_log_column():
    """元素超宽比行数超标更糟：worker 把它原样写进 CapacityAttempt。

    在 PostgreSQL 上 ``_log_attempt`` 的 flush 会抛 DataError，整个事务变
    aborted，``attempts += 1`` 跟着回滚 —— max_attempts 那道合规上限从此永远够不
    到，租约一过期任务就重新认领、再发一次 LaunchInstance。
    """
    width = _col_width(CapacityAttempt, "availability_domain")
    assert len(_job_create(availability_domains=["A" * width]).availability_domains[0]) == width
    with pytest.raises(ValidationError):
        _job_create(availability_domains=["A" * (width + 1)])


@pytest.mark.parametrize(
    "key",
    [
        "display_name",
        "compartment_id",
        "availability_domain",
        "shape",
        "image_id",
        "subnet_id",
        "vcn_id",
        "network_compartment_id",
        "managed_nsg_id",
        "ssh_public_key",
        "auth_mode",
        "launch_token",
    ],
)
def test_launch_payload_string_values_are_bounded(key):
    """sanitize_launch_payload 只白名单字段**名**，没给任何一个**值**设上限。"""
    payload = dict(_PAYLOAD)
    payload[key] = "x" * 100_000
    with pytest.raises(ValidationError):
        _job_create(launch_payload=payload)


def test_launch_payload_nsg_ids_are_bounded_in_count_and_width():
    payload = dict(_PAYLOAD)
    payload["nsg_ids"] = ["ocid1.networksecuritygroup.oc1..n"] * 5000
    with pytest.raises(ValidationError):
        _job_create(launch_payload=payload)

    payload = dict(_PAYLOAD)
    payload["nsg_ids"] = ["x" * 100_000]
    with pytest.raises(ValidationError):
        _job_create(launch_payload=payload)

    payload = dict(_PAYLOAD)
    payload["nsg_ids"] = ["ocid1.networksecuritygroup.oc1..n"]
    assert _job_create(launch_payload=payload).launch_payload["nsg_ids"]


def test_launch_payload_key_count_is_bounded():
    payload = dict(_PAYLOAD)
    payload.update({f"junk{i}": "x" for i in range(200)})
    with pytest.raises(ValidationError):
        _job_create(launch_payload=payload)


def test_absurd_capacity_job_numbers_are_rejected():
    with pytest.raises(ValidationError):
        _job_create(interval_sec=10**400)
    with pytest.raises(ValidationError):
        _job_create(max_attempts=10**400)


def test_a_normal_capacity_job_body_still_validates():
    """回归护栏：以上所有上限都不能挡住正常请求。"""
    body = _job_create(
        name="容量重试 · web-01",
        availability_domains=["kIdk:AP-TOKYO-1-AD-1"],
        interval_sec=180,
        max_attempts=200,
    )
    assert body.launch_payload["shape"] == "VM.Standard.A1.Flex"
