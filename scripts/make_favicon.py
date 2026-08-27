"""重新生成 web/frontend/public/favicon.ico。

只在改动品牌标记时才需要跑：

    python scripts/make_favicon.py

## 为什么是一个脚本而不是一个二进制文件躺在仓库里

favicon.ico 是二进制的，改了看不出 diff。把生成它的几何参数留在这里，
下次要调整时改数字重跑即可，不用去猜当初是怎么画的。

## 为什么自己光栅化

这个环境里没有 Pillow / cairosvg，而为了一个图标去加运行时依赖不值当。
标记本身只有三个形状（圆角底板、带缺口的圆环、圆角方块），用有向距离场
（SDF）+ 超采样直接算覆盖率就够了，输出质量和正经光栅化器没有区别。

PNG 和 ICO 也是手写的：PNG 只需要 IHDR/IDAT/IEND 三个块，ICO 允许直接内嵌
PNG（Vista 以后所有浏览器都认），加起来比引一个库还短。

## 几何必须和 SVG 保持一致

下面这些数字和 web/frontend/public/{favicon,logo}.svg 里的路径是同一套。
改了这里就要同步改那两个文件，否则标签页图标会和界面里的标记对不上。
侧边栏那份还内联在 web/frontend/src/layouts/AppLayout.vue 里（它用
currentColor 跟随主题），一共三处。

标记的含义：环 = 一直在转的容量循环，缺口 = 放出来的那个空位，
方块 = 抢到并落位的实例。
"""

from __future__ import annotations

import math
import pathlib
import struct
import zlib

# --- 32 单位网格上的几何（与两个 SVG 逐字对应） ---------------------------
CX, CY, R = 16.0, 16.0, 9.6
SW = 4.6                          # 圆环线宽
GAP_CENTER, GAP_HALF = -45.0, 40.0   # 缺口中心角与半角（度，y 向下）
NODE_ANG, NODE_SIZE, NODE_R = -45.0, 8.0, 2.2
TILE_R = 7.0                      # 底板圆角
# 带底板时字形收到 88%：贴着圆角边缘的图标在标签栏里显得又挤又糊。
GLYPH_INSET = 0.88

BRAND = (0x5B, 0x51, 0xD8)        # 亮色 #4a41c0 与暗色 #8a97ff 之间取值，
WHITE = (0xFF, 0xFF, 0xFF)        # 深浅两种标签栏上都够对比度（白字 5.7:1）
SIZES = (16, 32, 48, 64)

_OUT = pathlib.Path(__file__).resolve().parents[1] / "web/frontend/public/favicon.ico"


def _rrect_sdf(px, py, cx, cy, hw, hh, r):
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - r


def _ring_sdf(px, py):
    """圆环，挖掉一个角度楔形（缺口）。"""
    dx, dy = px - CX, py - CY
    d = abs(math.hypot(dx, dy) - R) - SW / 2.0
    delta = (math.degrees(math.atan2(dy, dx)) - GAP_CENTER + 180.0) % 360.0 - 180.0
    if abs(delta) < GAP_HALF:
        # 缺口内：把距离推出去，同时让弧的两端是平切的而不是渗进缺口。
        return max(d, (GAP_HALF - abs(delta)) * math.pi / 180.0 * R)
    return d


def _node_sdf(px, py):
    t = math.radians(NODE_ANG)
    return _rrect_sdf(
        px, py, CX + R * math.cos(t), CY + R * math.sin(t),
        NODE_SIZE / 2, NODE_SIZE / 2, NODE_R,
    )


def render(size: int, ss: int = 4) -> list[list[tuple[int, int, int, int]]]:
    """底板 + 挖空字形，返回 RGBA 像素行。ss 是每轴超采样倍数。"""
    rows = []
    scale = 32.0 / size
    for y in range(size):
        row = []
        for x in range(size):
            tile_a = glyph_a = 0
            for sy in range(ss):
                for sx in range(ss):
                    ux = (x + (sx + 0.5) / ss) * scale
                    uy = (y + (sy + 0.5) / ss) * scale
                    if _rrect_sdf(ux, uy, 16, 16, 16, 16, TILE_R) <= 0:
                        tile_a += 1
                    gx = 16 + (ux - 16) / GLYPH_INSET
                    gy = 16 + (uy - 16) / GLYPH_INSET
                    if min(_ring_sdf(gx, gy), _node_sdf(gx, gy)) <= 0:
                        glyph_a += 1
            n = ss * ss
            a, mix = tile_a / n, glyph_a / n
            if a <= 0:
                row.append((0, 0, 0, 0))
                continue
            row.append(
                tuple(int(BRAND[i] * (1 - mix) + WHITE[i] * mix) for i in range(3))
                + (int(a * 255),)
            )
        rows.append(row)
    return rows


def to_png(rows) -> bytes:
    h, w = len(rows), len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # 过滤器：无
        for px in row:
            raw += bytes(px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def to_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = blobs = b""
    for size, data in pngs:
        dim = 0 if size >= 256 else size          # ICO 里 256 记作 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main() -> None:
    pngs = [(s, to_png(render(s))) for s in SIZES]
    _OUT.write_bytes(to_ico(pngs))
    print(f"wrote {_OUT} ({_OUT.stat().st_size} bytes, sizes {list(SIZES)})")


if __name__ == "__main__":
    main()
