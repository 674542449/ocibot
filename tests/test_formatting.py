"""Tests for pure UI formatting / scaling helpers (no tkinter required)."""

from __future__ import annotations

import math

from app.formatting import (
    axis_max,
    format_launch_confirm_message,
    format_launch_confirm_rows,
    human_bytes,
    scale_points,
    validate_zip_password,
)


def test_human_bytes_units():
    # 口径已统一为十进制 SI（1 KB = 1000 B），见 app/formatting.py 顶部的说明。
    # 旧断言写的是二进制分档配 SI 标签：human_bytes(1023) == "1023 B" 和
    # human_bytes(5 * 1024**3) == "5.0 GB" 恰恰是被修掉的那个 bug 本身——后者
    # 5_368_709_120 字节在 Oracle 控制台上就是 5.37 GB，不是 5.0 GB。断言按新口径重写，
    # 边界行为另见 tests/test_formatting_units.py。
    assert human_bytes(0) == "0 B"
    assert human_bytes(999) == "999 B"
    assert human_bytes(1000) == "1.0 KB"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(10**6) == "1.0 MB"
    assert human_bytes(5 * 10**9) == "5.0 GB"
    assert human_bytes(5 * 1024**3) == "5.4 GB"
    assert human_bytes(-2048) == "-2.0 KB"
    assert human_bytes(None) == "—"


def test_axis_max_rounds_up_nicely():
    assert axis_max([3, 40, 7]) == 50.0
    assert axis_max([0.4, 0.9]) == 1.0  # never below the minimum
    assert axis_max([], minimum=100) == 100.0
    assert axis_max([120]) == 200.0
    # A flat zero series still yields a usable positive axis.
    assert axis_max([0, 0, 0]) >= 1.0


def test_axis_max_ignores_bad_values():
    assert axis_max([None, "x", 12]) == 20.0


def test_non_finite_values_do_not_crash():
    # OCI monitoring can emit NaN/inf for gap datapoints; helpers must be robust.
    assert human_bytes(float("nan")) == "—"
    assert human_bytes(float("inf")) == "—"
    assert axis_max([float("nan"), 5]) == 5.0
    assert axis_max([float("inf")]) >= 1.0  # inf skipped, falls back to minimum
    coords = scale_points([float("nan"), 100], width=100, height=100, y_max=100)
    assert coords[0][1] == 100.0  # NaN treated as 0 -> bottom
    assert coords[1][1] == 0.0


def test_scale_points_inverts_y_and_spreads_x():
    coords = scale_points([0, 100], width=100, height=100, y_max=100)
    assert len(coords) == 2
    (x0, y0), (x1, y1) = coords
    assert x0 == 0.0 and x1 == 100.0
    # value 0 -> bottom (large y), value 100 -> top (y == 0)
    assert y0 == 100.0
    assert y1 == 0.0


def test_scale_points_single_point_centered():
    coords = scale_points([50], width=80, height=40, y_max=100)
    assert len(coords) == 1
    x, y = coords[0]
    assert x == 40.0
    assert math.isclose(y, 20.0)


def test_scale_points_clamps_and_handles_empty():
    assert scale_points([], 100, 100, 100) == []
    # values above y_max are clamped to the top, negatives to the bottom
    coords = scale_points([-5, 200], 100, 100, 100)
    assert coords[0][1] == 100.0
    assert coords[1][1] == 0.0


def test_validate_zip_password():
    assert validate_zip_password("abcdef") is None
    assert validate_zip_password("123") is not None
    assert validate_zip_password("abcdef", confirm="abcdef") is None
    assert validate_zip_password("abcdef", confirm="different") is not None
    assert validate_zip_password("x", minimum=1) is None


def test_format_launch_confirm_rows_arm_flex():
    rows = format_launch_confirm_rows(
        display_name="ocibot-demo",
        shape="VM.Standard.A1.Flex",
        ocpus=4,
        memory_in_gbs=24,
        boot_volume_size_in_gbs=100,
        boot_volume_vpus_per_gb=120,
        boot_vpu_label="超高性能 (120 VPUs/GB) — 可能额外计费",
        image_label="Ubuntu 22.04 aarch64",
        availability_domain="AD-1",
        auth_mode="key",
        assign_public_ip=True,
        assign_ipv6_ip=True,
        as_retry=True,
        retry_interval=60,
        retry_max=100,
        free_tier_tag="免费 ARM",
    )
    by_label = dict(rows)
    assert by_label["显示名称"] == "ocibot-demo"
    assert "VM.Standard.A1.Flex" in by_label["机器型号"]
    assert "免费 ARM" in by_label["机器型号"]
    assert by_label["核心"] == "4 OCPU"
    assert by_label["内存"] == "24 GB"
    assert by_label["硬盘"] == "100 GB"
    assert "120" in by_label["硬盘性能"]
    assert by_label["镜像"] == "Ubuntu 22.04 aarch64"
    assert by_label["登录方式"] == "root + SSH 公钥"
    assert "IPv6" in by_label["网络"]
    assert "是" in by_label["容量重试"]
    assert "60" in by_label["容量重试"]


def test_format_launch_confirm_rows_fixed_micro_defaults():
    rows = format_launch_confirm_rows(
        display_name="micro",
        shape="VM.Standard.E2.1.Micro",
        ocpus=None,
        memory_in_gbs=None,
        boot_volume_size_in_gbs=None,
        boot_volume_vpus_per_gb=10,
        boot_vpu_label="平衡 (10 VPUs/GB)",
        auth_mode="password",
        assign_public_ip=False,
        as_retry=True,  # password mode should still report 否 via auth path in UI; here we check password auth text
        free_tier_tag="免费 AMD",
    )
    by_label = dict(rows)
    assert by_label["核心"] == "1 OCPU"
    assert by_label["内存"] == "1 GB"
    assert "镜像默认" in by_label["硬盘"]
    assert by_label["登录方式"] == "root + 服务器密码"
    assert "仅私网" in by_label["网络"]
    # as_retry ignored for password auth display
    assert by_label["容量重试"] == "否"


def test_format_launch_confirm_message_aligns_labels():
    text = format_launch_confirm_message(
        [
            ("机器型号", "A1"),
            ("核心", "4 OCPU"),
        ]
    )
    assert "机器型号" in text
    assert "4 OCPU" in text
    assert "\n" in text


