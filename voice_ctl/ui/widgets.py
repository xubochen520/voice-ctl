"""组合控件：导航项、电源键、键帽、点阵电平表、热键录制区、日志视图。

只放真正被页面用到的东西。基础控件（按钮、开关、输入框……）在 kit.py / inputs.py。
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from typing import Any

import numpy as np

from .. import events
from . import gfx, theme
from .inputs import Field, ScrollFrame, Scrollbar, Select, Slider, Stepper
from .kit import (
    Button,
    Callout,
    Card,
    Flow,
    IconButton,
    Panel,
    RoundedBg,
    Segmented,
    StatusPill,
    Switch,
    Tooltip,
    bg_of,
    check,
    hline,
    wrap_label,
)
from .theme import PALETTE as P
from .theme import S, hair, measure

# 旧名字：页面里一直叫 W.hint / W.Card，保持可用
hint = wrap_label

STATE_TONE = {"idle": "idle", "loading": "warn", "running": "ok", "error": "error"}
"""引擎状态 → StatusPill 的语义色。放在这儿是为了让侧栏、运行页两处永远一致。"""


# --------------------------------------------------------------------------- #
# 热键的显示
# --------------------------------------------------------------------------- #

_KEY_LABEL = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win", "space": "空格",
    "enter": "Enter", "esc": "Esc", "tab": "Tab", "backspace": "退格", "delete": "Delete",
    "up": "↑", "down": "↓", "left": "←", "right": "→", "page_up": "PgUp", "page_down": "PgDn",
    "caps_lock": "Caps", "home": "Home", "end": "End", "insert": "Insert",
}


def key_labels(spec: str) -> list[str]:
    """'<ctrl>+<alt>+space' → ['Ctrl', 'Alt', '空格']。"""
    from ..hotkey import _canonical  # noqa: PLC2701 - 复用同一套键名归一

    out: list[str] = []
    for raw in spec.split("+"):
        if not raw.strip():
            continue
        name = _canonical(raw)
        out.append(_KEY_LABEL.get(name, name.upper() if len(name) <= 3 else name.capitalize()))
    return out


def hotkey_text(spec: str) -> str:
    """给人看的热键文字：'Ctrl+Alt+空格'。别把 pynput 的 '<ctrl>+<alt>+space' 直接摆到界面上。"""
    return "+".join(key_labels(spec))


# --------------------------------------------------------------------------- #
# 键帽
# --------------------------------------------------------------------------- #


class KeyCaps(tk.Canvas):
    """一排有厚度的键帽。`pressed=True` 时键面下沉并变橙——按住热键说话时它真的"按下去"。

    键帽的"按下"只是把键面往下挪 depth-1 像素、盖住侧壁，再换一个暖色。
    没有阴影、没有模糊、没有动画，但一眼就知道键是按着的。
    """

    SIZES = {"md": (32, 4, 8, "key", "key_cjk", 46), "lg": (54, 7, 12, "key_l", "key_cjk_l", 80)}

    def __init__(self, master: tk.Misc, spec: str = "", *, size: str = "md", dark: bool = True,
                 bg: str | None = None) -> None:
        self._bg = bg or bg_of(master)
        self._dark = dark
        hf, depth, r, latin, cjk, minw = self.SIZES[size]
        self._hf, self._depth, self._r = S(hf), S(depth), S(r)
        self._latin, self._cjk, self._minw = theme.FONTS[latin], theme.FONTS[cjk], S(minw)
        super().__init__(master, bg=self._bg, highlightthickness=0, bd=0, height=self._hf + self._depth)
        self._labels = key_labels(spec)
        self._pressed = False
        self._draw()

    @property
    def labels(self) -> list[str]:
        return list(self._labels)

    @property
    def pressed(self) -> bool:
        return self._pressed

    def set_spec(self, spec: str) -> None:
        labels = key_labels(spec)
        if labels != self._labels:
            self._labels = labels
            self._draw()

    def set_pressed(self, on: bool) -> None:
        if on != self._pressed:
            self._pressed = on
            self._draw()

    def _colors(self) -> tuple[str, str, str, str | None]:
        if self._pressed:
            return "#FFC8B0", "#FF9E78", "#C4480F", None
        if self._dark:
            return "#F2F6F5", "#D2D9D8", "#8A9695", "#FFFFFF"
        return "#FFFFFF", "#E8EDEC", "#B4BEBD", None

    def _draw(self) -> None:
        self.delete("all")
        top, bottom, side, edge = self._colors()
        hf, depth, r = self._hf, self._depth, self._r
        gap = S(10)
        x = 0
        down = depth - 1 if self._pressed else 0
        for i, label in enumerate(self._labels):
            ascii_only = label.isascii()
            font = self._latin if ascii_only else self._cjk
            kw = max(self._minw, measure(label, font) + S(26))
            if label == "空格":
                kw = int(kw * 1.5)
            img = gfx.photo(self, ("kc", kw, hf, depth, r, top, bottom, side, edge, self._pressed),
                            lambda kw=kw: gfx.keycap_rgba(kw, hf, depth, r, top, bottom, side,
                                                          pressed=self._pressed, edge=edge))
            self.create_image(x, 0, anchor="nw", image=img)
            self.create_text(x + kw // 2, down + hf // 2, text=label, font=font, fill=P["ink"])
            x += kw
            if i < len(self._labels) - 1:
                x += gap
        self.configure(width=max(x, 1), height=hf + depth)


# --------------------------------------------------------------------------- #
# 点阵电平表
# --------------------------------------------------------------------------- #

_PALETTES = ("off", "orange", "teal", "green", "red", "amber", "faint")
_PAL_COLOR = {
    "orange": "live", "teal": "teal_lit", "green": "ok_lit", "red": "err_lit", "amber": "warn_lit", "faint": "scr_faint",
}
_LV = 5  # 每个调色板 0..4 五档亮度
_FLASH_PAL = {"ok": 3, "fail": 4, "nomatch": 5}


class DotMeter(tk.Canvas):
    """LED 点阵：左边是过去，右边是此刻，竖向从中线向上下长——录音时就是一条滚动的声波。

    模式：
      off    没在监听。只有中线一排暗点。
      ready  在监听、等你按键。中线上一道很慢的青色光扫过去。
      rec    正在录音。橙色，随 `push(level)` 送进来的电平起伏，越新越亮。
      busy   松手后识别中。青色光带来回扫。
      flash  出结果了。从中线向两边扩散一圈绿/红/琥珀的涟漪。

    **整块点阵只有一张图**，每帧用 numpy 拼好、以 PPM（无压缩）写进同一个 PhotoImage。
    第一版是 648 个独立的带 alpha 的图片项，实测一帧 47ms：Tk 的 Canvas 重绘是按"变化区域的
    外包矩形"来的，录音时几乎每个点都在变，于是每帧要把整块区域里 648 张 alpha 图读回背景再混合
    一遍——30fps 追不上，`update()` 里无限循环（表现为界面卡死、CPU 满载）。
    单张图一帧 3ms。点阵放在纯色底上，不需要 alpha，所以格子里直接预先和底色合成好。

    帧和上一帧一样就什么都不做（静态的"未启动"零开销）。
    """

    ROWS = 9

    def __init__(self, master: tk.Misc, *, bg: str | None = None, pitch: float = 10.5, dot: float = 4.4,
                 max_cols: int = 110) -> None:
        self._bg = bg or bg_of(master)
        self._pitch = max(6, S(pitch))
        self._d = max(3, S(dot))
        self._max_cols = max_cols
        super().__init__(master, bg=self._bg, highlightthickness=0, bd=0, height=self.ROWS * self._pitch)
        self._cols = 0
        self._ox = 0
        self._codes = np.zeros((0, self.ROWS), dtype=np.int16)
        self._mode = "off"
        self._hist: list[float] = []
        self._t0 = time.monotonic()
        self._flash_t0 = 0.0
        self._flash_pal = 3
        self._tiles = self._build_tiles()
        # 不给 PhotoImage 指定宽高：宽高为 0 时它会随写入的数据自动伸缩
        self._frame_img = tk.PhotoImage(master=self)
        self._item = self.create_image(0, 0, anchor="nw", image=self._frame_img)
        self.bind("<Configure>", self._on_configure)

    # -- 模式 ------------------------------------------------------------- #

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            if mode == "rec":
                self._hist = []
            self._t0 = time.monotonic()
            self.step()

    def flash(self, kind: str) -> None:
        """出结果：kind 是 ok / fail / nomatch。"""
        self._mode = "flash"
        self._flash_pal = _FLASH_PAL.get(kind, 3)
        self._flash_t0 = time.monotonic()
        self.step()

    @property
    def flash_done(self) -> bool:
        return self._mode != "flash" or (time.monotonic() - self._flash_t0) > 1.3

    def push(self, level: float) -> None:
        """送入一个 0–1 的电平。录音器给的是衰减峰值，说话时多在 0.05–0.5，
        所以做一次幂函数拉伸，否则小声说话只会亮中间一个点。"""
        a = float(np.clip((max(level, 0.0) * 2.4) ** 0.55, 0.0, 1.0))
        self._hist.append(a)
        if len(self._hist) > max(1, self._cols):
            del self._hist[: len(self._hist) - self._cols]

    def lit_count(self) -> int:
        """亮着的（非暗灰）点数。测试靠它判断"电平条有没有反应"。"""
        pal = self._codes // _LV
        lv = self._codes % _LV
        return int(np.count_nonzero((pal >= 1) & (pal <= 5) & (lv > 0)))

    # -- 绘制 ------------------------------------------------------------- #

    def _build_tiles(self) -> np.ndarray:
        """每个亮度码一块 pitch×pitch 的 RGB 小图，圆点和底色提前合成好。
        形状 (码数, pitch, pitch, 3)，拼帧时一次高级索引就取齐。"""
        pitch, d = self._pitch, self._d
        bg = np.array(gfx.to_rgb(self._bg), dtype=np.float32)
        off = P["dot_off"]
        tiles = np.empty((len(_PALETTES) * _LV, pitch, pitch, 3), dtype=np.uint8)
        for code in range(len(_PALETTES) * _LV):
            pal, lv = divmod(code, _LV)
            if pal == 0 or (lv == 0 and pal != 6):
                color, glow = off, 0.0
            elif pal == 6:
                color, glow = gfx.mix(off, P["scr_faint"], 0.55), 0.0
            else:
                lit = P[_PAL_COLOR[_PALETTES[pal]]]
                color = gfx.mix(off, lit, 0.30 + 0.70 * (lv / (_LV - 1)))
                glow = max(0.0, (lv - 1) / (_LV - 2))
            rgba = gfx.dot_rgba(d, max(1.0, (pitch - d) / 2.0), color, glow)
            h, w = rgba.shape[:2]
            tile = np.empty((pitch, pitch, 3), dtype=np.float32)
            tile[:] = bg
            # 光晕可能比格子大一点：以中心对齐裁切/居中
            sy0, sx0 = max(0, (h - pitch) // 2), max(0, (w - pitch) // 2)
            ty0, tx0 = max(0, (pitch - h) // 2), max(0, (pitch - w) // 2)
            hh, ww = min(h - sy0, pitch - ty0), min(w - sx0, pitch - tx0)
            src = rgba[sy0:sy0 + hh, sx0:sx0 + ww]
            a = src[..., 3:4].astype(np.float32) / 255.0
            tile[ty0:ty0 + hh, tx0:tx0 + ww] = src[..., :3] * a + tile[ty0:ty0 + hh, tx0:tx0 + ww] * (1.0 - a)
            tiles[code] = np.clip(np.rint(tile), 0, 255).astype(np.uint8)
        return tiles

    def _on_configure(self, e: tk.Event) -> None:
        self._build(e.width)
        self.step()

    def _build(self, width: int) -> None:
        cols = int(min(self._max_cols, max(8, (width - self._pitch) // self._pitch)))
        self._ox = max(0, (width - cols * self._pitch) // 2)
        self.coords(self._item, self._ox, 0)
        if cols != self._cols:
            self._cols = cols
            self._codes = np.full((cols, self.ROWS), -1, dtype=np.int16)  # -1：强制下一帧一定重画
            self._hist = self._hist[-cols:]
        self.configure(height=self.ROWS * self._pitch)

    def _frame(self, now: float) -> np.ndarray:
        C, R = self._cols, self.ROWS
        mid = R // 2
        codes = np.zeros((C, R), dtype=np.int16)
        if C == 0:
            return codes
        cols = np.arange(C, dtype=np.float32)[:, None]
        rows = np.arange(R, dtype=np.float32)[None, :]
        dist = np.abs(rows - mid)
        t = now - self._t0
        faint = 6 * _LV + 1
        codes[:, mid] = faint  # 中线永远有一排暗点：没有声音时它就是"一条平线"

        if self._mode == "ready":
            p = ((t % 5.0) / 5.0) * (C + 12) - 6
            w = np.exp(-(((cols - p) / 3.2) ** 2))[:, 0]
            lv_mid = np.clip(np.rint(1 + 3 * w), 1, 4).astype(np.int16)
            codes[:, mid] = np.where(w > 0.08, 2 * _LV + lv_mid, 2 * _LV + 1)
            side = np.rint(2.4 * w).astype(np.int16)
            for row in (mid - 1, mid + 1):
                codes[:, row] = np.where(side >= 1, 2 * _LV + np.clip(side, 1, 2), 0)
        elif self._mode == "busy":
            p = ((t * 28.0) % (C + 16)) - 8
            w = np.exp(-(((cols - p) / 3.6) ** 2)) * np.exp(-((dist / 1.9) ** 2))
            lv = np.clip(np.rint(4 * w), 0, 4).astype(np.int16)
            codes = np.where(lv > 0, 2 * _LV + lv, codes).astype(np.int16)
        elif self._mode == "rec":
            hist = np.full(C, -1.0, dtype=np.float32)
            if self._hist:
                recent = np.array(self._hist[-C:], dtype=np.float32)
                hist[C - len(recent):] = recent
            age = 0.38 + 0.62 * (cols[:, 0] / max(1, C - 1))
            n = np.rint(np.clip(hist, 0, 1) * mid).astype(np.int16)[:, None]
            lit = (dist <= n) & (hist[:, None] >= 0)
            lv = np.clip(np.rint(1 + 3 * age[:, None] * (1 - 0.28 * dist / (n + 1))), 1, 4).astype(np.int16)
            codes = np.where(lit, _LV + lv, codes).astype(np.int16)
        elif self._mode == "flash":
            ft = now - self._flash_t0
            fade = max(0.0, 1.0 - ft / 1.2)
            front = ft * 36.0
            mid_c = (C - 1) / 2.0
            wave = np.exp(-((np.abs(cols - mid_c) - front) / 5.0) ** 2) * fade
            n = np.rint(wave[:, 0] * mid).astype(np.int16)[:, None]
            lv = np.clip(np.rint(wave * 4), 1, 4).astype(np.int16)
            lit = (dist <= n) & (wave > 0.08)
            codes = np.where(lit, self._flash_pal * _LV + lv, codes).astype(np.int16)
            if ft > 1.25:
                self._mode = "ready"
                self._t0 = now
        return codes

    def step(self, now: float | None = None) -> None:
        """算下一帧；和上一帧不同才重画整张图。由页面的动画循环调用。"""
        if not self._cols:
            # 还没收到过 <Configure>（窗口没映射 / 刚创建）：先按一个合理的宽度建，真正布局时会重建
            self._build(max(self.winfo_width(), S(640)))
        codes = self._frame(time.monotonic() if now is None else now)
        if np.array_equal(codes, self._codes):
            return
        self._codes = codes
        pitch, C, R = self._pitch, self._cols, self.ROWS
        # (列, 行, 像素行, 像素列, 3) → (行·像素行, 列·像素列, 3)
        frame = self._tiles[codes].transpose(1, 2, 0, 3, 4).reshape(R * pitch, C * pitch, 3)
        self._frame_img.configure(data=f"P6\n{C * pitch} {R * pitch}\n255\n".encode() + frame.tobytes(), format="ppm")


# --------------------------------------------------------------------------- #
# 侧栏导航项
# --------------------------------------------------------------------------- #


class NavItem(tk.Canvas):
    """侧栏的一项：图标在上、文字在下。选中时浮起一块白色圆角瓷砖。

    侧栏很窄，图标 + 下方小字比"图标 + 右侧文字"省宽度，也更像设备面板上的模式键。
    """

    def __init__(self, master: tk.Misc, text: str, *, icon: str, command: Callable[[], None] | None = None,
                 bg: str | None = None) -> None:
        self._bg = bg or bg_of(master)
        self._w0, self._h0 = S(80), S(64)
        super().__init__(master, width=self._w0, height=self._h0, bg=self._bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=True)
        self._command = command
        self._icon = icon
        self._text = text
        self._active = False
        self._hover = False
        self._kbd = False
        self._by_mouse = False
        self._badge = 0
        self._tile = RoundedBg(self, S(14), None, None, 0)
        self._tile.resize(self._w0, self._h0)
        self._ico = self.create_image(self._w0 // 2, S(23), image=gfx.icon(self, icon, S(22), P["ink3"]))
        self._txt = self.create_text(self._w0 // 2, S(48), text=text, font=theme.FONTS["nav"], fill=P["ink3"])
        self._bdg = self.create_image(self._w0 // 2 + S(11), S(11), state="hidden")
        self._bdg_t = self.create_text(self._w0 // 2 + S(11), S(11), state="hidden", fill=P["white"],
                                       font=theme.FONTS["digits_s"])
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._click)
        self.bind("<space>", lambda _e: (self._invoke(), "break")[1])
        self.bind("<Return>", lambda _e: (self._invoke(), "break")[1])
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._paint()

    def _invoke(self) -> None:
        if self._command:
            self._command()

    def _click(self, _e: tk.Event) -> None:
        self._by_mouse = True
        self._kbd = False
        self.focus_set()
        self._invoke()

    def _focus_in(self, _e: tk.Event) -> None:
        self._kbd = not self._by_mouse
        self._paint()

    def _focus_out(self, _e: tk.Event) -> None:
        self._by_mouse = False
        self._kbd = False
        self._paint()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._paint()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._paint()

    def set_text(self, text: str) -> None:
        self._text = text
        self.itemconfigure(self._txt, text=text)

    def set_badge(self, n: int) -> None:
        """右上角的小红点计数（日志页有新错误时）。0 = 不显示。"""
        if n == self._badge:
            return
        self._badge = n
        if n <= 0:
            self.itemconfigure(self._bdg, state="hidden")
            self.itemconfigure(self._bdg_t, state="hidden")
            return
        label = str(n) if n < 10 else "9+"
        w, h = max(S(16), measure(label, theme.FONTS["digits_s"]) + S(8)), S(16)
        img = gfx.photo(self, ("badge", w, h), lambda: gfx.rrect_rgba(w, h, h / 2, P["err"], None, 0))
        self.itemconfigure(self._bdg, image=img, state="normal")
        self.itemconfigure(self._bdg_t, text=label, state="normal")

    def _paint(self) -> None:
        if self._kbd:
            self._tile.set(fill=P["raised"], border=P["teal"], bw=S(2))
        elif self._active:
            self._tile.set(fill=P["raised"], border=P["line"], bw=hair())
        elif self._hover:
            self._tile.set(fill=gfx.mix(self._bg, P["ink"], 0.06), border=None, bw=0)
        else:
            self._tile.set(fill=None, border=None, bw=0)
        fg = P["ink"] if (self._active or self._hover) else P["ink3"]
        self.itemconfigure(self._ico, image=gfx.icon(self, self._icon, S(22), fg))
        self.itemconfigure(self._txt, fill=fg, font=theme.FONTS["nav_b"] if self._active else theme.FONTS["nav"])
        self.tag_raise(self._ico)
        self.tag_raise(self._txt)
        self.tag_raise(self._bdg)
        self.tag_raise(self._bdg_t)


# --------------------------------------------------------------------------- #
# 电源键（侧栏底部：启动 / 停止监听）
# --------------------------------------------------------------------------- #


class PowerButton(tk.Canvas):
    """一个圆形电源键，颜色就是引擎状态：

      待命   白底灰圈          监听中   墨色底、青色发光图标
      加载中 白底琥珀圈        正在录音 橙色底、白图标
      出错   浅红底红圈
    """

    def __init__(self, master: tk.Misc, command: Callable[[], None] | None = None, *, bg: str | None = None,
                 tooltip: str = "") -> None:
        self._bg = bg or bg_of(master)
        self._d = S(52)
        self._m = S(4)
        super().__init__(master, width=self._d + 2 * self._m, height=self._d + 2 * self._m, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self._command = command
        self._state = "idle"
        self._live = False
        self._hover = False
        self._kbd = False
        self._by_mouse = False
        self._img = self.create_image(self._m, self._m, anchor="nw")
        self._ico = self.create_image(self._m + self._d // 2, self._m + self._d // 2)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._click)
        self.bind("<space>", lambda _e: (self._invoke(), "break")[1])
        self.bind("<Return>", lambda _e: (self._invoke(), "break")[1])
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.tooltip = Tooltip(self, tooltip) if tooltip else None
        self._paint()

    def set_tooltip(self, text: str) -> None:
        if self.tooltip is not None:
            self.tooltip.text = text
        else:
            self.tooltip = Tooltip(self, text)

    def set_state(self, state: str, *, live: bool = False) -> None:
        if (state, live) != (self._state, self._live):
            self._state, self._live = state, live
            self._paint()

    @property
    def state_key(self) -> tuple[str, bool]:
        return self._state, self._live

    def _invoke(self) -> None:
        if self._command:
            self._command()

    def _click(self, _e: tk.Event) -> None:
        self._by_mouse = True
        self._kbd = False
        self.focus_set()
        self._invoke()

    def _focus_in(self, _e: tk.Event) -> None:
        self._kbd = not self._by_mouse
        self._paint()

    def _focus_out(self, _e: tk.Event) -> None:
        self._by_mouse = False
        self._kbd = False
        self._paint()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._paint()

    def _paint(self) -> None:
        if self._live:
            fill, edge, fg = P["live"], None, P["white"]
        elif self._state == "running":
            fill, edge, fg = P["ink"], P["teal_lit"], P["teal_lit"]
        elif self._state == "loading":
            fill, edge, fg = P["raised"], P["warn_lit"], P["warn"]
        elif self._state == "error":
            fill, edge, fg = P["err_bg"], P["err"], P["err"]
        else:
            fill, edge, fg = P["raised"], P["line_strong"], P["ink2"]
        if self._hover and self._state == "idle" and not self._live:
            fill = gfx.mix(fill, P["ink"], 0.04)
        d, bw = self._d, S(2)
        if self._kbd:
            edge, bw = P["teal"], S(3)
        img = gfx.photo(self, ("power", d, fill, edge, bw), lambda: gfx.rrect_rgba(d, d, d / 2, fill, edge, bw))
        self.itemconfigure(self._img, image=img)
        self.itemconfigure(self._ico, image=gfx.icon(self, "power", S(24), fg))


# --------------------------------------------------------------------------- #
# 快捷键录制
# --------------------------------------------------------------------------- #

_TK_MODS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
    "Super_L": "cmd", "Super_R": "cmd", "Win_L": "cmd", "Win_R": "cmd",
}
_TK_SPECIAL = {
    "space": "space", "Return": "enter", "KP_Enter": "enter", "Escape": "esc",
    "Tab": "tab", "BackSpace": "backspace", "Delete": "delete", "Insert": "insert",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Home": "home", "End": "end", "Prior": "page_up", "Next": "page_down",
}


def keysym_to_name(keysym: str) -> str | None:
    """tk 的 keysym → 我们内部的键名。认不出来返回 None。

    认不出来时**返回 None 而不是猜**：猜错会让用户配出一个永远触发不了的热键，
    比直接说"这个键不支持"糟糕得多。
    """
    if keysym in _TK_MODS:
        return _TK_MODS[keysym]
    if keysym in _TK_SPECIAL:
        return _TK_SPECIAL[keysym]
    if len(keysym) == 1 and keysym.isprintable():
        return keysym.lower()
    if keysym.startswith("F") and keysym[1:].isdigit() and 1 <= int(keysym[1:]) <= 24:
        return keysym.lower()
    return None


_MOD_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "cmd": 3}


def _order(names: Any) -> list[str]:
    """修饰键排前面，且按固定顺序——否则显示成 'space+alt+ctrl' 很别扭。"""
    return sorted(names, key=lambda n: (_MOD_ORDER.get(n, 9), n))


class HotkeyCapture(tk.Frame):
    """点一下，然后按下组合键。

    只把**组合键**（至少一个非修饰键）算数：只按 Ctrl 不算，
    否则用户想按 Ctrl+Alt+Space 时会先被 Ctrl 单独触发一次。

    录制中边框变成信号橙、已按下的键帽会真的"按下去"——和运行页按住热键时同一个视觉语言。
    """

    IDLE = "点击这里，然后按下新的组合键"
    WAIT = "请按下组合键 …（Esc 取消）"

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_capture: Callable[[str], None] | None = None,
        on_capture_start: Callable[[], None] | None = None,
        on_capture_end: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, bg=bg_of(master))
        self._on_capture = on_capture
        self._on_start = on_capture_start
        self._on_end = on_capture_end

        self._box = Panel(self, radius=20, fill=P["scr"], border=P["scr_line"], pad=26)
        self._box.pack(fill="x")
        self._box.configure(takefocus=True, cursor="hand2")
        inner = tk.Frame(self._box.body, bg=P["scr"])
        inner.pack(fill="x", pady=S(6))
        self._caps = KeyCaps(inner, "", size="lg", dark=True, bg=P["scr"])
        self._caps.pack()
        self._prompt = tk.Label(inner, text=self.IDLE, bg=P["scr"], fg=P["scr_dim"], font=theme.FONTS["note"])
        self._prompt.pack(pady=(S(18), 0))

        self._down: set[str] = set()
        self._main: str | None = None
        self._capturing = False
        self._spec = ""

        for w in (self._box, self._box.body, inner, self._caps, self._prompt):
            w.bind("<Button-1>", self._focus)
        self._box.bind("<FocusIn>", lambda _e: self._begin())
        self._box.bind("<FocusOut>", lambda _e: self._cancel(silent=True))
        self._box.bind("<KeyPress>", self._on_press)
        self._box.bind("<KeyRelease>", self._on_release)
        self._render()

    # -- 外部接口 --------------------------------------------------------- #

    def set_spec(self, spec: str) -> None:
        self._spec = spec
        self._render()

    @property
    def spec(self) -> str:
        return self._spec

    @property
    def capturing(self) -> bool:
        return self._capturing

    def focus_box(self) -> None:
        self._box.focus_set()

    # -- 内部 ------------------------------------------------------------- #

    def _focus(self, _e: tk.Event) -> None:
        self._box.focus_set()

    def _begin(self) -> None:
        if self._capturing:
            return
        self._capturing = True
        self._down.clear()
        self._main = None
        if self._on_start:
            self._on_start()
        self._render()

    def _cancel(self, *, silent: bool = False) -> None:
        if not self._capturing:
            return
        self._capturing = False
        self._down.clear()
        self._main = None
        if self._on_end:
            self._on_end()
        self._render()
        if not silent:
            events.info("已取消录制热键", kind="hotkey")

    def _on_press(self, e: tk.Event) -> str | None:
        if not self._capturing:
            return None
        if e.keysym == "Escape":
            self._cancel()
            return "break"
        name = keysym_to_name(e.keysym)
        if name is None:
            self._prompt.configure(text=f"不支持这个键：{e.keysym}", fg=P["warn_lit"])
            return "break"
        self._down.add(name)
        if name not in ("ctrl", "alt", "shift", "cmd"):
            self._main = name
        self._render()
        return "break"  # 别让 Tab/空格 触发默认行为

    def _on_release(self, e: tk.Event) -> str | None:
        if not self._capturing:
            return None
        name = keysym_to_name(e.keysym)
        if name is None or self._main is None:
            self._down.discard(name or "")
            self._render()
            return "break"
        if name == self._main:
            # 主键松手时才算数：这样"按住 Ctrl 再按 Space"和
            # "按住 Space 再按 Ctrl"都能得到同一套键
            spec = "+".join(_order(self._down))
            self._down.clear()
            self._main = None
            self._capturing = False
            self._spec = spec
            if self._on_end:
                self._on_end()
            self._render()
            if self._on_capture:
                self._on_capture(spec)
        else:
            self._down.discard(name)
            self._render()
        return "break"

    def _render(self) -> None:
        if self._capturing:
            self._box.set_style(border=P["live"], bw=S(2))
            self._prompt.configure(text=self.WAIT, fg=P["live"])
            if self._down:
                self._caps.set_spec("+".join(_order(self._down)))
                self._caps.set_pressed(True)
            else:
                self._caps.set_spec(self._spec)
                self._caps.set_pressed(False)
            return
        self._box.set_style(border=P["scr_line"], bw=hair())
        self._prompt.configure(text=self.IDLE, fg=P["scr_dim"])
        self._caps.set_spec(self._spec)
        self._caps.set_pressed(False)


# --------------------------------------------------------------------------- #
# 日志面板
# --------------------------------------------------------------------------- #


class LogView(tk.Frame):
    """带级别着色、过滤、暂停、导出的日志面板（深色屏幕）。

    用 Text 而不是 Treeview：一次事件常常是多行（一次命中的完整报告），
    Treeview 一行一条会把内容截断，而排查问题恰恰要看那些被截断的部分。

    每行的级别用一个**抗锯齿的小圆点图**标，而不是 ✓ ⚠ ✗ 这些字符：这几个字符在等宽字体里
    会各自回退到别的字体，宽度不一，消息文字就对不齐；图标宽度固定，列才整齐。
    """

    CAPACITY = 4000

    def __init__(self, master: tk.Misc, *, on_autoscroll: Callable[[bool], None] | None = None, **kw: Any) -> None:
        super().__init__(master, bg=P["scr"], **kw)
        self._events: list[events.Event] = []
        self._min_rank = 0
        self._keyword = ""
        self._paused = False
        self._autoscroll = True
        self._on_autoscroll = on_autoscroll

        font = theme.FONTS["mono_s"]
        cw = measure("0", font)
        x1 = cw * 12 + S(12)  # 时间戳之后：圆点列
        x2 = x1 + S(24)  # 圆点之后：消息列
        self.text = theme.text_widget(
            self, font=font, state="disabled", cursor="arrow", wrap="word", spacing1=S(3), spacing3=S(3),
            tabs=(x1, x2), tabstyle="tabular", padx=S(16), pady=S(12),
        )
        self._sb = Scrollbar(self, command=self.text.yview, bg=P["scr"], dark=True)
        self.text.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y", padx=(0, S(4)), pady=S(8))
        self.text.pack(side="left", fill="both", expand=True)

        for level in events.LEVELS:
            self.text.tag_configure(f"lv_{level}", foreground=theme.LEVEL_COLOR[level])
        self.text.tag_configure("ts", foreground=P["scr_faint"])
        self.text.tag_configure("cont", foreground=P["scr_dim"])
        self.text.tag_configure("row", lmargin2=x2)  # 长消息折行后，续行对齐到消息列
        self._dots: dict[str, tk.PhotoImage] = {}
        d = S(7)
        for level in events.LEVELS:
            col = theme.LEVEL_COLOR[level]
            glow = 0.0 if level in ("debug", "info") else 0.7
            self._dots[level] = gfx.photo(self, ("logdot", d, col, glow),
                                          lambda col=col, glow=glow: gfx.dot_rgba(d, S(3), col, glow))

        self.text.bind("<MouseWheel>", self._on_wheel)

    # -- 数据 ------------------------------------------------------------- #

    def set_filter(self, *, min_level: str | None = None, keyword: str | None = None) -> None:
        if min_level is not None:
            self._min_rank = events.RANK.get(min_level, 0)
        if keyword is not None:
            self._keyword = keyword.strip().lower()
        self.rerender()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def set_autoscroll(self, on: bool) -> None:
        self._autoscroll = on
        if on:
            self.text.see("end")

    @property
    def count(self) -> int:
        return len(self._events)

    @property
    def shown(self) -> int:
        """当前显示的条数（每个事件第一行有一个圆点图标，数它就行）。"""
        return len(self.text.image_names())

    def _passes(self, ev: events.Event) -> bool:
        if ev.rank < self._min_rank:
            return False
        if self._keyword:
            hay = f"{ev.text} {ev.kind} {ev.level}".lower()
            if self._keyword not in hay:
                return False
        return True

    def feed(self, incoming: list[events.Event]) -> int:
        """喂新事件。返回实际插入的条数。"""
        if not incoming:
            return 0
        self._events.extend(incoming)
        if len(self._events) > self.CAPACITY:
            del self._events[: len(self._events) - self.CAPACITY]
        if self._paused:
            return 0
        n = 0
        for ev in incoming:
            if self._passes(ev):
                self._insert(ev)
                n += 1
        if n:
            self._trim()
            if self._autoscroll:
                self.text.see("end")
        return n

    def rerender(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        if self._paused:
            return
        for ev in self._events:
            if self._passes(ev):
                self._insert(ev)
        self._trim()
        if self._autoscroll:
            self.text.see("end")

    def clear(self) -> None:
        self._events.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def export(self, path: str) -> int:
        lines = [
            f"{e.stamp()}.{int((e.ts % 1) * 1000):03d} {e.level.upper():5} [{e.kind}] {e.text}"
            for e in self._events
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return len(lines)

    # -- 渲染 ------------------------------------------------------------- #

    def _insert(self, ev: events.Event) -> None:
        tag = f"lv_{ev.level}"
        t = self.text
        t.configure(state="normal")
        start = t.index("end-1c")
        t.insert("end", f"{ev.clock(millis=True)}\t", ("ts",))
        t.image_create("end", image=self._dots.get(ev.level, self._dots["info"]), align="center")
        t.insert("end", "\t")
        lines = ev.text.splitlines() or [""]
        t.insert("end", lines[0] + "\n", (tag,))
        for extra in lines[1:]:
            t.insert("end", "\t\t" + extra + "\n", ("cont",))
        t.tag_add("row", start, "end-1c")
        t.configure(state="disabled")

    def _trim(self) -> None:
        # Text 里行数超过 CAPACITY 就从头上删，否则常驻几天内存会涨
        total = int(self.text.index("end-1c").split(".")[0])
        if total > self.CAPACITY:
            self.text.configure(state="normal")
            self.text.delete("1.0", f"{total - self.CAPACITY}.0")
            self.text.configure(state="disabled")

    def _on_wheel(self, _e: tk.Event) -> None:
        # 手动滚动过就说明用户想往回看，别再用自动滚动把他拽到底部
        self.after(400, self._check_scroll)

    def _check_scroll(self) -> None:
        try:
            if self.text.yview()[1] < 0.995 and self._autoscroll:
                self._autoscroll = False
                if self._on_autoscroll:
                    self._on_autoscroll(False)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def open_folder(path: str) -> None:
    import os
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path])  # noqa: S603,S607
    except Exception as e:  # noqa: BLE001
        events.error(f"打不开 {path}：{e}", kind="ui")


def open_url(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url, new=2)
        events.info(f"已在浏览器中打开 {url}", kind="ui")
    except Exception as e:  # noqa: BLE001
        events.error(f"打不开 {url}：{e}", kind="ui")


def copy_to_clipboard(root: tk.Misc, text: str) -> None:
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        events.ok(f"已复制到剪贴板（{len(text)} 字符）", kind="ui")
    except tk.TclError as e:  # pragma: no cover
        events.error(f"复制失败：{e}", kind="ui")


def human_time(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


__all__ = [
    "Button", "Callout", "Card", "DotMeter", "Flow", "Field", "HotkeyCapture", "IconButton", "KeyCaps", "LogView",
    "NavItem", "Panel", "PowerButton", "STATE_TONE", "ScrollFrame", "Scrollbar", "Segmented", "Select",
    "Slider", "StatusPill", "Stepper", "Switch", "Tooltip", "bg_of", "check", "copy_to_clipboard", "hint",
    "hline", "hotkey_text", "human_time", "key_labels", "keysym_to_name", "open_folder", "open_url",
]
