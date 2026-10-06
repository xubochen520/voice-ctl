"""UI 主题：设计令牌、字体、DPI 缩放。

视觉语言（一句话）：这是一台「按住说话」的设备的控制面——
    **浅色机壳**承载控件，**深色屏幕**承载机器输出（实时状态、日志、识别报告）。

    * 机壳是带一点青灰的雾色，不是纯白也不是奶油色；墨色 `ink` 是主按钮与开关的颜色。
    * 信号橙 `live` **只**表示「正在录音」——整个界面里它一出现，就是麦克风开着。
    * 青 `teal` 表示就绪与焦点。
    * 圆角按角色区分（屏幕 22 / 面板 16 / 输入框 10 / 药丸按钮 / 键帽 8），不是一刀切。
    * 发丝线代替阴影——Tk 没有真正的模糊，假阴影比没有更廉价。

为什么所有尺寸都过 S()：
    不声明 DPI 感知的话 Windows 会把窗口位图拉伸，125%/150% 缩放下整个界面是糊的——
    这是"一看就是外行做的"的头号特征。声明之后窗口是真像素，所有像素值就得自己缩放。

为什么字号用像素（负数）而不是磅：
    Tk 的磅换算依赖 `tk scaling`，而 S() 依赖另一套 DPI 计算，两者在非整数缩放下会差一两个像素。
    负数字号 = 物理像素，和 S() 同源，布局算得准。
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from typing import Any

from . import gfx

# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #

PALETTE: dict[str, str] = {
    # --- 机壳（浅）----------------------------------------------------------
    "chassis": "#EAEEED",      # 窗口底色
    "panel": "#F8FAF9",        # 面板
    "raised": "#FFFFFF",       # 输入框、选中的导航块、次要按钮
    "well": "#DDE3E2",         # 凹陷：滑块轨道、分段控件底
    "line": "#D4DAD9",         # 发丝线
    "line_strong": "#B9C2C1",  # 输入框/次要按钮描边
    "ink": "#151A1C",          # 正文、主按钮
    "ink2": "#454F52",         # 次级文字
    "ink3": "#5F6B6D",         # 说明文字（在 chassis 上 ≥ 4.5:1）
    "ink4": "#98A3A4",         # 占位符、停用（不承载必要信息）
    # --- 屏幕（深）----------------------------------------------------------
    "scr": "#0F1416",
    "scr2": "#161D20",
    "scr3": "#1F292C",
    "scr_line": "#283437",
    "scr_ink": "#E8F0F0",
    "scr_dim": "#8D9DA0",
    "scr_faint": "#5E6D70",
    "dot_off": "#1B2427",
    # --- 信号 --------------------------------------------------------------
    "live": "#FF5B1F",         # 正在录音（屏幕上）
    "live_ink": "#C43C09",     # 正在录音（浅底上的文字）
    "teal": "#087376",         # 就绪 / 焦点（浅底；在 chassis/panel/info_bg 上都 ≥ 4.5:1）
    "teal_lit": "#36E2C2",     # 就绪（屏幕上）
    "ok": "#0F7A52",
    "ok_lit": "#3DDC97",
    "warn": "#98590A",
    "warn_lit": "#FFB454",
    "err": "#C42E2E",
    "err_lit": "#FF6B6B",
    # --- 底色 tint（提示条）------------------------------------------------
    "ok_bg": "#E1F2EA",
    "warn_bg": "#FAEFDB",
    "err_bg": "#FAE5E3",
    "info_bg": "#DDEFEF",
    "white": "#FFFFFF",
}

# 日志级别 → 屏幕上的颜色。和 events.GLYPH 对应。
LEVEL_COLOR = {
    "debug": PALETTE["scr_faint"],
    "info": PALETTE["scr_ink"],
    "ok": PALETTE["ok_lit"],
    "warn": PALETTE["warn_lit"],
    "error": PALETTE["err_lit"],
}

_UI = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimHei", "TkDefaultFont")
_UI_LIGHT = ("Microsoft YaHei UI Light", *_UI)
_DISPLAY = ("Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Semibold", "Segoe UI", *_UI)
_DISPLAY_LIGHT = ("Bahnschrift Light", "Bahnschrift SemiLight", "Segoe UI Light", "Segoe UI", *_UI)
_MONO = ("Cascadia Mono", "Consolas", "Sarasa Mono SC", "Courier New", "TkFixedFont")

SCALE = 1.0
"""DPI 缩放系数。所有像素尺寸都过 S()。"""

FONTS: dict[str, tuple] = {}

TITLEBAR_THEMED = False
"""最近一次 apply_titlebar 的结果。出问题时第一个要看的诊断位。"""


def S(n: float) -> int:
    """按 DPI 缩放一个像素值。"""
    return int(round(n * SCALE))


def hair() -> int:
    """发丝线的物理宽度。1.5× 时取 1：整数像素才锐利，1.5px 的线两边都是糊的。"""
    return 1 if SCALE < 2.0 else 2


def tint(bg: str, amount: float, toward: str | None = None) -> str:
    """把 bg 往 toward（默认墨色）拽一点，悬停/按下的底色都是这么来的。"""
    return gfx.mix(bg, toward or PALETTE["ink"], amount)


def enable_dpi_awareness() -> str:
    """在创建 Tk 之前调用。必须在 Tk() 之前——之后调用无效。"""
    if sys.platform != "win32":
        return "n/a"
    try:
        import ctypes

        try:
            # 2 = PROCESS_PER_MONITOR_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            return "per-monitor"
        except Exception:  # noqa: BLE001 - Win8.1 以下没有 shcore
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
            return "system"
    except Exception:  # noqa: BLE001
        return "none"


def reduced_motion() -> bool:
    """系统是否关了动画（设置 → 辅助功能 → 视觉效果 → 动画效果）。

    关了就别再有任何过渡：开关直接跳到位，电平条不拖尾。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        flag = ctypes.c_int(1)
        # SPI_GETCLIENTAREAANIMATION = 0x1042
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(flag), 0):  # type: ignore[attr-defined]
            return not flag.value
    except Exception:  # noqa: BLE001
        pass
    return False


def _pick(candidates: tuple[str, ...], available: set[str], fallback: str) -> str:
    for name in candidates:
        if name in available:
            return name
    return fallback


def _colorref(hex_color: str) -> int:
    """'#rrggbb' → COLORREF（0x00BBGGRR，注意是反的）。"""
    r, g, b = gfx.to_rgb(hex_color)
    return (b << 16) | (g << 8) | r


def apply_titlebar(root: tk.Misc) -> bool:
    """让标题栏和机壳同色，而不是顶着系统默认的一条白/黑带。

    分两步，缺一不可（这是在深色版上踩出来的）：

      1. `DWMWA_USE_IMMERSIVE_DARK_MODE`（属性 20，旧预览版是 19）设成 0——让 DWM 按浅色画边框。
         **但它返回 S_OK 不代表真的生效**：打包成 exe 后实测它照样返回 0，边框却没变。
      2. `DWMWA_CAPTION_COLOR`(35) / `DWMWA_TEXT_COLOR`(36) / `DWMWA_BORDER_COLOR`(34)——
         Win11 起可以直接指定颜色，绕开主题推断，结果确定。设不上（Win10）就只吃第 1 步。

    最后那发 `SetWindowPos(SWP_FRAMECHANGED)` 也不是多余的：DWM 有时收下了属性
    却不重画边框，表现是"设了但看不出来"。
    """
    global TITLEBAR_THEMED
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        root.update_idletasks()
        u32 = ctypes.windll.user32  # type: ignore[attr-defined]
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        own = root.winfo_id()
        parent = u32.GetParent(own)
        targets = [own] if not parent else [parent, own]

        def put(hwnd: int, attr: int, ref: Any) -> bool:  # noqa: ANN401
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref)) == 0

        flag = ctypes.c_int(0)
        caption = ctypes.c_uint32(_colorref(PALETTE["chassis"]))
        text = ctypes.c_uint32(_colorref(PALETTE["ink"]))
        border = ctypes.c_uint32(_colorref(PALETTE["line"]))

        done = False
        for hwnd in targets:
            for attr in (20, 19):
                if put(hwnd, attr, flag):
                    done = True
            put(hwnd, 35, caption)
            put(hwnd, 36, text)
            put(hwnd, 34, border)
            # SWP_NOSIZE|SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED
            u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        TITLEBAR_THEMED = done
        return done
    except Exception:  # noqa: BLE001
        return False


def round_popup_corners(widget: tk.Misc, *, border: str | None = None) -> None:
    """让下拉/提示这类无边框弹窗在 Win11 上有系统级圆角（Win10 上无效，保持直角）。

    无边框的 Tk 弹窗没法做透明圆角：Tk 只有"整窗按色键镂空"，圆角边缘会带一圈杂色。
    DWM 的 `DWMWA_WINDOW_CORNER_PREFERENCE`(33)=DWMWCP_ROUND(2) 才是干净的做法。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        widget.update_idletasks()
        u32 = ctypes.windll.user32  # type: ignore[attr-defined]
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        own = widget.winfo_id()
        hwnd = u32.GetParent(own) or own
        pref = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        if border:
            col = ctypes.c_uint32(_colorref(border))
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), ctypes.sizeof(col))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #

# 角色 → (字体族候选, 100% 缩放下的像素字号, 字重)
_ROLES: dict[str, tuple[tuple[str, ...], int, str]] = {
    "hero": (_UI_LIGHT, 40, "normal"),
    "title": (_UI, 22, "bold"),
    "heading": (_UI, 15, "bold"),
    "body": (_UI, 14, "normal"),
    "body_b": (_UI, 14, "bold"),
    "lead": (_UI, 17, "normal"),
    "note": (_UI, 12, "normal"),
    "note_b": (_UI, 12, "bold"),
    "caption": (_UI, 11, "normal"),
    "button": (_UI, 13, "normal"),
    "button_b": (_UI, 13, "bold"),
    "nav": (_UI, 12, "normal"),
    "nav_b": (_UI, 12, "bold"),
    "mono": (_MONO, 12, "normal"),
    "mono_s": (_MONO, 11, "normal"),
    "digits": (_DISPLAY, 15, "normal"),
    "digits_s": (_DISPLAY, 12, "normal"),
    "digits_m": (_DISPLAY_LIGHT, 24, "normal"),
    "digits_l": (_DISPLAY_LIGHT, 40, "normal"),
    "key": (_DISPLAY, 15, "normal"),
    "key_l": (_DISPLAY, 21, "normal"),
    "key_cjk": (_UI, 14, "bold"),
    "key_cjk_l": (_UI, 19, "bold"),
    "brand": (_DISPLAY, 20, "normal"),
    "brand_s": (_DISPLAY, 14, "normal"),
    "brand_l": (_DISPLAY, 32, "normal"),
}


def init(root: tk.Misc) -> dict[str, tuple]:
    """算出 DPI 缩放、装好字体表、设好默认选项。返回字体表。"""
    global SCALE

    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:  # noqa: BLE001
        dpi = 96.0
    SCALE = max(1.0, min(3.0, dpi / 96.0))
    try:
        # 磅制字体（messagebox 等遗留控件）靠它换算
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:  # noqa: BLE001
        pass

    available = set(tkfont.families(root))
    # 原地填充而不是重新赋值：别的模块 `from .theme import FONTS` 拿到的是同一个字典对象，
    # 重新赋值会让它们永远看着一个空表（KeyError: 'hero'）。
    FONTS.clear()
    for role, (cands, px, weight) in _ROLES.items():
        fam = _pick(cands, available, cands[-1])
        FONTS[role] = (fam, -S(px)) if weight == "normal" else (fam, -S(px), weight)

    root.configure(bg=PALETTE["chassis"])
    # 少数没被我们自己接管的 Tk 控件（弹出的原生对话框里的 Text 等）的兜底默认值
    root.option_add("*Font", FONTS["body"])
    root.option_add("*Background", PALETTE["chassis"])
    root.option_add("*Foreground", PALETTE["ink"])
    root.option_add("*selectBackground", gfx.mix(PALETTE["teal"], PALETTE["white"], 0.72))
    root.option_add("*selectForeground", PALETTE["ink"])
    return FONTS


_MEASURE: dict[tuple, tkfont.Font] = {}


def _font_for(font: tuple) -> tkfont.Font:
    f = _MEASURE.get(font)
    if f is None:
        f = _MEASURE[font] = tkfont.Font(font=font)
    return f


def _with_font(font: tuple, fn: Any) -> Any:  # noqa: ANN401
    """用缓存的 Font 对象算东西；Font 属于创建它的 Tcl 解释器，解释器没了（测试里反复建销根窗口、
    或者用户关了窗口又开一个）它就是死的——撞到 TclError 就重建一个再来。
    不能每次都先"探活"：这是每个控件、每次折行都会走的热路径。"""
    try:
        return fn(_font_for(font))
    except tk.TclError:
        _MEASURE.pop(font, None)
        return fn(_font_for(font))


def measure(text: str, font: tuple) -> int:
    """文字的像素宽度。按字体缓存 Font 对象——每次新建一个很慢。"""
    return int(_with_font(font, lambda f: f.measure(text)))


def line_height(font: tuple) -> int:
    return int(_with_font(font, lambda f: f.metrics("linespace")))


def text_widget(parent: tk.Misc, **kw: Any) -> tk.Text:
    """统一风格的 Text（屏幕那一类）。Text 不是 ttk 的，只能手工配。"""
    P = PALETTE
    opts: dict[str, Any] = dict(
        bg=P["scr"],
        fg=P["scr_ink"],
        insertbackground=P["scr_ink"],
        selectbackground=gfx.mix(P["scr"], P["teal_lit"], 0.32),
        selectforeground=P["scr_ink"],
        relief="flat",
        bd=0,
        highlightthickness=0,
        padx=S(14),
        pady=S(12),
        wrap="word",
        font=FONTS["mono"],
        spacing1=S(2),
        spacing3=S(2),
    )
    opts.update(kw)
    return tk.Text(parent, **opts)
