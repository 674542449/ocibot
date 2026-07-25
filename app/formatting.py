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


def human_bytes(num: float) -> str:
    """Format a byte count with binary units (e.g. 1536 -> '1.5 KB')."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value):
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{sign}{int(value)} {units[idx]}"
    return f"{sign}{value:.1f} {units[idx]}"


def axis_max(values: list[float], minimum: float = 1.0) -> float:
    """Return a 'nice' rounded upper bound for a chart axis.

    Rounds up to 1/2/5 x 10^n so gridlines land on readable numbers. Never
    returns less than ``minimum`` so a flat-zero series still yields a usable
    axis instead of collapsing to a single line at the top.
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
    peak = max(peak, float(minimum))
    if peak <= 0:
        return float(minimum)
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
