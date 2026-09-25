"""Pure formatting / scaling helpers shared by the UI (no tkinter dependency).

Kept free of GUI imports so the numeric logic behind charts and
password checks can be unit-tested without a display.
"""

from __future__ import annotations

import math
from typing import Optional


# OCI 区域标识符 -> Oracle 官方中文区域名称。**只收有出处的，一个字都不自己译。**
#
# 名称逐字取自 Oracle 中文官网的区域列表：
#   https://www.oracle.com/cn/cloud/public-cloud-regions/
# 那一页只有名称、没有标识符，标识符靠英文名一一对上：中文名是英文官方名的直译
# （「日本东部（东京）」= Japan East (Tokyo)），而英文名到标识符的对应取自
#   https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm
# 两个欧盟主权区域的中文名和标识符同时出现在 Oracle 中文文档里：
#   https://docs.oracle.com/cloud/help/zh_CN/epm-common/TSEPM/opc_gen_2_regions.htm
#
# 两条是操作者确认过的对应（名称仍逐字取自中文官网，只是标识符表的英文写法不同）：
#   ap-kulai-2  = 「马来西亚西部（古来）」   标识符表写 "Malaysia West 2 (Kulai)"
#   eu-madrid-3 = 「西班牙中部 2（马德里）」 标识符表写 "Spain Central (Madrid 3)"
# 刻意**不收**的：肯尼亚、摩洛哥 2 —— 官网有名字，但文档里还没有标识符，显示标识符本身。
# 以前这里是按城市名子串模糊匹配的自译表（「新加坡西」「卡迪夫」这类都是猜的），
# 新区域上线时也会被猜成一个看着像、其实不对的名字。
_REGION_NAME_ZH = {
    # 北美地区
    "us-ashburn-1": "美国东部（阿什本）",
    "us-chicago-1": "美国中西部（芝加哥）",
    "us-phoenix-1": "美国西部（凤凰城）",
    "us-sanjose-1": "美国西部（圣何塞）",
    "ca-montreal-1": "加拿大东南部（蒙特利尔）",
    "ca-toronto-1": "加拿大东南部（多伦多）",
    "mx-queretaro-1": "墨西哥中部（克雷塔罗）",
    "mx-monterrey-1": "墨西哥东北部（蒙特雷）",
    # 南美地区
    "sa-saopaulo-1": "巴西东部（圣保罗）",
    "sa-vinhedo-1": "巴西东南部（维涅杜）",
    "sa-santiago-1": "智利中部（圣地亚哥）",
    "sa-valparaiso-1": "智利西部（瓦尔帕莱索）",
    "sa-bogota-1": "哥伦比亚中部（波哥大）",
    # 欧洲
    "eu-paris-1": "法国中部（巴黎）",
    "eu-marseille-1": "法国南部（马赛）",
    "eu-frankfurt-1": "德国中部（法兰克福）",
    "eu-milan-1": "意大利西北部（米兰）",
    "eu-turin-1": "意大利北部（都灵）",
    "eu-amsterdam-1": "荷兰西北部（阿姆斯特丹）",
    "eu-jovanovac-1": "塞尔维亚中部（乔万诺瓦茨）",
    "eu-madrid-1": "西班牙中部（马德里）",
    "eu-madrid-3": "西班牙中部 2（马德里）",
    "eu-stockholm-1": "瑞典中部（斯德哥尔摩）",
    "eu-zurich-1": "瑞士北部（苏黎世）",
    "uk-london-1": "英国南部（伦敦）",
    "uk-cardiff-1": "英国西部（纽波特）",
    # 中东和非洲地区
    "il-jerusalem-1": "以色列中部（耶路撒冷）",
    "af-casablanca-1": "摩洛哥西部（卡萨布兰卡）",
    "me-jeddah-1": "沙特阿拉伯西部（吉达）",
    "me-riyadh-1": "沙特阿拉伯中部（利雅得）",
    "af-johannesburg-1": "南非中部（约翰内斯堡）",
    "me-dubai-1": "阿联酋东部（迪拜）",
    "me-abudhabi-1": "阿联酋中部（阿布扎比）",
    # 亚太地区
    "ap-sydney-1": "澳大利亚东部（悉尼）",
    "ap-melbourne-1": "澳大利亚东南部（墨尔本）",
    "ap-mumbai-1": "印度西部（孟买）",
    "ap-hyderabad-1": "印度南部（海得拉巴）",
    "ap-batam-1": "印度尼西亚北部（巴淡）",
    "ap-tokyo-1": "日本东部（东京）",
    "ap-osaka-1": "日本中部（大阪）",
    "ap-kulai-2": "马来西亚西部（古来）",
    "ap-singapore-1": "新加坡（新加坡）",
    "ap-singapore-2": "新加坡西部（新加坡）",
    "ap-seoul-1": "韩国中部（首尔）",
    "ap-chuncheon-1": "韩国北部（春川）",
    # 主权区域
    "eu-frankfurt-2": "欧盟主权区域中部（法兰克福）",
    "eu-madrid-2": "欧盟主权区域南部（马德里）",
}


def region_area(region: str) -> str:
    """Oracle's official Chinese name for an OCI region id, e.g.
    'ap-tokyo-1' -> '日本东部（东京）', 'ap-singapore-2' -> '新加坡西部（新加坡）'.

    Exact lookup only (see _REGION_NAME_ZH for the sources). An id without a
    sourced name falls back to the id itself — still more useful than a generic
    placeholder, and never a plausible-looking wrong name.
    """
    raw = (region or "").strip().lower()
    if not raw:
        return "未知"
    return _REGION_NAME_ZH.get(raw) or region.strip()


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

    if as_retry:
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
