"""纯 numpy 的小光栅器：给 Tk 画抗锯齿的圆角、图标、光点、键帽。

为什么自己画：
    tk.Canvas 在 Windows 上不做抗锯齿，`create_polygon(smooth=True)` 画出来的圆角
    是锯齿状的——在 150% 缩放的屏幕上一眼就能看出"这是 Tk"。而带 alpha 的 PNG
    PhotoImage 是 Tk 8.6 原生支持的，Canvas 会把它和底色混合。所以这里：
    用有符号距离场（SDF）在 numpy 里算出每个像素的覆盖率 → 编成 PNG → 喂给 PhotoImage。

为什么不用 Pillow：
    numpy 本来就是硬依赖（音频处理），再加 Pillow 会让每个 exe 多十几 MB，
    而我们要画的只有圆角矩形、线段、圆、多边形——一百来行距离场就够了。

约定：
    * 所有尺寸都是**物理像素**（调用方负责先过 theme.S()）。
    * 图标在 24×24 的设计网格里定义，按 size/24 缩放；线宽 1.8、圆头圆角。
    * 缓存挂在 Tk 根窗口上而不是模块全局：PhotoImage 属于创建它的 Tcl 解释器，
      测试里反复建销根窗口时，模块级缓存会把已经死掉的图像交给新窗口。
"""

from __future__ import annotations

import base64
import math
import struct
import tkinter as tk
import zlib
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #


def to_rgb(color: str) -> tuple[int, int, int]:
    """'#rrggbb' → (r, g, b)。"""
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: Sequence[float]) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(a: str, b: str, t: float) -> str:
    """a 到 b 之间 t 处的颜色（t=0 是 a，t=1 是 b）。"""
    ra, rb = to_rgb(a), to_rgb(b)
    return to_hex([x + (y - x) * t for x, y in zip(ra, rb, strict=True)])


def _lin(v: float) -> float:
    v /= 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    """WCAG 相对亮度。"""
    r, g, b = to_rgb(color)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    """WCAG 对比度（1–21）。正文要 ≥ 4.5，大字 ≥ 3。"""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------- #
# PNG 编码 / PhotoImage
# --------------------------------------------------------------------------- #


def png_bytes(rgba: np.ndarray) -> bytes:
    """(h, w, 4) uint8 → PNG。不过滤（filter 0）+ zlib 低压缩：这些图大多是纯色块，
    压得很小，再多花 CPU 不值得。"""
    h, w = rgba.shape[:2]
    raw = np.zeros((h, 1 + w * 4), dtype=np.uint8)
    raw[:, 1:] = rgba.reshape(h, w * 4)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 3))
        + chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> np.ndarray:
    """只认我们自己编出来的 PNG（8 位 RGBA、filter 0）。测试用，证明编码是对的。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, w, h, idat = 8, 0, 0, b""
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + n]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + w * 4)
    return raw[:, 1:].reshape(h, w, 4).copy()


def _root_of(master: tk.Misc) -> Any:
    return master._root()  # noqa: SLF001 - tkinter 没有公开的"拿根窗口"接口


def cache_of(master: tk.Misc) -> dict[Any, Any]:
    root = _root_of(master)
    try:
        return root._voice_ctl_gfx  # type: ignore[no-any-return]
    except AttributeError:
        root._voice_ctl_gfx = {}  # noqa: SLF001
        return root._voice_ctl_gfx  # type: ignore[no-any-return]


def photo(master: tk.Misc, key: Any, build: Callable[[], np.ndarray]) -> tk.PhotoImage:
    """按 key 缓存的 PhotoImage。build 只在第一次调用。"""
    cache = cache_of(master)
    img = cache.get(key)
    if img is None:
        rgba = build()
        img = tk.PhotoImage(master=_root_of(master), data=base64.b64encode(png_bytes(rgba)))
        cache[key] = img
    return img


# --------------------------------------------------------------------------- #
# 图层：预乘 alpha 的浮点画布
# --------------------------------------------------------------------------- #


class Layer:
    """往上面一层层 paint 覆盖率蒙版。最后 `rgba()` 出 straight-alpha 的 uint8。"""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.px = np.zeros((h, w, 4), dtype=np.float32)  # 预乘 RGBA，0..1

    def paint(self, cov: np.ndarray, color: str | np.ndarray, opacity: float = 1.0) -> Layer:
        """cov: (h, w) 覆盖率 0..1。color: '#rrggbb' 或 (h, w, 3) 的 0..255 数组。"""
        a = (cov * opacity).astype(np.float32)
        if isinstance(color, str):
            c = np.array(to_rgb(color), dtype=np.float32) / 255.0
            src_rgb = a[..., None] * c
        else:
            src_rgb = a[..., None] * (color.astype(np.float32) / 255.0)
        inv = 1.0 - a
        self.px[..., :3] = src_rgb + self.px[..., :3] * inv[..., None]
        self.px[..., 3] = a + self.px[..., 3] * inv
        return self

    def rgba(self) -> np.ndarray:
        a = self.px[..., 3]
        safe = np.where(a > 1e-6, a, 1.0)
        rgb = np.clip(self.px[..., :3] / safe[..., None], 0.0, 1.0)
        out = np.empty((self.h, self.w, 4), dtype=np.uint8)
        out[..., :3] = np.rint(rgb * 255.0)
        out[..., 3] = np.rint(np.clip(a, 0.0, 1.0) * 255.0)
        return out


# --------------------------------------------------------------------------- #
# 距离场
# --------------------------------------------------------------------------- #


def grid(w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """像素中心坐标。像素 (i, j) 覆盖 [i, i+1]×[j, j+1]，中心在 (i+.5, j+.5)。"""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    return xs + 0.5, ys + 0.5


def cov(d: np.ndarray) -> np.ndarray:
    """有符号距离（像素，内部为负）→ 覆盖率。1px 宽的线性斜坡 = 标准的盒式抗锯齿。"""
    return np.clip(0.5 - d, 0.0, 1.0)


def sd_rrect(xs: np.ndarray, ys: np.ndarray, x0: float, y0: float, x1: float, y1: float, r: float) -> np.ndarray:
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    qx = np.abs(xs - cx) - ((x1 - x0) / 2 - r)
    qy = np.abs(ys - cy) - ((y1 - y0) / 2 - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def sd_circle(xs: np.ndarray, ys: np.ndarray, cx: float, cy: float, r: float) -> np.ndarray:
    return np.hypot(xs - cx, ys - cy) - r


def sd_segment(xs: np.ndarray, ys: np.ndarray, ax: float, ay: float, bx: float, by: float) -> np.ndarray:
    """到线段 ab 的距离（≥0）。"""
    pax, pay = xs - ax, ys - ay
    bax, bay = bx - ax, by - ay
    denom = bax * bax + bay * bay
    t = np.clip((pax * bax + pay * bay) / denom, 0.0, 1.0) if denom > 0 else np.zeros_like(xs)
    return np.hypot(pax - bax * t, pay - bay * t)


def sd_polyline(xs: np.ndarray, ys: np.ndarray, pts: Sequence[tuple[float, float]], closed: bool = False) -> np.ndarray:
    d = np.full(xs.shape, 1e9, dtype=np.float32)
    n = len(pts)
    last = n if closed else n - 1
    for i in range(last):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        d = np.minimum(d, sd_segment(xs, ys, ax, ay, bx, by))
    return d


def sd_polygon(xs: np.ndarray, ys: np.ndarray, pts: Sequence[tuple[float, float]]) -> np.ndarray:
    """多边形的有符号距离（内部为负）。奇偶规则判内外。"""
    d = sd_polyline(xs, ys, pts, closed=True)
    inside = np.zeros(xs.shape, dtype=bool)
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        if ay == by:
            continue
        cond = (ay > ys) != (by > ys)
        xint = ax + (ys - ay) * (bx - ax) / (by - ay)
        inside ^= cond & (xs < xint)
    return np.where(inside, -d, d)


def arc_points(cx: float, cy: float, r: float, a0: float, a1: float, step: float = 6.0) -> list[tuple[float, float]]:
    """圆弧采样点。角度制，0° 指向 +x，顺时针为正（屏幕坐标 y 向下）。"""
    n = max(2, int(abs(a1 - a0) / step) + 1)
    return [
        (cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / (n - 1))),
         cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / (n - 1))))
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# 圆角矩形
# --------------------------------------------------------------------------- #


def rrect_rgba(
    w: int,
    h: int,
    r: float,
    fill: str | None,
    border: str | None = None,
    bw: float = 1.0,
    *,
    ring: str | None = None,
    ring_w: float = 2.0,
) -> np.ndarray:
    """圆角矩形（可带描边与外圈焦点环）。

    描边画在形状**内侧**：这样 w×h 就是它实际占的大小，几个控件并排时对得齐。
    `ring` 是焦点环：画在形状外面（隔 1px 透明缝），所以带焦点环的图要比不带的
    四周各大 ring_w+1 像素，由调用方留好余量。
    """
    xs, ys = grid(w, h)
    lay = Layer(w, h)
    pad = ring_w + 1.0 if ring else 0.0
    x0, y0, x1, y1 = pad, pad, w - pad, h - pad
    if ring:
        # 环与形状之间留 1px 透明缝（露出父级底色），焦点环才看得出是"环"而不是加粗的描边
        gap = sd_rrect(xs, ys, x0 - 1.0, y0 - 1.0, x1 + 1.0, y1 + 1.0, r + 1.0)
        outer_ring = cov(sd_rrect(xs, ys, 0, 0, w, h, r + ring_w + 1.0))
        lay.paint(np.clip(outer_ring - cov(gap), 0.0, 1.0), ring)
    outer = cov(sd_rrect(xs, ys, x0, y0, x1, y1, r))
    if border:
        lay.paint(outer, border)
        if fill:
            inner = cov(sd_rrect(xs, ys, x0 + bw, y0 + bw, x1 - bw, y1 - bw, max(0.0, r - bw)))
            lay.paint(inner, fill)
    elif fill:
        lay.paint(outer, fill)
    return lay.rgba()


def corner_tiles(r: int, fill: str | None, border: str | None, bw: int) -> list[np.ndarray]:
    """九宫格的四个角：[左上, 右上, 左下, 右下]，每块 r×r。

    面板大小会随窗口变，整张重画太贵；而圆角只占四个小角——角用图、边用色块，
    窗口怎么拉都只是挪几个 canvas item 的坐标。
    """
    full = rrect_rgba(2 * r, 2 * r, r, fill, border, bw)
    return [full[:r, :r], full[:r, r:], full[r:, :r], full[r:, r:]]


# --------------------------------------------------------------------------- #
# 图标（24×24 设计网格）
# --------------------------------------------------------------------------- #

STROKE = 1.8

# 图元：
#   ("L", x0, y0, x1, y1)           线段
#   ("P", [(x, y), ...])            折线        ("PC" = 闭合)
#   ("C", cx, cy, r)                实心圆
#   ("O", cx, cy, r)                圆环
#   ("R", x, y, w, h, r)            实心圆角矩形
#   ("RO", x, y, w, h, r)           圆角矩形描边
#   ("A", cx, cy, r, a0, a1)        圆弧描边
#   ("F", [(x, y), ...])            实心多边形
#   ("cut", 图元)                    把前面画的东西里落在该图元（实心）内的部分挖掉
Prim = tuple[Any, ...]

ICONS: dict[str, list[Prim]] = {
    "mic": [
        ("RO", 9, 2.6, 6, 11.4, 3),
        ("A", 12, 11.2, 6.6, 0, 180),
        ("L", 12, 17.8, 12, 21.2),
        ("L", 8.6, 21.2, 15.4, 21.2),
    ],
    "list": [
        ("L", 9, 6.5, 20, 6.5), ("L", 9, 12, 20, 12), ("L", 9, 17.5, 20, 17.5),
        ("C", 4.6, 6.5, 1.25), ("C", 4.6, 12, 1.25), ("C", 4.6, 17.5, 1.25),
    ],
    "keyboard": [
        ("RO", 2.6, 5.6, 18.8, 12.8, 2.6),
        ("L", 6.6, 9.6, 6.61, 9.6), ("L", 10.2, 9.6, 10.21, 9.6),
        ("L", 13.8, 9.6, 13.81, 9.6), ("L", 17.4, 9.6, 17.41, 9.6),
        ("L", 8, 14.4, 16, 14.4),
    ],
    "bolt": [("PC", [(13.2, 2.6), (5, 13.4), (11.6, 13.4), (10.8, 21.4), (19, 10.4), (12.4, 10.4)])],
    "sliders": [
        ("L", 3.6, 7, 6.6, 7), ("L", 11.4, 7, 20.4, 7), ("O", 9, 7, 2.4),
        ("L", 3.6, 17, 12.6, 17), ("L", 17.4, 17, 20.4, 17), ("O", 15, 17, 2.4),
    ],
    "info": [("O", 12, 12, 9.4), ("L", 12, 11, 12, 16.6), ("C", 12, 7.7, 1.15)],
    "play": [("F", [(7.2, 4.6), (19.4, 12), (7.2, 19.4)])],
    "stop": [("R", 6, 6, 12, 12, 2.6)],
    "pause": [("R", 6.4, 5, 4, 14, 1.4), ("R", 13.6, 5, 4, 14, 1.4)],
    "power": [("A", 12, 13, 7.6, 305, 595), ("L", 12, 3.4, 12, 11.4)],
    "search": [("O", 10.6, 10.6, 6.4), ("L", 15.4, 15.4, 20.4, 20.4)],
    "x": [("L", 6, 6, 18, 18), ("L", 18, 6, 6, 18)],
    "check": [("P", [(5, 12.6), (10, 17.4), (19, 7.4)])],
    "chev_down": [("P", [(6.2, 9.4), (12, 15.2), (17.8, 9.4)])],
    "chev_up": [("P", [(6.2, 14.6), (12, 8.8), (17.8, 14.6)])],
    "chev_right": [("P", [(9.4, 6.2), (15.2, 12), (9.4, 17.8)])],
    "plus": [("L", 12, 5, 12, 19), ("L", 5, 12, 19, 12)],
    "minus": [("L", 5, 12, 19, 12)],
    "trash": [
        ("L", 4, 6.6, 20, 6.6),
        ("P", [(9, 6.6), (9, 3.8), (15, 3.8), (15, 6.6)]),
        ("P", [(6, 6.6), (7, 20), (17, 20), (18, 6.6)]),
        ("L", 10.2, 10.6, 10.2, 16.2), ("L", 13.8, 10.6, 13.8, 16.2),
    ],
    "arrow_up": [("L", 12, 19, 12, 5), ("P", [(6.2, 10.6), (12, 4.8), (17.8, 10.6)])],
    "arrow_down": [("L", 12, 5, 12, 19), ("P", [(6.2, 13.4), (12, 19.2), (17.8, 13.4)])],
    "copy": [
        ("RO", 8.6, 8.6, 11.4, 11.4, 2.6),
        ("RO", 3.6, 3.6, 11.4, 11.4, 2.6),
        ("cut", ("R", 7.1, 7.1, 14.4, 14.4, 3.2)),
        ("RO", 8.6, 8.6, 11.4, 11.4, 2.6),
    ],
    "folder": [("PC", [(3, 6), (9.2, 6), (11.4, 8.6), (21, 8.6), (21, 19.2), (3, 19.2)])],
    "refresh": [
        ("A", 12, 12, 7.8, 20, 300),
        ("P", [(17.4, 3.4), (18.2, 8.6), (13, 9.2)]),
    ],
    "download": [
        ("L", 12, 4, 12, 15), ("P", [(7, 10.2), (12, 15.2), (17, 10.2)]),
        ("P", [(4.6, 15.6), (4.6, 19.6), (19.4, 19.6), (19.4, 15.6)]),
    ],
    "external": [
        ("P", [(14, 4), (20, 4), (20, 10)]), ("L", 20, 4, 11, 13),
        ("P", [(17.4, 14.6), (17.4, 19.6), (4.4, 19.6), (4.4, 6.6), (9.6, 6.6)]),
    ],
    "wave": [
        ("L", 4, 10, 4, 14), ("L", 8, 6.4, 8, 17.6), ("L", 12, 3.4, 12, 20.6),
        ("L", 16, 7, 16, 17), ("L", 20, 10, 20, 14),
    ],
    "warn": [("PC", [(12, 3.6), (21.4, 19.8), (2.6, 19.8)]), ("L", 12, 9.6, 12, 13.8), ("L", 12, 16.7, 12.01, 16.7)],
    "err": [("O", 12, 12, 9.4), ("L", 8.8, 8.8, 15.2, 15.2), ("L", 15.2, 8.8, 8.8, 15.2)],
    "ok": [("O", 12, 12, 9.4), ("P", [(8, 12.4), (11, 15.4), (16.2, 9.4)])],
    "send": [("PC", [(3.4, 11), (20.6, 3.4), (14, 20.6), (11, 13.2)]), ("L", 11, 13.2, 20.6, 3.4)],
    "speaker": [
        ("PC", [(4, 9.6), (8, 9.6), (12.6, 5.6), (12.6, 18.4), (8, 14.4), (4, 14.4)]),
        ("A", 12.6, 12, 4.6, -48, 48), ("A", 12.6, 12, 8.6, -48, 48),
    ],
    "flask": [
        ("PC", [(9.6, 3.4), (9.6, 9.6), (4.6, 18.4), (5.2, 19.8), (18.8, 19.8), (19.4, 18.4), (14.4, 9.6), (14.4, 3.4)]),
        ("L", 8, 3.4, 16, 3.4),
    ],
    "file": [
        ("PC", [(6, 3.4), (14, 3.4), (19, 8.4), (19, 20.6), (6, 20.6)]),
        ("P", [(14, 3.6), (14, 8.4), (18.8, 8.4)]),
    ],
    "scroll": [("L", 12, 4, 12, 15.4), ("P", [(6.6, 10.4), (12, 15.8), (17.4, 10.4)]), ("L", 5, 20, 19, 20)],
    "lock": [("RO", 5, 10.6, 14, 10.4, 2.6), ("A", 12, 10.6, 4.6, 180, 360)],
    "shield": [
        ("PC", [(12, 2.8), (19.6, 5.6), (19.6, 12), (12, 21.2), (4.4, 12), (4.4, 5.6)]),
        ("P", [(8.6, 12), (11.0, 14.4), (15.6, 9.4)]),
    ],
    "undo": [("A", 12, 13, 7, 200, 340), ("P", [(3.8, 6.6), (4.6, 12.2), (10, 11.2)])],
    "dot": [("C", 12, 12, 4)],
}


def _icon_sd(xs: np.ndarray, ys: np.ndarray, prim: Prim, k: float, hw: float) -> np.ndarray:
    """单个图元的有符号距离（像素）。k = 设计网格 → 像素的缩放；hw = 半线宽（像素）。"""
    kind = prim[0]
    p = prim[1:]
    if kind == "L":
        return sd_segment(xs, ys, p[0] * k, p[1] * k, p[2] * k, p[3] * k) - hw
    if kind in ("P", "PC"):
        pts = [(x * k, y * k) for x, y in p[0]]
        return sd_polyline(xs, ys, pts, closed=(kind == "PC")) - hw
    if kind == "C":
        return sd_circle(xs, ys, p[0] * k, p[1] * k, p[2] * k)
    if kind == "O":
        return np.abs(sd_circle(xs, ys, p[0] * k, p[1] * k, p[2] * k)) - hw
    if kind == "R":
        return sd_rrect(xs, ys, p[0] * k, p[1] * k, (p[0] + p[2]) * k, (p[1] + p[3]) * k, p[4] * k)
    if kind == "RO":
        return np.abs(sd_rrect(xs, ys, p[0] * k, p[1] * k, (p[0] + p[2]) * k, (p[1] + p[3]) * k, p[4] * k)) - hw
    if kind == "A":
        pts = [(x * k, y * k) for x, y in arc_points(p[0], p[1], p[2], p[3], p[4])]
        return sd_polyline(xs, ys, pts) - hw
    if kind == "F":
        pts = [(x * k, y * k) for x, y in p[0]]
        # 实心多边形也带一点圆头线宽，尖角不会在小尺寸下被削没
        return np.minimum(sd_polygon(xs, ys, pts), sd_polyline(xs, ys, pts, closed=True) - hw * 0.55)
    raise ValueError(f"未知图元 {kind!r}")


def icon_rgba(name: str, size: int, color: str, stroke: float = STROKE) -> np.ndarray:
    prims = ICONS[name]
    xs, ys = grid(size, size)
    k = size / 24.0
    hw = stroke * k / 2.0
    d = np.full((size, size), 1e9, dtype=np.float32)
    for prim in prims:
        if prim[0] == "cut":
            hole = _icon_sd(xs, ys, prim[1], k, hw)
            d = np.maximum(d, -hole)
        else:
            d = np.minimum(d, _icon_sd(xs, ys, prim, k, hw))
    return Layer(size, size).paint(cov(d), color).rgba()


def icon(master: tk.Misc, name: str, size: int, color: str, stroke: float = STROKE) -> tk.PhotoImage:
    return photo(master, ("icon", name, size, color, stroke), lambda: icon_rgba(name, size, color, stroke))


# --------------------------------------------------------------------------- #
# 光点（点阵电平用）
# --------------------------------------------------------------------------- #


def dot_rgba(d: float, pad: float, color: str, glow: float = 0.0) -> np.ndarray:
    """直径 d 的实心圆点；glow>0 时外面带一圈柔光。pad 是四周留给光晕的余量。"""
    size = int(math.ceil(d + 2 * pad))
    xs, ys = grid(size, size)
    c = size / 2.0
    dist = np.hypot(xs - c, ys - c)
    lay = Layer(size, size)
    if glow > 0:
        halo = np.exp(-((np.maximum(dist - d / 2, 0) / max(pad * 0.55, 0.01)) ** 2))
        lay.paint(halo, color, 0.34 * glow)
    lay.paint(cov(dist - d / 2), color)
    return lay.rgba()


# --------------------------------------------------------------------------- #
# 键帽
# --------------------------------------------------------------------------- #


def keycap_rgba(
    w: int,
    h: int,
    depth: int,
    r: float,
    face_top: str,
    face_bottom: str,
    side: str,
    *,
    pressed: bool = False,
    edge: str | None = None,
) -> np.ndarray:
    """一个有厚度的键帽。总高 = h + depth：上面 h 是键面，底下 depth 是侧壁。

    按下时键面下沉 depth-1 像素，几乎盖住侧壁——这就是"按下去"的全部动画，
    不需要任何阴影或模糊。
    """
    total = h + depth
    xs, ys = grid(w, total)
    lay = Layer(w, total)
    lay.paint(cov(sd_rrect(xs, ys, 0, 0, w, total, r)) * (0.0 if pressed else 1.0), side)
    top = depth - 1 if pressed else 0
    face_cov = cov(sd_rrect(xs, ys, 0, top, w, top + h, r))
    if pressed:
        # 下沉后底边只剩 1px 侧壁
        lay.paint(cov(sd_rrect(xs, ys, 0, top + 1, w, top + h + 1, r)), side)
    t = np.clip((ys - top) / max(h, 1), 0.0, 1.0)[..., None]
    a, b = np.array(to_rgb(face_top), np.float32), np.array(to_rgb(face_bottom), np.float32)
    lay.paint(face_cov, a * (1 - t) + b * t)
    if edge:
        ring = cov(sd_rrect(xs, ys, 0, top, w, top + h, r)) - cov(sd_rrect(xs, ys, 1, top + 1, w - 1, top + h - 1, max(r - 1, 0)))
        lay.paint(np.clip(ring, 0, 1) * (1.0 - t[..., 0] * 0.6), edge, 0.55)
    return lay.rgba()


# --------------------------------------------------------------------------- #
# 应用图标
# --------------------------------------------------------------------------- #


def app_icon_rgba(size: int, ink: str = "#151A1C", bars: str = "#FF5B1F") -> np.ndarray:
    """墨色圆角方块里五根橙色竖条——一段正在被说出来的话的电平。

    五根条的高度是手挑的（中间最高、两侧不对称），对称的看起来像"信号强度"，
    不对称的才像声音。16px 时每根条只有 1–2 像素，依然能认出来。
    """
    xs, ys = grid(size, size)
    lay = Layer(size, size)
    lay.paint(cov(sd_rrect(xs, ys, 0, 0, size, size, size * 0.24)), ink)
    heights = (0.26, 0.5, 0.76, 0.44, 0.3)
    bar_w = size * 0.1
    gap = size * 0.065
    total = 5 * bar_w + 4 * gap
    x0 = (size - total) / 2
    d = np.full((size, size), 1e9, dtype=np.float32)
    for i, hgt in enumerate(heights):
        cx = x0 + i * (bar_w + gap) + bar_w / 2
        half = size * hgt / 2
        d = np.minimum(d, sd_rrect(xs, ys, cx - bar_w / 2, size / 2 - half, cx + bar_w / 2, size / 2 + half, bar_w / 2))
    lay.paint(cov(d), bars)
    return lay.rgba()


def app_icon(master: tk.Misc, size: int) -> tk.PhotoImage:
    return photo(master, ("appicon", size), lambda: app_icon_rgba(size))


__all__ = [
    "ICONS",
    "app_icon",
    "app_icon_rgba",
    "Layer",
    "arc_points",
    "cache_of",
    "contrast",
    "corner_tiles",
    "cov",
    "decode_png",
    "dot_rgba",
    "icon",
    "icon_rgba",
    "keycap_rgba",
    "luminance",
    "mix",
    "photo",
    "png_bytes",
    "rrect_rgba",
    "to_hex",
    "to_rgb",
]
