"""重新生成 web/frontend/public/ 下所有位图图标。

只在改动品牌标记时才需要跑：

    python scripts/make_favicon.py

会写出（全部来自下面同一套几何，和 logo.svg / favicon.svg 一模一样）：

    favicon.ico            16/32/48/64，浏览器标签页兜底（老 Safari、抓图标的工具只认它）
    apple-touch-icon.png   180×180，iOS「添加到主屏幕」。**不带圆角**：iOS 会自己套
                           圆角蒙版，底图四角透明的话会被垫成黑色
    icon-192.png           Android / PWA 清单图标（manifest.webmanifest 引用）
    icon-512.png           同上，大尺寸（启动画面、应用抽屉）

## 为什么是一个脚本而不是一堆二进制文件躺在仓库里

这些文件是二进制的，改了看不出 diff。把生成它们的几何参数留在这里，
下次要调整时改数字重跑即可，不用去猜当初是怎么画的。

## 为什么自己光栅化

这个环境里没有 Pillow / cairosvg，而为了几个图标去加运行时依赖不值当。
标记本身只有三个形状（底板、圆环、实心圆），用有向距离场（SDF）+ 超采样
直接算覆盖率就够了，输出质量和正经光栅化器没有区别。

PNG 和 ICO 也是手写的：PNG 只需要 IHDR/IDAT/IEND 三个块，ICO 允许直接内嵌
PNG（Vista 以后所有浏览器都认），加起来比引一个库还短。

## 几何必须和 SVG 保持一致

下面这些数字和 web/frontend/public/{logo,favicon}.svg 是同一套。0.4.122 起全站只有
这一个标记：侧边栏、手机顶栏、登录页都直接引用 /logo.svg，favicon.svg 与它逐字相同。
tests/test_brand_mark.py 把这几处钉在一起。

标记「环与核心」：陶土橙圆角底板 + 白色圆环 + 同心实心圆点。环 = 云上的资源池，
核心 = 面板管着的那台机器。
"""

from __future__ import annotations

import math
import pathlib
import struct
import zlib

# --- 32 单位网格上的几何（与两个 SVG 逐字对应） ---------------------------
CX, CY, R = 16.0, 16.0, 11.0      # 圆环中心与半径（线宽中线）
SW = 4.2                          # 圆环线宽
CORE_R = 4.2                      # 中心实心圆半径（与线宽相同，粗细一致）
TILE_R = 7.0                      # 底板圆角
# 字形收到 78%：圆环外径 26.2，不收的话贴着底板的边，在标签栏里显得又挤又糊。
# 16 - 16*0.78 = 3.52，所以 SVG 里平移 3.52 保持居中。
GLYPH_INSET = 0.78

BRAND = (0xC6, 0x61, 0x3F)        # Claude 陶土橙 #c6613f，与 logo.svg / favicon.svg
WHITE = (0xFF, 0xFF, 0xFF)        # 的底板同色；白字形 4.05:1
ICO_SIZES = (16, 32, 48, 64)

_OUT = pathlib.Path(__file__).resolve().parents[1] / "web/frontend/public/favicon.ico"
_PUBLIC = _OUT.parent


def _rrect_sdf(px, py, cx, cy, hw, hh, r):
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - r


def _ring_sdf(px, py):
    """完整圆环（线宽 SW，中线半径 R）。"""
    return abs(math.hypot(px - CX, py - CY) - R) - SW / 2.0


def _core_sdf(px, py):
    """中心实心圆。"""
    return math.hypot(px - CX, py - CY) - CORE_R


def render(size: int, ss: int = 4, *, rounded: bool = True) -> list[list[tuple[int, int, int, int]]]:
    """底板 + 字形，返回 RGBA 像素行。ss 是每轴超采样倍数。

    rounded=False 画满整个方块（给 iOS：它自己套圆角蒙版）。
    """
    rows = []
    scale = 32.0 / size
    tile_r = TILE_R if rounded else 0.0
    for y in range(size):
        row = []
        for x in range(size):
            tile_a = glyph_a = 0
            for sy in range(ss):
                for sx in range(ss):
                    ux = (x + (sx + 0.5) / ss) * scale
                    uy = (y + (sy + 0.5) / ss) * scale
                    if _rrect_sdf(ux, uy, 16, 16, 16, 16, tile_r) <= 0:
                        tile_a += 1
                    gx = 16 + (ux - 16) / GLYPH_INSET
                    gy = 16 + (uy - 16) / GLYPH_INSET
                    if min(_ring_sdf(gx, gy), _core_sdf(gx, gy)) <= 0:
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


def _write(name: str, data: bytes) -> None:
    path = _PUBLIC / name
    path.write_bytes(data)
    print(f"wrote {path} ({len(data)} bytes)")


def main() -> None:
    _write("favicon.ico", to_ico([(s, to_png(render(s))) for s in ICO_SIZES]))
    # 大尺寸用 2× 超采样就够平滑，4× 在纯 Python 里要跑很久。
    _write("apple-touch-icon.png", to_png(render(180, 2, rounded=False)))
    _write("icon-192.png", to_png(render(192, 2)))
    _write("icon-512.png", to_png(render(512, 2)))


if __name__ == "__main__":
    main()
