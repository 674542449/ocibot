"""字节口径与坐标轴上界的回归测试。

这一批断言全部对应实测复现过的显示缺陷，每条都注明了修复前打出来的是什么，
免得后人看到「奇怪的边界用例」以为可以顺手删掉。
"""

from __future__ import annotations

import math

import pytest

from app.formatting import axis_max, human_bytes, scale_points


# --------------------------------------------------------------------------
# 口径：十进制 SI，1 KB = 1000 B
# --------------------------------------------------------------------------


def test_human_bytes_is_decimal_si():
    # 修复前：除数 1024 却打 SI 标签，human_bytes(10**9) == '953.7 MB'。
    # Oracle 控制台把同一个桶记作 1 GB，两边差 7.4%，用户没法判断谁算错了。
    assert human_bytes(10**3) == "1.0 KB"
    assert human_bytes(10**6) == "1.0 MB"
    assert human_bytes(10**9) == "1.0 GB"
    assert human_bytes(10**12) == "1.0 TB"
    assert human_bytes(10**15) == "1.0 PB"


def test_human_bytes_matches_frontend_convention():
    """前端 InstanceDetailView.vue::formatMetricValue 用的是同一套除数、标签和进位规则。

    两处若不一致，同一个字节数在实例详情页和这里会差出一整档（历史上就是 1024 vs 1000
    差 7.4%）。右列是把前端那个函数原样跑在 node 上得到的输出，不是推算的；改动任一侧
    都应该回来更新这张表，跑一下另一侧确认还对得上。前端多一到两位小数，只比单位。
    """
    frontend = {
        0: "0 B/s",
        512: "512 B/s",
        999: "999 B/s",
        1000: "1.0 KB/s",
        1536: "1.5 KB/s",
        999_999: "1.00 MB/s",  # 两边都在进位线上升到 MB
        10**6: "1.00 MB/s",
        999_995_000: "1.00 GB/s",
        10**9: "1.00 GB/s",
        5 * 10**9: "5.00 GB/s",
    }
    for value, rate_text in frontend.items():
        unit = human_bytes(value).split()[-1]
        expected = rate_text.split()[-1].removesuffix("/s")
        assert unit == expected, f"{value} -> {human_bytes(value)}，前端是 {rate_text}"


# --------------------------------------------------------------------------
# 进位线：先选单位、后四舍五入 -> '1000.0 KB'
# --------------------------------------------------------------------------


def test_human_bytes_promotes_on_rounding_boundary():
    # 修复前（1024 版）：human_bytes(1048575) == '1024.0 KB'，
    # human_bytes(1024**3 - 1) == '1024.0 MB'——.1f 的进位发生在单位选定之后，
    # 循环不会回头再判一次，于是打出一个本档根本不该出现的数。
    # 换成十进制后同一类边界在这几个值上：
    assert human_bytes(999_950) == "1.0 MB"  # 999.95 KB 四舍五入到 1000.0
    assert human_bytes(999_950_000) == "1.0 GB"
    assert human_bytes(999.96) == "1.0 KB"
    assert human_bytes(999_999_999) == "1.0 GB"


def test_human_bytes_does_not_over_promote():
    # 只在真的跨过进位线时才升档；差一点点的值必须留在本档，
    # 否则修完进位又会把 999.9 MB 错报成 1.0 GB。
    assert human_bytes(999_940) == "999.9 KB"
    assert human_bytes(999_949_999) == "999.9 MB"
    assert human_bytes(999) == "999 B"


def test_human_bytes_top_unit_does_not_promote_past_pb():
    # PB 是最后一档，超出范围只能继续加大数字，不能越界索引单位表。
    assert human_bytes(1.2345e18) == "1234.5 PB"


# --------------------------------------------------------------------------
# B 档的小数与负零
# --------------------------------------------------------------------------


def test_human_bytes_sub_byte_values_are_not_truncated():
    # 修复前：B 档用 int() 截断，human_bytes(0.5) == '0 B'，
    # 而 human_bytes(-0.5) == '-0 B'——带负号的零。
    assert human_bytes(0.5) == "0.5 B"
    assert human_bytes(-0.5) == "-0.5 B"
    assert human_bytes(999.4) == "999.4 B"


def test_human_bytes_never_renders_negative_zero():
    # 舍入之后数字是 0 的话：负号没有意义（不能是 '-0.0 B'），而且写法要统一成
    # 一种零。以前 human_bytes(0) 走 int 分支给 '0 B'、human_bytes(0.04) 走 .1f
    # 分支给 '0.0 B'，同一列里会同时出现两种零。
    assert human_bytes(-0.04) == "0 B"
    assert human_bytes(0.04) == "0 B"
    assert human_bytes(-0.0) == "0 B"
    assert human_bytes(0) == "0 B"
    for v in (-0.0, -0.04, -0.004, 0.0):
        assert not human_bytes(v).startswith("-"), human_bytes(v)


def test_human_bytes_integer_bytes_stay_integers():
    # 整数字节数不要平白多出一位小数（'512 B'，不是 '512.0 B'）。
    assert human_bytes(0) == "0 B"
    assert human_bytes(1) == "1 B"
    assert human_bytes(512) == "512 B"


def test_human_bytes_bad_input_still_returns_placeholder():
    assert human_bytes(None) == "—"
    assert human_bytes("abc") == "—"
    assert human_bytes(float("nan")) == "—"
    assert human_bytes(float("-inf")) == "—"


# --------------------------------------------------------------------------
# axis_max：上界永远是正数
# --------------------------------------------------------------------------


def test_axis_max_is_always_positive_with_custom_minimum():
    # 修复前：peak <= 0 时原样返回 minimum。
    #   axis_max([0, 0, 0], minimum=0) -> 0.0   调用方拿去做除数就是 ZeroDivisionError
    #   axis_max([], minimum=0)        -> 0.0
    #   axis_max([0], minimum=-3.0)    -> -3.0  一个比所有数据点都低的「上界」
    # 默认 minimum=1.0 走不到这个分支，所以只有传自定义 minimum 的调用方会踩到。
    assert axis_max([0, 0, 0], minimum=0) == 1.0
    assert axis_max([], minimum=0) == 1.0
    assert axis_max([0], minimum=-3.0) == 1.0
    assert axis_max([], minimum=float("nan")) == 1.0
    assert axis_max([0.0], minimum=None) == 1.0  # type: ignore[arg-type]


def test_axis_max_result_is_safe_as_a_divisor():
    # 这就是上面那条要防的故障形态：旧代码在这里直接 ZeroDivisionError。
    for values, minimum in (([0, 0, 0], 0), ([], 0), ([0], -3.0), ([], -1)):
        top = axis_max(values, minimum=minimum)
        assert top > 0
        # 分子要用数据里的**实际峰值**。写死 0.0 的话，在 `top > 0` 已经成立的前提下
        # 0.0/top 恒等于 0.0，这一行就是一句恒真断言，什么也检测不了。
        peak = max(values) if values else 0.0
        assert 0.0 <= (peak / top) <= 1.0, (values, minimum, top)
        # scale_points 自己也会兜底，但正常口径下不该再触发那层兜底
        coords = scale_points(values or [0.0], width=100, height=100, y_max=top)
        assert all(math.isfinite(x) and math.isfinite(y) for x, y in coords)


def test_axis_max_non_positive_minimum_means_no_floor():
    # minimum <= 0 读作「不设下限」而不是一个真的下限：数据自己有正峰值时，
    # 仍然按数据缩放（不要一律抬到 1.0，那会把小量程的曲线压平）。
    assert axis_max([0.4], minimum=0) == 0.5
    assert axis_max([0.4], minimum=-3.0) == 0.5
    assert axis_max([3, 40, 7], minimum=0) == 50.0


def test_axis_max_positive_minimum_still_wins():
    # 正的 minimum 仍然是硬下限，原有行为不能被上面的兜底改掉。
    assert axis_max([], minimum=100) == 100.0
    assert axis_max([3], minimum=100) == 100.0
    assert axis_max([3, 40, 7]) == 50.0
    assert axis_max([0.4, 0.9]) == 1.0


@pytest.mark.parametrize("values", [[], [0], [0, 0, 0], [-5, -1], [float("nan")]])
def test_axis_max_never_returns_zero_or_negative(values):
    for minimum in (1.0, 0.0, -1.0, -1000.0):
        assert axis_max(values, minimum=minimum) > 0
