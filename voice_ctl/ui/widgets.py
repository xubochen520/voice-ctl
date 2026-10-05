"""可复用控件。

只放真正被两个以上页面用到的东西，避免这里变成杂物间。
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any

from .. import events
from . import theme
from .theme import PALETTE as P
from .theme import S

# --------------------------------------------------------------------------- #
# 侧栏导航项
# --------------------------------------------------------------------------- #


class NavItem(tk.Frame):
    """左侧导航的一行。选中时左侧亮一条强调色竖条。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        *,
        glyph: str = "",
        command: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, bg=P["surface"], cursor="hand2")
        self._command = command
        self._active = False

        self._bar = tk.Frame(self, bg=P["surface"], width=S(3))
        self._bar.pack(side="left", fill="y")

        inner = tk.Frame(self, bg=P["surface"])
        inner.pack(side="left", fill="both", expand=True, padx=(S(10), S(8)), pady=S(9))

        self._glyph = tk.Label(
            inner, text=glyph, bg=P["surface"], fg=P["muted"], font=theme.FONTS["body"]
        )
        if glyph:
            self._glyph.pack(side="left", padx=(0, S(8)))
        self._label = tk.Label(
            inner, text=text, bg=P["surface"], fg=P["muted"], font=theme.FONTS["nav"], anchor="w"
        )
        self._label.pack(side="left", fill="x", expand=True)

        for w in (self, inner, self._glyph, self._label):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

    def _click(self, _e: tk.Event) -> None:
        if self._command:
            self._command()

    def _enter(self, _e: tk.Event) -> None:
        if not self._active:
            self._paint(P["surface2"], P["text"])

    def _leave(self, _e: tk.Event) -> None:
        if not self._active:
            self._paint(P["surface"], P["muted"])

    def _paint(self, bg: str, fg: str) -> None:
        self.configure(bg=bg)
        self._bar.configure(bg=bg)
        for child in self.winfo_children():
            if child is self._bar:
                continue
            child.configure(bg=bg)
            for grand in child.winfo_children():
                grand.configure(bg=bg)
        self._label.configure(fg=fg)
        # 选中态下图标用强调色，让"当前在哪一页"一眼可见
        self._glyph.configure(fg=P["accent"] if fg == P["text"] else fg)

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._paint(P["surface2"], P["text"])
            self._bar.configure(bg=P["accent"])
            self._label.configure(font=theme.FONTS["bold"])
        else:
            self._paint(P["surface"], P["muted"])
            self._bar.configure(bg=P["surface"])
            self._label.configure(font=theme.FONTS["nav"])

    def set_text(self, text: str) -> None:
        """改文字。相同就什么都不做——顶栏每 120ms 刷一次，无脑 configure
        会让 Tk 反复重排这一个控件，白白占 CPU。"""
        if self._label.cget("text") != text:
            self._label.configure(text=text)


# --------------------------------------------------------------------------- #
# 卡片 / 分隔
# --------------------------------------------------------------------------- #


class Card(tk.Frame):
    """带 1px 边框的容器。`body` 是内容区。"""

    def __init__(self, master: tk.Misc, *, title: str = "", pad: int = 14, **kw: Any) -> None:
        super().__init__(
            master,
            bg=P["surface"],
            highlightthickness=1,
            highlightbackground=P["border"],
            highlightcolor=P["border"],
            **kw,
        )
        self.body = tk.Frame(self, bg=P["surface"])
        self.body.pack(fill="both", expand=True, padx=S(pad), pady=S(pad))
        if title:
            tk.Label(
                self.body,
                text=title,
                bg=P["surface"],
                fg=P["muted"],
                font=theme.FONTS["small"],
                anchor="w",
            ).pack(fill="x", pady=(0, S(10)))


def hline(master: tk.Misc, *, soft: bool = False) -> tk.Frame:
    f = tk.Frame(master, bg=P["border_soft"] if soft else P["border"], height=1)
    f.pack(fill="x")
    return f


def hint(master: tk.Misc, text: str, *, bg: str | None = None) -> tk.Label:
    """说明文字。

    换行宽度**跟着实际宽度走**，不写死。写死的话，窄面板里文字会被右边
    裁掉（用户看不到后半句），宽面板里又会在半路莫名其妙折行。说明文字
    正是最需要读完整的那部分。
    """
    lbl = tk.Label(
        master,
        text=text,
        bg=bg or P["surface"],
        fg=P["faint"],
        font=theme.FONTS["tiny"],
        anchor="w",
        justify="left",
        wraplength=S(560),
    )

    def _fit(e: tk.Event) -> None:
        want = max(S(140), e.width - S(4))
        if abs(int(lbl.cget("wraplength")) - want) > S(8):
            lbl.configure(wraplength=want)

    lbl.bind("<Configure>", _fit)
    return lbl


# --------------------------------------------------------------------------- #
# 状态点
# --------------------------------------------------------------------------- #


class StatusPill(tk.Frame):
    """一个色点 + 一行字，用于顶栏状态。"""

    def __init__(self, master: tk.Misc, text: str = "", *, color: str | None = None) -> None:
        super().__init__(master, bg=P["bg"])
        self._dot = tk.Label(
            self, text="●", bg=P["bg"], fg=color or P["faint"], font=theme.FONTS["small"]
        )
        self._dot.pack(side="left", padx=(0, S(5)))
        self._text = tk.Label(
            self, text=text, bg=P["bg"], fg=P["muted"], font=theme.FONTS["small"]
        )
        self._text.pack(side="left")

    def set(self, text: str, color: str | None = None) -> None:
        self._text.configure(text=text)
        if color:
            self._dot.configure(fg=color)


# --------------------------------------------------------------------------- #
# 键位胶囊
# --------------------------------------------------------------------------- #


def key_pills(master: tk.Misc, spec: str, *, bg: str | None = None, big: bool = False) -> tk.Frame:
    """把 '<ctrl>+<alt>+space' 画成一排按键胶囊。"""
    from ..hotkey import _canonical  # noqa: PLC2701 - 复用同一套键名归一

    bg = bg or P["surface"]
    frame = tk.Frame(master, bg=bg)
    parts = [p for p in spec.split("+") if p.strip()]
    label = {
        "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win", "space": "空格",
        "enter": "Enter", "esc": "Esc", "tab": "Tab",
    }
    font = theme.FONTS["bold"] if big else theme.FONTS["mono"]
    for i, raw in enumerate(parts):
        name = _canonical(raw)
        text = label.get(name, name.upper() if len(name) <= 3 else name.capitalize())
        cap = tk.Label(
            frame,
            text=f" {text} ",
            bg=P["surface2"],
            fg=P["text"],
            font=font,
            padx=S(7),
            pady=S(3),
            highlightthickness=1,
            highlightbackground=P["border"],
        )
        cap.pack(side="left")
        if i < len(parts) - 1:
            tk.Label(frame, text="+", bg=bg, fg=P["faint"], font=theme.FONTS["tiny"]).pack(
                side="left", padx=S(4)
            )
    return frame


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


class HotkeyCapture(tk.Frame):
    """点一下，然后按下组合键。

    只把**组合键**（至少一个非修饰键）算数：只按 Ctrl 不算，
    否则用户想按 Ctrl+Alt+Space 时会先被 Ctrl 单独触发一次。
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
        super().__init__(master, bg=P["bg"])
        self._on_capture = on_capture
        self._on_start = on_capture_start
        self._on_end = on_capture_end

        self._box = tk.Frame(
            self,
            bg=P["surface"],
            highlightthickness=2,
            highlightbackground=P["border"],
            highlightcolor=P["accent"],
            cursor="hand2",
            takefocus=True,
        )
        self._box.pack(fill="x")
        self._inner = tk.Frame(self._box, bg=P["surface"])
        self._inner.pack(fill="x", padx=S(16), pady=S(16))

        self._prompt = tk.Label(
            self._inner, text=self.IDLE, bg=P["surface"], fg=P["muted"], font=theme.FONTS["small"]
        )
        self._prompt.pack()

        self._down: set[str] = set()
        self._main: str | None = None
        self._capturing = False
        self._spec = ""

        self._box.bind("<Button-1>", self._focus)
        self._inner.bind("<Button-1>", self._focus)
        self._prompt.bind("<Button-1>", self._focus)
        self._box.bind("<FocusIn>", lambda _e: self._begin())
        self._box.bind("<FocusOut>", lambda _e: self._cancel(silent=True))
        self._box.bind("<KeyPress>", self._on_press)
        self._box.bind("<KeyRelease>", self._on_release)

    # -- 外部接口 --------------------------------------------------------- #

    def set_spec(self, spec: str) -> None:
        self._spec = spec
        self._render()

    @property
    def spec(self) -> str:
        return self._spec

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
            self._prompt.configure(text=f"不支持这个键：{e.keysym}", fg=P["warn"])
            return "break"
        self._down.add(name)
        if name not in ("ctrl", "alt", "shift", "cmd"):
            self._main = name
        self._render()
        return "break"  # 别让 Tab/空格 触发 ttk 的默认行为

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
        for w in self._inner.winfo_children():
            w.destroy()
        if self._capturing:
            self._prompt = tk.Label(
                self._inner, text=self.WAIT, bg=P["surface"], fg=P["warn"],
                font=theme.FONTS["small"],
            )
            self._prompt.pack()
            self._prompt.bind("<Button-1>", self._focus)
            if self._down:
                key_pills(self._inner, "+".join(_order(self._down)), bg=P["surface"], big=True).pack(
                    pady=(S(12), 0)
                )
            return
        self._prompt = tk.Label(
            self._inner, text=self.IDLE, bg=P["surface"], fg=P["muted"], font=theme.FONTS["small"]
        )
        self._prompt.pack()
        self._prompt.bind("<Button-1>", self._focus)
        if self._spec:
            key_pills(self._inner, self._spec, bg=P["surface"], big=True).pack(pady=(S(12), 0))


_MOD_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "cmd": 3}


def _order(names: Any) -> list[str]:
    """修饰键排前面，且按固定顺序——否则显示成 'space+alt+ctrl' 很别扭。"""
    return sorted(names, key=lambda n: (_MOD_ORDER.get(n, 9), n))


# --------------------------------------------------------------------------- #
# 表单
# --------------------------------------------------------------------------- #


class Form(tk.Frame):
    """两列表单：左边标签，右边控件，下面可选一行说明。"""

    def __init__(self, master: tk.Misc, *, label_width: int = 96, bg: str | None = None) -> None:
        bg = bg or P["surface"]
        super().__init__(master, bg=bg)
        self._bg = bg
        self._row = 0
        self.columnconfigure(1, weight=1)
        self._label_width = label_width

    def add(
        self,
        label: str,
        widget: tk.Widget,
        *,
        note: str = "",
        label_fg: str | None = None,
    ) -> tk.Widget:
        lbl = tk.Label(
            self,
            text=label,
            bg=self._bg,
            fg=label_fg or P["muted"],
            font=theme.FONTS["small"],
            anchor="nw",
            width=0,
        )
        lbl.grid(row=self._row, column=0, sticky="nw", padx=(0, S(12)), pady=(S(4), S(10)))
        lbl.configure(width=max(8, self._label_width // 8))
        widget.grid(row=self._row, column=1, sticky="ew", pady=(0, S(10)))
        if note:
            n = hint(self, note, bg=self._bg)
            n.grid(row=self._row + 1, column=1, sticky="ew", pady=(0, S(10)))
            self._row += 1
        self._row += 1
        return widget

    def spacer(self, px: int = 6) -> None:
        tk.Frame(self, bg=self._bg, height=S(px)).grid(row=self._row, column=0, columnspan=2)
        self._row += 1


def check(
    master: tk.Misc, text: str, var: tk.BooleanVar, *, bg: str | None = None, command: Any = None
) -> "Check":
    """兼容旧写法，返回自定义 Check。"""
    return Check(master, text, var, bg=bg, command=command)


class Check(tk.Frame):
    """自己画的复选框。

    为什么不用 ttk.Checkbutton：clam 主题的选中标记是一个**叉号**（✗），
    在一堆「✓ 已就绪」中间显得像出错了；而且指示器是个没有描边的实心方块，
    未选中时看上去就是一团黑。这两点都没法靠 configure 改掉——标记形状是
    画死在元素里的。

    自己画反而简单：一个 Canvas，选中时画一条对勾。
    """

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        *,
        bg: str | None = None,
        command: Any = None,
        size: int | None = None,
    ) -> None:
        bg = bg or P["bg"]
        super().__init__(master, bg=bg, cursor="hand2", takefocus=True)
        self.var = variable
        self._command = command
        self._bg = bg
        self._side = size or S(15)
        self._hover = False

        self._canvas = tk.Canvas(
            self, width=self._side, height=self._side, bg=bg,
            highlightthickness=0, bd=0, takefocus=False,
        )
        self._canvas.pack(side="left", pady=S(1))
        self._label = tk.Label(self, text=text, bg=bg, fg=P["text"], font=theme.FONTS["small"])
        self._label.pack(side="left", padx=(S(7), 0))

        for w in (self, self._canvas, self._label):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<FocusIn>", lambda _e: self._draw(focus=True))
        self.bind("<FocusOut>", lambda _e: self._draw())
        self.var.trace_add("write", lambda *_a: self._draw())
        self._draw()

    # -- 交互 ------------------------------------------------------------- #

    def _toggle(self, _e: Any = None) -> str:
        self.var.set(not bool(self.var.get()))
        self.focus_set()
        if self._command:
            self._command()
        return "break"

    def _enter(self, _e: Any) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _e: Any) -> None:
        self._hover = False
        self._draw()

    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # noqa: ANN401
        """让 ttk 那套 state()/configure() 的调用方式在这里也能用。"""
        if "state" in kw:
            kw.pop("state")
        return super().configure(cnf, **kw)

    def state(self, *_a: Any) -> None:
        return None

    # -- 绘制 ------------------------------------------------------------- #

    def _draw(self, *, focus: bool = False) -> None:
        s = self._side
        c = self._canvas
        c.delete("all")
        on = bool(self.var.get())
        if on:
            fill, outline = P["accent"], P["accent"]
        elif self._hover:
            fill, outline = P["surface3"], P["faint"]
        else:
            fill, outline = P["surface2"], P["border"]
        c.create_rectangle(1, 1, s - 1, s - 1, fill=fill, outline=outline, width=1)
        if on:
            # 手绘对勾：两条圆头线段。比 clam 那个叉号更像"勾选"
            w = max(2, s // 7)
            c.create_line(s * 0.26, s * 0.52, s * 0.43, s * 0.70, fill=P["white"],
                          width=w, capstyle="round", joinstyle="round")
            c.create_line(s * 0.43, s * 0.70, s * 0.76, s * 0.30, fill=P["white"],
                          width=w, capstyle="round", joinstyle="round")
        elif focus:
            c.create_rectangle(0, 0, s, s, outline=P["accent"], width=1)


# --------------------------------------------------------------------------- #
# 可滚动容器
# --------------------------------------------------------------------------- #


class ScrollFrame(tk.Frame):
    """竖向滚动容器。`inner` 是往里面塞东西的父窗口。

    Tk 没有现成的滚动 Frame。滚轮只在指针进入区域时接管——全局绑会让
    日志面板和设置页抢滚轮，体验很糟。
    """

    def __init__(self, master: tk.Misc, *, bg: str | None = None) -> None:
        bg = bg or P["bg"]
        super().__init__(master, bg=bg)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._sb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner)
        self._canvas.bind("<Configure>", self._on_canvas)
        for w in (self._canvas, self.inner):
            w.bind("<Enter>", lambda _e: self._bind_wheel(True))
            w.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def _on_inner(self, _e: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, e: tk.Event) -> None:
        self._canvas.itemconfigure(self._win, width=e.width)

    def _bind_wheel(self, on: bool) -> None:
        if on:
            self._canvas.bind_all("<MouseWheel>", self._wheel)
        else:
            self._canvas.unbind_all("<MouseWheel>")

    def _wheel(self, e: tk.Event) -> None:
        self._canvas.yview_scroll(int(-e.delta / 120), "units")

    def to_top(self) -> None:
        self._canvas.yview_moveto(0.0)


# --------------------------------------------------------------------------- #
# 日志面板
# --------------------------------------------------------------------------- #


class LogView(tk.Frame):
    """带级别着色、过滤、暂停、导出的日志面板。

    用 Text 而不是 Treeview：一次事件常常是多行（一次命中的完整报告），
    Treeview 一行一条会把内容截断，而排查问题恰恰要看那些被截断的部分。
    """

    CAPACITY = 4000

    def __init__(self, master: tk.Misc, **kw: Any) -> None:
        super().__init__(master, bg=P["bg"], **kw)
        self._events: list[events.Event] = []
        self._min_rank = 0
        self._keyword = ""
        self._paused = False
        self._autoscroll = True

        wrap = tk.Frame(self, bg=P["bg"])
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(
            wrap,
            bg=P["surface"],
            fg=P["text"],
            insertbackground=P["text"],
            selectbackground=P["accent_dim"],
            selectforeground=P["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=P["border"],
            highlightcolor=P["border"],
            padx=S(10),
            pady=S(8),
            wrap="word",
            font=theme.FONTS["mono_small"],
            spacing1=S(1),
            spacing3=S(1),
            state="disabled",
            cursor="arrow",
        )
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        for level in events.LEVELS:
            self.text.tag_configure(f"lv_{level}", foreground=theme.LEVEL_COLOR[level])
        self.text.tag_configure("ts", foreground=P["faint"])
        self.text.tag_configure("cont", foreground=P["muted"], lmargin1=S(90), lmargin2=S(90))

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
        self.text.configure(state="normal")
        self.text.insert("end", f"{ev.clock(millis=True)} ", ("ts",))
        self.text.insert("end", f"{events.GLYPH.get(ev.level, '·')} ", (tag,))
        lines = ev.text.splitlines() or [""]
        self.text.insert("end", lines[0] + "\n", (tag,))
        for extra in lines[1:]:
            self.text.insert("end", extra + "\n", ("cont",))
        self.text.configure(state="disabled")

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
            if self.text.yview()[1] < 0.995:
                self._autoscroll = False
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


# 引擎状态 → 颜色。放在这儿是为了让顶栏、底栏、运行页三处永远一致。
STATE_COLOR = {
    "idle": P["faint"],
    "loading": P["warn"],
    "running": P["ok"],
    "error": P["error"],
}
