"""Pure formatting / scaling helpers shared by the UI (no tkinter dependency).

Kept free of GUI imports so the numeric logic behind charts and
password checks can be unit-tested without a display.
"""

from __future__ import annotations

import math
from typing import Optional


# Full OCI region id -> display name. Used when the same city token maps to
# multiple commercial regions that must stay visually distinct (e.g. Singapore
# vs Singapore West). Checked before the city-token table below.
_REGION_AREA_EXACT = {
    "ap-singapore-1": "新加坡",
    "ap-singapore-2": "新加坡西",
}

# OCI region city token -> region name (Chinese). Keyed on the city so it
# tolerates any realm prefix / index suffix, e.g. "eu-amsterdam-1" and
# "ap-tokyo-1". Prefer _REGION_AREA_EXACT when two regions share a city token.
_REGION_AREA = {
    # Asia Pacific
    "tokyo": "东京", "osaka": "大阪",
    "seoul": "首尔", "chuncheon": "春川",
    "singapore": "新加坡",  # fallback; ap-singapore-1/2 use exact map
    "mumbai": "孟买", "hyderabad": "海得拉巴",
    "sydney": "悉尼", "melbourne": "墨尔本",
    "hongkong": "香港",
    "kualalumpur": "吉隆坡",
    "jakarta": "雅加达",
    "bangkok": "曼谷",
    "manila": "马尼拉",
    # Europe
    "amsterdam": "阿姆斯特丹",
    "frankfurt": "法兰克福",
    "zurich": "苏黎世",
    "paris": "巴黎", "marseille": "马赛",
    "milan": "米兰",
    "madrid": "马德里",
    "stockholm": "斯德哥尔摩",
    "london": "伦敦", "cardiff": "卡迪夫", "newport": "纽波特",
    "jovanovac": "约万诺瓦茨",
    # Americas
    "ashburn": "阿什本", "phoenix": "凤凰城", "sanjose": "圣何塞",
    "chicago": "芝加哥", "sterling": "斯特灵", "tucson": "图森",
    "langley": "兰利", "luke": "卢克",
    "toronto": "多伦多", "montreal": "蒙特利尔",
    "saopaulo": "圣保罗", "vinhedo": "维涅杜",
    "santiago": "圣地亚哥", "valparaiso": "瓦尔帕莱索",
    "bogota": "波哥大",
    "queretaro": "克雷塔罗", "monterrey": "蒙特雷",
    # Middle East / Africa
    "jeddah": "吉达", "riyadh": "利雅得",
    "dubai": "迪拜", "abudhabi": "阿布扎比",
    "jerusalem": "耶路撒冷",
    "johannesburg": "约翰内斯堡",
}


def region_area(region: str) -> str:
    """Map an OCI region id to its region (city) name, e.g.
    'eu-amsterdam-1' -> '阿姆斯特丹', 'ap-singapore-2' -> '新加坡西'.

    Falls back to the region id itself when unknown, which is still more useful
    to the user than a generic placeholder.
    """
    raw = (region or "").strip().lower()
    if not raw:
        return "未知"
    exact = _REGION_AREA_EXACT.get(raw)
    if exact:
        return exact
    # Match the longest city token first so e.g. "sanjose" wins over "jose".
    compact = raw.replace("-", "").replace("_", "")
    for city in sorted(_REGION_AREA, key=len, reverse=True):
        if city in compact:
            return _REGION_AREA[city]
    return region.strip()


# 全仓字节口径：十进制 SI，1 KB = 1000 B，标签写 KB/MB/GB。
#
# 定这个口径的理由：这些数字最后是拿去和 Oracle 控制台 / 账单对照的，Oracle 那边的
# 出网流量、对象存储都按十进制 GB 计；网络速率本身也是十进制惯例（1 Mbps = 10^6 bit）。
# 原来这里除以 1024 却打 SI 标签，等于每上一档偏 2.4%、到 GB 档累计偏 7.4%：
# 一个 Oracle 记作「1 GB」的桶，面板上显示成 953.7 MB，用户会以为哪一边算错了。
# 前端 web/frontend/src/views/InstanceDetailView.vue 的 formatMetricValue 一直是
# 1000 + KB/MB/GB，两处从此一致；要改口径必须两处一起改，否则同一个字节数在
# 实例详情页和这里会差出一整档。
# （另一条路是保留 1024 改标签成 KiB/MiB/GiB，也自洽，但跟控制台对不上号，
#  而且前端的速率单位没法跟着改成 KiB/s——速率没人用二进制。）
_BYTE_STEP = 1000.0
_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(num: float) -> str:
    """Format a byte count with decimal SI units (e.g. 1536 -> '1.5 KB')."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value):
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    idx = 0
    while value >= _BYTE_STEP and idx < len(_BYTE_UNITS) - 1:
        value /= _BYTE_STEP
        idx += 1
    # 单位是按未舍入的值选的，而 .1f 的进位发生在之后：999_950 落在 KB 档，
    # /1000 = 999.95，打印出来就是 '1000.0 KB'（旧的 1024 版同理会打出 '1024.0 KB'）。
    # 循环不会回头再判一次，所以这里按**实际要显示的精度**补判一次进位。
    # 只需判一次：value < step，除一次之后必然落在 [0.99…, 1.0]。
    if idx < len(_BYTE_UNITS) - 1 and round(value, 1) >= _BYTE_STEP:
        value /= _BYTE_STEP
        idx += 1
    if idx == 0 and value == int(value):
        text = str(int(value))
    else:
        # 旧代码在 B 档用 int() 截断，0.5 B 显示成 '0 B'、-0.5 B 显示成 '-0 B'
        # ——一个带负号的零。小数在 B 档保留一位，比截断成 0 诚实。
        text = f"{value:.1f}"
    if float(text) == 0.0:
        # 舍到零之后统一成 '0'：负号没有意义了（别打出 '-0.0 B'），而且
        # 0 和 0.04 都显示成零时，写法也该一致 —— 否则同一列里会同时出现
        # '0 B' 和 '0.0 B' 两种零。
        sign = ""
        text = "0"
    return f"{sign}{text} {_BYTE_UNITS[idx]}"


def axis_max(values: list[float], minimum: float = 1.0) -> float:
    """Return a 'nice' rounded upper bound for a chart axis.

    Rounds up to 1/2/5 x 10^n so gridlines land on readable numbers. Never
    returns less than ``minimum`` so a flat-zero series still yields a usable
    axis instead of collapsing to a single line at the top.

    The result is always > 0: a non-positive (or non-finite) ``minimum`` is read
    as "no floor" rather than as a real bound.
    """
    peak = 0.0
    for v in values:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fv):
            continue
        if fv > peak:
            peak = fv
    try:
        floor = float(minimum)
    except (TypeError, ValueError):
        floor = 0.0
    if not math.isfinite(floor):
        floor = 0.0
    if floor > 0:
        peak = max(peak, floor)
    if peak <= 0:
        # 上界要拿去当除数（scale_points 的 y_max、调用方自己的 v / y_max）。
        # 默认 minimum=1.0 时走不到这里，但调用方传自定义 minimum 就能踩到：
        # 旧代码 axis_max([0,0,0], minimum=0) 原样返回 0.0 -> 调用方 ZeroDivisionError；
        # axis_max([0], minimum=-3.0) 返回 -3.0，一个比所有数据点都低的「上界」，
        # 曲线会整条画到坐标轴外面去。数据没有正的峰值时退回 1.0，画出一根贴底的平线。
        peak = 1.0
    exponent = math.floor(math.log10(peak))
    base = 10.0**exponent
    for mult in (1, 2, 5, 10):
        candidate = mult * base
        if candidate >= peak:
            return float(candidate)
    return float(10 * base)


def scale_points(
    values: list[float],
    width: float,
    height: float,
    y_max: float,
    *,
    pad_left: float = 0.0,
    pad_top: float = 0.0,
    pad_bottom: float = 0.0,
) -> list[tuple[float, float]]:
    """Map a value series to (x, y) pixel coordinates for a canvas line chart.

    X is spread evenly across the plot width by index; Y is inverted so larger
    values sit higher. A single point is centered. Empty input yields an empty
    list. ``y_max`` must be > 0.
    """
    n = len(values)
    if n == 0:
        return []
    y_max = float(y_max) if y_max and y_max > 0 else 1.0
    plot_w = max(1.0, float(width) - float(pad_left))
    plot_h = max(1.0, float(height) - float(pad_top) - float(pad_bottom))
    coords: list[tuple[float, float]] = []
    for i, raw in enumerate(values):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            v = 0.0
        if not math.isfinite(v):
            v = 0.0
        v = 0.0 if v < 0 else (y_max if v > y_max else v)
        x = pad_left + (plot_w / 2.0 if n == 1 else plot_w * i / (n - 1))
        y = pad_top + plot_h * (1.0 - v / y_max)
        coords.append((x, y))
    return coords


def validate_zip_password(password: str, *, confirm: Optional[str] = None, minimum: int = 6) -> Optional[str]:
    """Return a user-facing error string, or None if the password is acceptable."""
    pw = password or ""
    if len(pw.strip()) < minimum:
        return f"备份密码至少需要 {minimum} 位"
    if confirm is not None and pw != confirm:
        return "两次输入的密码不一致"
    return None


# Fixed free-tier shapes whose OCPU / memory are not chosen in the launch form.
_FIXED_SHAPE_RESOURCES: dict[str, tuple[float, float]] = {
    "VM.Standard.E2.1.Micro": (1.0, 1.0),  # 1 OCPU / 1 GB
}


def _fmt_resource_number(value: float | int | None) -> str:
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(num):
        return "—"
    if num == int(num):
        return str(int(num))
    return f"{num:g}"


def format_launch_confirm_rows(
    *,
    display_name: str,
    shape: str,
    ocpus: float | int | None = None,
    memory_in_gbs: float | int | None = None,
    boot_volume_size_in_gbs: int | None = None,
    boot_volume_vpus_per_gb: int | None = None,
    boot_vpu_label: str = "",
    image_label: str = "",
    availability_domain: str = "",
    auth_mode: str = "key",
    assign_public_ip: bool = True,
    assign_ipv6_ip: bool = False,
    as_retry: bool = False,
    retry_interval: int | None = None,
    retry_max: int | None = None,
    free_tier_tag: str = "",
) -> list[tuple[str, str]]:
    """Build label/value rows for the pre-launch configuration confirm dialog.

    Pure helper — no tkinter / OCI dependency. Callers supply display-ready
    labels (image, VPU tier) when available.
    """
    shape_name = (shape or "").strip() or "—"
    shape_display = shape_name
    tag = (free_tier_tag or "").strip()
    if tag:
        shape_display = f"{shape_name}（{tag}）"

    cpu = ocpus
    mem = memory_in_gbs
    if cpu is None or mem is None:
        fixed = _FIXED_SHAPE_RESOURCES.get(shape_name)
        if fixed:
            if cpu is None:
                cpu = fixed[0]
            if mem is None:
                mem = fixed[1]

    boot = boot_volume_size_in_gbs
    if boot is None:
        boot_text = "镜像默认（约 47 GB）"
    else:
        boot_text = f"{int(boot)} GB"

    vpu = boot_volume_vpus_per_gb
    vpu_text = (boot_vpu_label or "").strip()
    if not vpu_text:
        if vpu is None:
            vpu_text = "—"
        else:
            vpu_text = f"{int(vpu)} VPUs/GB"

    auth = (auth_mode or "key").strip().lower()
    auth_text = "root + 服务器密码" if auth == "password" else "root + SSH 公钥"

    net_parts: list[str] = []
    if assign_public_ip:
        net_parts.append("公网 IPv4")
    else:
        net_parts.append("仅私网 IPv4")
    if assign_ipv6_ip:
        net_parts.append("IPv6")
    net_text = " · ".join(net_parts)

    if as_retry and auth != "password":
        interval = retry_interval if retry_interval is not None else "—"
        attempts = retry_max if retry_max is not None else "—"
        retry_text = f"是（间隔 {interval} 秒 · 最多 {attempts} 次）"
    else:
        retry_text = "否"

    rows: list[tuple[str, str]] = [
        ("显示名称", (display_name or "").strip() or "—"),
        ("机器型号", shape_display),
        ("核心", f"{_fmt_resource_number(cpu)} OCPU"),
        ("内存", f"{_fmt_resource_number(mem)} GB"),
        ("硬盘", boot_text),
        ("硬盘性能", vpu_text),
    ]
    if (image_label or "").strip():
        rows.append(("镜像", image_label.strip()))
    if (availability_domain or "").strip():
        rows.append(("可用域", availability_domain.strip()))
    rows.append(("登录方式", auth_text))
    rows.append(("网络", net_text))
    rows.append(("容量重试", retry_text))
    return rows


def format_launch_confirm_message(rows: list[tuple[str, str]]) -> str:
    """Render confirm rows as a plain multi-line message (tests / fallbacks)."""
    if not rows:
        return ""
    width = max(len(label) for label, _ in rows)
    lines = [f"{label.ljust(width)}  {value}" for label, value in rows]
    return "\n".join(lines)
