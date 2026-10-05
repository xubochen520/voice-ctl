"""UI 主题：配色、字体、DPI 缩放。

为什么用 `clam` 而不是 Windows 默认的 `vista` 主题：
    vista 主题下 `background` 对 TNotebook / TButton / TFrame 基本无效，
    想做成深色只能一个个去 hack 元素布局，而且换个 Windows 版本就变样。
    clam 是纯 Tk 绘制的，每个元素都能上色，跨版本表现一致。

DPI 那一坨不是可选项：不声明 DPI 感知的话，Windows 会把窗口**位图拉伸**，
在 125%/150% 缩放的屏幕上整个界面都是糊的——这是"一看就是外行做的"的
头号特征。
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Any

# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #

PALETTE: dict[str, str] = {
    "bg": "#0f1115",
    "surface": "#171a21",
    "surface2": "#1e222b",
    "surface3": "#262b36",
    "border": "#2b3140",
    "border_soft": "#21252f",
    "text": "#e8ebf2",
    "muted": "#8d95a6",
    "faint": "#5c6478",
    "accent": "#5b8def",
    "accent_hover": "#6f9bf5",
    "accent_dim": "#2c3d63",
    "ok": "#3ddc97",
    "warn": "#ffb454",
    "error": "#ff6b6b",
    "info": "#8d95a6",
    "debug": "#5c6478",
    "white": "#ffffff",
}

# 日志级别 → 颜色。和 events.GLYPH 对应。
LEVEL_COLOR = {
    "debug": PALETTE["debug"],
    "info": PALETTE["text"],
    "ok": PALETTE["ok"],
    "warn": PALETTE["warn"],
    "error": PALETTE["error"],
}

_UI_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimHei", "TkDefaultFont")
_MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Sarasa Mono SC", "Courier New", "TkFixedFont")

SCALE = 1.0
"""DPI 缩放系数。所有像素尺寸都过 S()。"""

FONTS: dict[str, tuple] = {}

DARK_TITLEBAR = False
"""最近一次 apply_dark_titlebar 的结果。出问题时第一个要看的诊断位。"""


def S(n: float) -> int:
    """按 DPI 缩放一个像素值。"""
    return int(round(n * SCALE))


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


def _pick(candidates: tuple[str, ...], available: set[str], fallback: str) -> str:
    for name in candidates:
        if name in available:
            return name
    return fallback


def _colorref(hex_color: str) -> int:
    """'#rrggbb' → COLORREF（0x00BBGGRR，注意是反的）。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def apply_dark_titlebar(root: tk.Misc) -> bool:
    """把标题栏也变深色。

    不做的话，深色界面顶着一个亮白标题栏，观感非常割裂。

    分两步，缺一不可：

      1. `DWMWA_USE_IMMERSIVE_DARK_MODE`（属性 20，旧预览版是 19）——
         让 DWM 按深色主题画边框。**但它返回 S_OK 不代表真的变深了**：
         实测打包成 exe 后它照样返回 0，边框却依然是浅色（同样的代码在
         源码运行时是好的）。所以不能只靠它。

      2. `DWMWA_CAPTION_COLOR`(35) / `DWMWA_TEXT_COLOR`(36) /
         `DWMWA_BORDER_COLOR`(34)——Win11 起可以直接指定颜色，绕开
         主题推断，结果确定。设不上（Win10）就只吃第 1 步的效果。

    最后那发 `SetWindowPos(SWP_FRAMECHANGED)` 也不是多余的：DWM 有时
    收下了属性却不重画边框，表现是"设了但看不出来"。
    """
    global DARK_TITLEBAR
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

        def setattr(hwnd: int, attr: int, ref: Any) -> bool:  # noqa: ANN401
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref)) == 0

        flag = ctypes.c_int(1)
        caption = ctypes.c_uint32(_colorref(PALETTE["surface"]))
        text = ctypes.c_uint32(_colorref(PALETTE["text"]))
        border = ctypes.c_uint32(_colorref(PALETTE["border"]))

        done = False
        for hwnd in targets:
            for attr in (20, 19):
                if setattr(hwnd, attr, flag):
                    done = True
            # 35/36/34 只在 Win11 上有；设不上不影响前面那步
            setattr(hwnd, 35, caption)
            setattr(hwnd, 36, text)
            setattr(hwnd, 34, border)
            # SWP_NOSIZE|SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED
            u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        DARK_TITLEBAR = done
        return done
    except Exception:  # noqa: BLE001
        return False


def init(root: tk.Tk) -> dict[str, tuple]:
    """装好字体与 ttk 样式。返回字体表。"""
    global SCALE, FONTS

    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:  # noqa: BLE001
        dpi = 96.0
    SCALE = max(1.0, min(3.0, dpi / 96.0))
    try:
        # tk 的 scaling 单位是「点/像素」，fonts 用点号时靠它换算
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:  # noqa: BLE001
        pass

    available = set(tkfont.families(root))
    ui = _pick(_UI_CANDIDATES, available, "TkDefaultFont")
    mono = _pick(_MONO_CANDIDATES, available, "TkFixedFont")

    FONTS = {
        "title": (ui, 15, "bold"),
        "subtitle": (ui, 11, "bold"),
        "body": (ui, 10),
        "small": (ui, 9),
        "tiny": (ui, 8),
        "bold": (ui, 10, "bold"),
        "nav": (ui, 10),
        "mono": (mono, 9),
        "mono_small": (mono, 8),
    }

    root.configure(bg=PALETTE["bg"])
    _style(root)
    return FONTS


def _style(root: tk.Tk) -> None:
    P = PALETTE
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:  # pragma: no cover
        pass

    # --- 容器 -------------------------------------------------------------
    st.configure(".", background=P["bg"], foreground=P["text"], font=FONTS["body"])
    st.configure("TFrame", background=P["bg"])
    st.configure("Surface.TFrame", background=P["surface"])
    st.configure("Surface2.TFrame", background=P["surface2"])
    st.configure("Card.TFrame", background=P["surface"], relief="flat")

    st.configure("TLabel", background=P["bg"], foreground=P["text"], font=FONTS["body"])
    for name, bg, fg, font in (
        ("Surface.TLabel", P["surface"], P["text"], FONTS["body"]),
        ("SurfaceMuted.TLabel", P["surface"], P["muted"], FONTS["small"]),
        ("Muted.TLabel", P["bg"], P["muted"], FONTS["small"]),
        ("Faint.TLabel", P["bg"], P["faint"], FONTS["tiny"]),
        ("Title.TLabel", P["bg"], P["text"], FONTS["title"]),
        ("Subtitle.TLabel", P["bg"], P["text"], FONTS["subtitle"]),
        ("Section.TLabel", P["bg"], P["muted"], FONTS["small"]),
        ("Key.TLabel", P["surface2"], P["text"], FONTS["mono"]),
        ("Mono.TLabel", P["bg"], P["text"], FONTS["mono"]),
    ):
        st.configure(name, background=bg, foreground=fg, font=font)

    st.configure("TSeparator", background=P["border"])
    st.configure("Thin.TSeparator", background=P["border_soft"])

    # --- 按钮 -------------------------------------------------------------
    # clam 的按钮默认有立体边框，把三个颜色都设成背景色才真正变平
    st.configure(
        "TButton",
        background=P["surface2"],
        foreground=P["text"],
        bordercolor=P["border"],
        lightcolor=P["surface2"],
        darkcolor=P["surface2"],
        focuscolor=P["surface2"],
        relief="flat",
        padding=(S(12), S(6)),
        font=FONTS["small"],
    )
    st.map(
        "TButton",
        background=[("pressed", P["surface3"]), ("active", P["surface3"]), ("disabled", P["surface"])],
        foreground=[("disabled", P["faint"])],
        bordercolor=[("active", P["border"])],
    )

    st.configure(
        "Primary.TButton",
        background=P["accent"],
        foreground=P["white"],
        bordercolor=P["accent"],
        lightcolor=P["accent"],
        darkcolor=P["accent"],
        focuscolor=P["accent"],
        font=FONTS["bold"],
        padding=(S(16), S(7)),
    )
    st.map(
        "Primary.TButton",
        background=[("pressed", P["accent_dim"]), ("active", P["accent_hover"]), ("disabled", P["surface2"])],
        foreground=[("disabled", P["faint"])],
    )

    st.configure(
        "Ghost.TButton",
        background=P["bg"],
        foreground=P["muted"],
        bordercolor=P["bg"],
        lightcolor=P["bg"],
        darkcolor=P["bg"],
        focuscolor=P["bg"],
        font=FONTS["small"],
        padding=(S(8), S(4)),
    )
    st.map(
        "Ghost.TButton",
        background=[("active", P["surface2"])],
        foreground=[("active", P["text"])],
        bordercolor=[("active", P["bg"])],
    )

    # --- 输入 -------------------------------------------------------------
    for cls in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(
            cls,
            fieldbackground=P["surface2"],
            background=P["surface2"],
            foreground=P["text"],
            bordercolor=P["border"],
            lightcolor=P["border"],
            darkcolor=P["border"],
            arrowcolor=P["muted"],
            insertcolor=P["text"],
            padding=(S(6), S(4)),
            relief="flat",
        )
        st.map(
            cls,
            fieldbackground=[("disabled", P["surface"]), ("readonly", P["surface2"])],
            foreground=[("disabled", P["faint"])],
            bordercolor=[("focus", P["accent"])],
        )

    # 下拉列表是独立的 Tk Listbox，只能走 option 数据库
    root.option_add("*TCombobox*Listbox.background", P["surface2"])
    root.option_add("*TCombobox*Listbox.foreground", P["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", P["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", P["white"])
    root.option_add("*TCombobox*Listbox.borderWidth", "0")

    # --- 勾选 / 滑块 ------------------------------------------------------
    st.configure(
        "TCheckbutton",
        background=P["bg"],
        foreground=P["text"],
        focuscolor=P["bg"],
        indicatorcolor=P["surface2"],
        indicatorrelief="flat",
        bordercolor=P["border"],
        font=FONTS["small"],
        padding=(0, S(2)),
    )
    st.map(
        "TCheckbutton",
        background=[("active", P["bg"])],
        indicatorcolor=[("selected", P["accent"]), ("pressed", P["accent_dim"])],
        foreground=[("disabled", P["faint"])],
    )
    st.configure("Surface.TCheckbutton", background=P["surface"])
    st.map("Surface.TCheckbutton", background=[("active", P["surface"])])

    st.configure(
        "TScale",
        background=P["bg"],
        troughcolor=P["surface2"],
        bordercolor=P["bg"],
        lightcolor=P["accent"],
        darkcolor=P["accent"],
    )

    st.configure(
        "TProgressbar",
        background=P["accent"],
        troughcolor=P["surface2"],
        bordercolor=P["surface2"],
        lightcolor=P["accent"],
        darkcolor=P["accent"],
        thickness=S(4),
    )

    # --- 树 / 列表 --------------------------------------------------------
    st.configure(
        "Treeview",
        background=P["surface"],
        fieldbackground=P["surface"],
        foreground=P["text"],
        bordercolor=P["border"],
        lightcolor=P["border"],
        darkcolor=P["border"],
        rowheight=S(24),
        font=FONTS["small"],
        relief="flat",
    )
    st.map(
        "Treeview",
        background=[("selected", P["accent_dim"])],
        foreground=[("selected", P["text"])],
    )
    st.configure(
        "Treeview.Heading",
        background=P["surface2"],
        foreground=P["muted"],
        bordercolor=P["border"],
        lightcolor=P["surface2"],
        darkcolor=P["surface2"],
        relief="flat",
        font=FONTS["small"],
        padding=(S(6), S(4)),
    )
    st.map("Treeview.Heading", background=[("active", P["surface3"])])

    # --- 滚动条 -----------------------------------------------------------
    st.configure(
        "Vertical.TScrollbar",
        background=P["surface3"],
        troughcolor=P["bg"],
        bordercolor=P["bg"],
        arrowcolor=P["muted"],
        lightcolor=P["surface3"],
        darkcolor=P["surface3"],
        relief="flat",
        arrowsize=S(12),
        width=S(10),
    )
    st.map(
        "Vertical.TScrollbar",
        background=[("active", P["faint"]), ("pressed", P["accent"])],
    )
    st.configure(
        "Horizontal.TScrollbar",
        background=P["surface3"],
        troughcolor=P["bg"],
        bordercolor=P["bg"],
        arrowcolor=P["muted"],
        lightcolor=P["surface3"],
        darkcolor=P["surface3"],
        relief="flat",
        arrowsize=S(12),
    )
    st.map(
        "Horizontal.TScrollbar",
        background=[("active", P["faint"]), ("pressed", P["accent"])],
    )


def text_widget(parent: tk.Misc, **kw) -> tk.Text:
    """统一风格的 Text。Text 不是 ttk 的，只能手工配。"""
    P = PALETTE
    opts = dict(
        bg=P["surface"],
        fg=P["text"],
        insertbackground=P["text"],
        selectbackground=P["accent_dim"],
        selectforeground=P["text"],
        relief="flat",
        highlightthickness=1,
        highlightbackground=P["border"],
        highlightcolor=P["accent"],
        padx=S(8),
        pady=S(6),
        wrap="word",
        font=FONTS["body"],
    )
    opts.update(kw)
    return tk.Text(parent, **opts)
