"""控件库（二）：滚动条、输入框、下拉、步进器、滑块、可滚动容器。

和 kit.py 同一套约定：自己画、不用 ttk。输入类控件的共同点是**焦点态要看得见**——
边框变青并加粗到 2px——键盘用户靠它知道自己在哪；鼠标点进去也会变，这是输入框的惯例。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from typing import Any

from . import gfx, theme
from .kit import RoundedBg, bg_of, ellipsize
from .theme import PALETTE as P
from .theme import S, hair, measure, tint

# --------------------------------------------------------------------------- #
# 滚动条
# --------------------------------------------------------------------------- #


class Scrollbar(tk.Canvas):
    """细滚动条：没有箭头，圆头滑块；内容装得下时什么都不画。

    clam 的滚动条带两个箭头按钮，在深浅两种底上都是全界面最"Windows 95"的东西。
    这里只留滑块：悬停时变粗变深，拖动时更深。接口和 ttk.Scrollbar 一致
    （`set(first, last)` + `command`），所以 `yscrollcommand=sb.set` 照常用。
    """

    def __init__(self, master: tk.Misc, command: Callable[..., Any], *, bg: str | None = None,
                 dark: bool = False, width: float = 12) -> None:
        self._bg = bg or bg_of(master)
        self._dark = dark
        self._command = command
        self._cw = S(width)
        super().__init__(master, width=self._cw, bg=self._bg, highlightthickness=0, bd=0)
        self._first, self._last = 0.0, 1.0
        self._hover = False
        self._drag: float | None = None
        self._thumb_w = S(6)
        self._rbg = RoundedBg(self, self._thumb_w // 2, self._color(), None, 0, tag="thumb")
        self._top = self._len = 0
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)

    def _color(self) -> str:
        if self._drag is not None:
            return P["scr_faint"] if self._dark else P["ink3"]
        if self._hover:
            return "#42555A" if self._dark else P["ink4"]
        return "#2E3B3F" if self._dark else P["line_strong"]

    def set(self, first: Any, last: Any) -> None:
        self._first, self._last = float(first), float(last)
        self._draw()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw()

    def _geometry(self) -> tuple[int, int] | None:
        h = self.winfo_height()
        vis = self._last - self._first
        if h < 4 or vis >= 0.9999:
            return None
        length = max(S(28), int(vis * h))
        span = max(1, h - length)
        frac = self._first / max(1e-9, 1.0 - vis)
        return int(round(frac * span)), length

    def _draw(self) -> None:
        g = self._geometry()
        w = self._thumb_w + (S(2) if (self._hover or self._drag is not None) else 0)
        if g is None:
            self._rbg.place(0, 0, 0, 0)
            self._top = self._len = 0
            return
        self._top, self._len = g
        x = (self._cw - w) // 2
        self._rbg.radius = w // 2
        self._rbg.set(fill=self._color())
        self._rbg.place(x, self._top, w, self._len)

    def _press(self, e: tk.Event) -> None:
        if not self._len:
            return
        if self._top <= e.y <= self._top + self._len:
            self._drag = e.y - self._top
            self._draw()
        else:
            self._command("scroll", 1 if e.y > self._top else -1, "pages")

    def _motion(self, e: tk.Event) -> None:
        if self._drag is None or not self._len:
            return
        h = self.winfo_height()
        span = max(1, h - self._len)
        frac = min(1.0, max(0.0, (e.y - self._drag) / span))
        vis = self._last - self._first
        self._command("moveto", frac * (1.0 - vis))

    def _release(self, _e: tk.Event) -> None:
        self._drag = None
        self._draw()


# --------------------------------------------------------------------------- #
# 鼠标滚轮：统一分发给"指针底下"最近的可滚动祖先
# --------------------------------------------------------------------------- #


def install_wheel_router(root: tk.Misc) -> None:
    """全局只绑一次 <MouseWheel>，按指针位置找可滚动的祖先。

    逐个控件绑 Enter/Leave 抢滚轮的老办法有两个毛病：嵌套时（滚动容器里套着日志框）
    两层一起动；指针在子控件上快速移动时 Leave/Enter 的顺序会让滚轮"掉线"。
    """
    r = root._root()  # noqa: SLF001
    if getattr(r, "_voice_ctl_wheel", False):
        return
    r._voice_ctl_wheel = True  # noqa: SLF001
    r.bind_all("<MouseWheel>", _on_wheel, add="+")


def _on_wheel(e: tk.Event) -> None:
    try:
        w = e.widget.winfo_containing(e.x_root, e.y_root)
    except (tk.TclError, KeyError):
        return
    while w is not None:
        if isinstance(w, tk.Text):
            try:
                first, last = w.yview()
            except tk.TclError:
                return
            # Text 自己有类绑定会滚；它还能滚就别让外层再跟着动
            if (e.delta > 0 and first > 0.0) or (e.delta < 0 and last < 1.0):
                return
        target = getattr(w, "_wheel_target", None)
        if target is not None:
            target(e.delta)
            return
        w = w.master


# --------------------------------------------------------------------------- #
# 可滚动容器
# --------------------------------------------------------------------------- #


class ScrollFrame(tk.Frame):
    """竖向滚动容器。`inner` 是往里面塞东西的父窗口。

    Tk 没有现成的滚动 Frame。滚轮由全局分发器接管（见 install_wheel_router）。
    """

    def __init__(self, master: tk.Misc, *, bg: str | None = None, dark: bool = False) -> None:
        bg = bg or bg_of(master)
        super().__init__(master, bg=bg)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=S(22))
        self._sb = Scrollbar(self, command=self._canvas.yview, bg=bg, dark=dark)
        self._canvas.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._acc = 0.0
        self.inner.bind("<Configure>", self._on_inner)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._wheel_target = self._wheel
        install_wheel_router(self)

    def _on_inner(self, _e: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, e: tk.Event) -> None:
        self._canvas.itemconfigure(self._win, width=e.width)

    def _wheel(self, delta: int) -> None:
        # 触控板给的是一串小 delta：累加到够一个单位再滚，否则会一格都不动
        self._acc += -delta / 120.0 * 3.0
        units = int(self._acc)
        if units:
            self._acc -= units
            self._canvas.yview_scroll(units, "units")

    def to_top(self) -> None:
        self._canvas.yview_moveto(0.0)

    def scroll_into_view(self, widget: tk.Misc) -> None:
        """把 widget 滚进可视区——**最小滚动**：已经看得见就不动，在上面/下面才往那个方向挪。"""
        self.update_idletasks()
        total = max(1, self.inner.winfo_height())
        top = widget.winfo_rooty() - self.inner.winfo_rooty()
        bottom = top + widget.winfo_height()
        view_top = int(self._canvas.canvasy(0))
        view_h = max(1, self._canvas.winfo_height())
        if top < view_top:
            self._canvas.yview_moveto(max(0.0, (top - S(8)) / total))
        elif bottom > view_top + view_h:
            self._canvas.yview_moveto(max(0.0, (bottom - view_h + S(8)) / total))


# --------------------------------------------------------------------------- #
# 单行输入框
# --------------------------------------------------------------------------- #


class Field(tk.Frame):
    """圆角输入框。真正接收键盘的是里面的 `entry`（tk.Entry），外壳只负责外观。

    为什么不用 ttk.Entry：clam 的输入框是方角、边框颜色分三层配（border/light/dark），
    怎么调都有一圈对不上的描边；而且圆角、焦点环、占位符都没法做。

    占位符是盖在 Entry 上的一个 Label（有内容或获得焦点就藏起来）——
    不往 Entry 里写占位文字，textvariable 里就永远只有用户真正输入的东西。
    """

    def __init__(self, master: tk.Misc, *, textvariable: tk.StringVar | None = None, width: float = 0,
                 placeholder: str = "", icon: str | None = None, show: str | None = None,
                 bg: str | None = None, dark: bool = False, mono: bool = False, justify: str = "left",
                 height: float = 38, state: str = "normal", font: str | None = None) -> None:
        self._outer = bg or bg_of(master)
        super().__init__(master, bg=self._outer, height=S(height), width=S(width) if width else S(220))
        self.pack_propagate(False)
        self._dark = dark
        self._fill = P["scr2"] if dark else P["raised"]
        self._edge = P["scr_line"] if dark else P["line_strong"]
        self._focus_edge = P["teal_lit"] if dark else P["teal"]
        fg = P["scr_ink"] if dark else P["ink"]
        font = theme.FONTS[font or ("mono" if mono else "body")]

        self._cv = tk.Canvas(self, bg=self._outer, highlightthickness=0, bd=0)
        self._cv.place(x=0, y=0, relwidth=1, relheight=1)
        self._rbg = RoundedBg(self._cv, S(10), self._fill, self._edge)
        self._cv.bind("<Configure>", lambda e: self._rbg.resize(e.width, e.height))

        self.var = textvariable if textvariable is not None else tk.StringVar()
        self.entry = tk.Entry(
            self, textvariable=self.var, relief="flat", bd=0, highlightthickness=0, bg=self._fill, fg=fg,
            insertbackground=fg, insertwidth=S(2), font=font, justify=justify, show=show or "",
            selectbackground=gfx.mix(self._fill, P["teal_lit"] if dark else P["teal"], 0.38),
            selectforeground=fg, disabledbackground=self._fill, disabledforeground=P["scr_faint"] if dark else P["ink4"],
            readonlybackground=self._fill,
        )
        left = S(13)
        if icon:
            self._icon = tk.Label(self, bg=self._fill, bd=0,
                                  image=gfx.icon(self, icon, S(16), P["scr_dim"] if dark else P["ink3"]))
            self._icon.place(x=S(12), rely=0.5, anchor="w")
            left = S(12) + S(16) + S(8)
        self.entry.place(x=left, rely=0.5, anchor="w", relwidth=1.0, width=-(left + S(12)))
        self._ph: tk.Label | None = None
        if placeholder:
            self._ph = tk.Label(self, text=placeholder, bg=self._fill, fg=P["scr_faint"] if dark else P["ink4"],
                                font=font, anchor="w", cursor="xterm")
            self._ph.place(x=left, rely=0.5, anchor="w")
            self._ph.bind("<ButtonPress-1>", lambda _e: self.entry.focus_set())
        self._hover = False
        self._focused = False
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))
        for w in (self, self._cv, self.entry):
            w.bind("<Enter>", lambda _e: self._set_hover(True), add="+")
            w.bind("<Leave>", lambda _e: self._set_hover(False), add="+")
        self._trace = self.var.trace_add("write", lambda *_a: self._sync_ph())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.set_state(state)
        self._sync_ph()

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    # -- 外观 ------------------------------------------------------------- #

    def _paint(self) -> None:
        if self._focused:
            self._rbg.set(border=self._focus_edge, bw=S(2))
        else:
            edge = gfx.mix(self._edge, P["scr_dim"] if self._dark else P["ink"], 0.2) if self._hover else self._edge
            self._rbg.set(border=edge, bw=hair())

    def _set_focus(self, on: bool) -> None:
        self._focused = on
        self._paint()
        self._sync_ph()

    def _set_hover(self, on: bool) -> None:
        if on != self._hover:
            self._hover = on
            self._paint()

    def _sync_ph(self) -> None:
        if self._ph is None:
            return
        if self.var.get() or self._focused:
            self._ph.place_forget()
        else:
            self._ph.place(x=int(self.entry.place_info()["x"]), rely=0.5, anchor="w")

    def set_state(self, state: str) -> None:
        self.entry.configure(state=state)
        self._rbg.set(fill=gfx.mix(self._fill, self._outer, 0.55) if state == "disabled" else self._fill)

    # -- 接口 ------------------------------------------------------------- #

    def get(self) -> str:
        return self.entry.get()

    def set(self, value: str) -> None:
        self.var.set(value)

    def insert(self, index: Any, text: str) -> None:
        self.entry.insert(index, text)

    def delete(self, first: Any, last: Any = None) -> None:
        self.entry.delete(first, last)

    def focus_set(self) -> None:
        self.entry.focus_set()


# --------------------------------------------------------------------------- #
# 下拉选择
# --------------------------------------------------------------------------- #


class Select(tk.Canvas):
    """下拉选择。弹出的列表是自己画的：一行一行、悬停有圆角高亮、当前项带对勾。

    ttk.Combobox 的弹出层是个独立的 Tk Listbox，只能通过 option 数据库改色，
    选中色/边框/行高都不听话；而且和输入框不是一个视觉家族。
    """

    ROW = 34

    def __init__(self, master: tk.Misc, *, variable: tk.StringVar | None = None, values: Sequence[str] = (),
                 command: Callable[[str], Any] | None = None, width: float = 0, bg: str | None = None,
                 dark: bool = False, height: float = 38) -> None:
        self._outer = bg or bg_of(master)
        self._dark = dark
        self._h = S(height)
        super().__init__(master, width=S(width) if width else S(220), height=self._h, bg=self._outer,
                         highlightthickness=0, bd=0, takefocus=True, cursor="hand2")
        self.var = variable if variable is not None else tk.StringVar()
        self.values: list[str] = list(values)
        self._command = command
        self._fill = P["scr2"] if dark else P["raised"]
        self._edge = P["scr_line"] if dark else P["line_strong"]
        self._rbg = RoundedBg(self, S(10), self._fill, self._edge)
        self._txt = self.create_text(S(13), self._h // 2, anchor="w", font=theme.FONTS["body"],
                                     fill=P["scr_ink"] if dark else P["ink"])
        self._chev = self.create_image(0, self._h // 2, anchor="e")
        self._wd = 0
        self._hover = False
        self._focused = False
        self._open: _Dropdown | None = None
        self._enabled = True
        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._toggle)
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        for key in ("<Return>", "<space>", "<Down>", "<Up>"):
            self.bind(key, self._toggle_key)
        self._trace = self.var.trace_add("write", lambda *_a: self._refresh())
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            self.close()
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    # -- 接口（兼容 ttk.Combobox 的 configure(values=...)）---------------- #

    def set_values(self, values: Sequence[str]) -> None:
        self.values = list(values)
        self._refresh()

    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # noqa: ANN401
        if "values" in kw:
            self.values = list(kw.pop("values"))
            self._refresh()
        if "state" in kw:
            st = kw.pop("state")
            self.set_enabled(st != "disabled")
        return super().configure(cnf, **kw) if (cnf or kw) else None

    config = configure

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        tk.Canvas.configure(self, cursor="hand2" if on else "arrow")
        self._refresh()

    # -- 外观 ------------------------------------------------------------- #

    def _on_configure(self, e: tk.Event) -> None:
        self._wd = e.width
        self._rbg.resize(e.width, e.height)
        self._refresh()

    def _paint_edge(self) -> None:
        if self._focused or self._open is not None:
            self._rbg.set(border=P["teal_lit"] if self._dark else P["teal"], bw=S(2))
        else:
            edge = gfx.mix(self._edge, P["scr_dim"] if self._dark else P["ink"], 0.2) if self._hover else self._edge
            self._rbg.set(border=edge, bw=hair())

    def _refresh(self) -> None:
        if not self._wd:
            return
        color = P["scr_dim"] if self._dark else P["ink3"]
        self.itemconfigure(self._chev, image=gfx.icon(self, "chev_down", S(16), color))
        self.coords(self._chev, self._wd - S(11), self._h // 2)
        room = self._wd - S(13) - S(11) - S(16) - S(8)
        text = self.var.get()
        self.itemconfigure(self._txt, text=ellipsize(text, theme.FONTS["body"], max(S(20), room)),
                           fill=(P["scr_faint"] if self._dark else P["ink4"]) if not self._enabled
                           else (P["scr_ink"] if self._dark else P["ink"]))
        self._paint_edge()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._paint_edge()

    def _set_focus(self, on: bool) -> None:
        self._focused = on
        self._paint_edge()

    # -- 弹出 ------------------------------------------------------------- #

    def _toggle(self, _e: Any = None) -> None:
        if not self._enabled:
            return
        self.focus_set()
        if self._open is not None:
            self.close()
        else:
            self.open()

    def _toggle_key(self, _e: tk.Event) -> str:
        self._toggle()
        return "break"

    def open(self) -> None:
        if self._open is not None or not self.values:
            return
        self._open = _Dropdown(self)
        self._paint_edge()

    def close(self) -> None:
        dd, self._open = self._open, None
        if dd is not None:
            dd.dismiss()
        try:
            self._paint_edge()
        except tk.TclError:
            pass

    def choose(self, value: str) -> None:
        self.var.set(value)
        self.close()
        if self._command:
            self._command(value)


class _Dropdown(tk.Toplevel):
    """Select 的弹出层。没有标题栏的置顶小窗，自己管键盘与"点外面就收起"。"""

    def __init__(self, owner: Select) -> None:
        super().__init__(owner)
        self.owner = owner
        self.wm_overrideredirect(True)
        self.wm_attributes("-topmost", True)
        dark = owner._dark  # noqa: SLF001
        edge = P["scr_line"] if dark else P["line_strong"]
        self._fill = P["scr2"] if dark else P["raised"]
        self.configure(bg=edge)
        self._values = owner.values
        n = len(self._values)
        self._rowh = S(Select.ROW)
        self._pad = S(6)
        self._visible = min(n, 8)
        width = max(owner.winfo_width(), S(180))
        self._cw = width - 2
        self._cv = tk.Canvas(self, width=self._cw, height=self._visible * self._rowh + 2 * self._pad,
                             bg=self._fill, highlightthickness=0, bd=0, yscrollincrement=1)
        self._cv.pack(padx=1, pady=1)
        self._cv.configure(scrollregion=(0, 0, self._cw, n * self._rowh + 2 * self._pad))
        self._hi = self._cv.create_image(0, 0, anchor="nw")
        hw, hh = self._cw - 2 * S(6), self._rowh - S(4)
        hi_fill = P["scr3"] if dark else gfx.mix(P["raised"], P["ink"], 0.06)
        self._cv.itemconfigure(self._hi, image=gfx.photo(
            self._cv, ("ddhi", hw, hh, hi_fill), lambda: gfx.rrect_rgba(hw, hh, S(8), hi_fill, None, 0)))
        self._cur = owner.values.index(owner.var.get()) if owner.var.get() in owner.values else 0
        self._sel = self._cur
        fg = P["scr_ink"] if dark else P["ink"]
        font = theme.FONTS["body"]
        for i, v in enumerate(self._values):
            y = self._pad + i * self._rowh + self._rowh // 2
            room = self._cw - S(14) - S(40)
            self._cv.create_text(S(16), y, text=ellipsize(v, font, room), anchor="w", font=font, fill=fg)
            if i == self._cur:
                self._cv.create_image(self._cw - S(16), y, anchor="e", image=gfx.icon(
                    self._cv, "check", S(16), P["teal_lit"] if dark else P["teal"]))
        self._move_hi()
        if n > self._visible:
            self._sb = Scrollbar(self, command=self._cv.yview, bg=self._fill, dark=dark, width=8)
            self._sb.place(relx=1.0, x=-S(9), y=S(6), relheight=1.0, height=-S(12), width=S(8))
            self._cv.configure(yscrollcommand=self._sb.set)

        ox, oy, oh = owner.winfo_rootx(), owner.winfo_rooty(), owner.winfo_height()
        self.update_idletasks()
        h = self.winfo_reqheight()
        y = oy + oh + S(4)
        if y + h > self.winfo_screenheight() - S(40):
            y = max(S(8), oy - h - S(4))
        self.wm_geometry(f"{width}x{h}+{ox}+{y}")
        theme.round_popup_corners(self, border=edge)

        self._cv.bind("<Motion>", self._motion)
        self._cv.bind("<ButtonRelease-1>", self._click)
        self._cv.bind("<MouseWheel>", lambda e: self._cv.yview_scroll(int(-e.delta / 120 * self._rowh * 2.5), "units"))
        for key, fn in (("<Up>", lambda: self._go(-1)), ("<Down>", lambda: self._go(1)),
                        ("<Home>", lambda: self._go(-10**6)), ("<End>", lambda: self._go(10**6)),
                        ("<Prior>", lambda: self._go(-self._visible)), ("<Next>", lambda: self._go(self._visible))):
            self.bind(key, lambda _e, fn=fn: (fn(), "break")[1])
        self.bind("<Return>", lambda _e: self._pick())
        self.bind("<Escape>", lambda _e: owner.close())
        self.bind("<Tab>", lambda _e: owner.close())
        top = owner.winfo_toplevel()
        self._outside_id = top.bind("<ButtonPress>", self._outside, add="+")
        self._cfg_id = top.bind("<Configure>", lambda _e: owner.close(), add="+")
        self._main = top
        self.bind("<FocusOut>", lambda _e: self.after(80, self._lost_focus))
        self.after(10, self._grab_focus)
        self._ensure_visible()

    def _grab_focus(self) -> None:
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _lost_focus(self) -> None:
        try:
            if self.focus_displayof() is None:
                self.owner.close()
        except tk.TclError:
            pass

    def _outside(self, e: tk.Event) -> None:
        w = e.widget
        if w is self.owner:  # 点回 Select 自己：交给它的 toggle 去收起，别在这里先收再被重新打开
            return
        try:
            if str(w).startswith(str(self)):
                return
        except tk.TclError:
            pass
        self.owner.close()

    def _move_hi(self) -> None:
        self._cv.coords(self._hi, S(6), self._pad + self._sel * self._rowh + S(2))

    def _ensure_visible(self) -> None:
        total = len(self._values) * self._rowh + 2 * self._pad
        view = self._visible * self._rowh + 2 * self._pad
        if total <= view:
            return
        top = self._pad + self._sel * self._rowh
        cur = int(self._cv.canvasy(0))
        if top < cur:
            self._cv.yview_moveto(top / total)
        elif top + self._rowh > cur + view:
            self._cv.yview_moveto((top + self._rowh - view + self._pad) / total)

    def _index_at(self, y: int) -> int:
        i = (int(self._cv.canvasy(y)) - self._pad) // self._rowh
        return max(0, min(len(self._values) - 1, i))

    def _motion(self, e: tk.Event) -> None:
        i = self._index_at(e.y)
        if i != self._sel:
            self._sel = i
            self._move_hi()

    def _go(self, d: int) -> None:
        self._sel = max(0, min(len(self._values) - 1, self._sel + d))
        self._move_hi()
        self._ensure_visible()

    def _click(self, e: tk.Event) -> None:
        self._sel = self._index_at(e.y)
        self._pick()

    def _pick(self) -> None:
        self.owner.choose(self._values[self._sel])

    def dismiss(self) -> None:
        try:
            self._main.unbind("<ButtonPress>", self._outside_id)
            self._main.unbind("<Configure>", self._cfg_id)
        except (tk.TclError, AttributeError):
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass
        try:
            self.owner.focus_set()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- #
# 步进器
# --------------------------------------------------------------------------- #


class Stepper(tk.Frame):
    """[−] 数值 单位 [+]，按住 + / − 会连续加减。输入框里手输也行，回车或离开时校验并夹到范围内。

    以前用 ttk.Spinbox：箭头小到点不准，而且它不夹范围——手输 99999 会被原样接受。
    """

    def __init__(self, master: tk.Misc, *, variable: tk.Variable, from_: float, to: float, step: float = 1,
                 unit: str = "", width: float = 150, bg: str | None = None, dark: bool = False,
                 command: Callable[[], Any] | None = None) -> None:
        self._outer = bg or bg_of(master)
        super().__init__(master, bg=self._outer, width=S(width), height=S(38))
        self.pack_propagate(False)
        self.var = variable
        self.lo, self.hi, self.step = from_, to, step
        self._integer = float(step).is_integer() and float(from_).is_integer()
        self._command = command
        self._dark = dark
        fill = P["scr2"] if dark else P["raised"]
        self._edge = P["scr_line"] if dark else P["line_strong"]
        self._fill = fill
        h = S(38)
        self._cv = tk.Canvas(self, bg=self._outer, highlightthickness=0, bd=0)
        self._cv.place(x=0, y=0, relwidth=1, relheight=1)
        self._rbg = RoundedBg(self._cv, S(10), fill, self._edge)
        self._cv.bind("<Configure>", self._on_configure)
        icol = P["scr_dim"] if dark else P["ink2"]
        self._btn_w = S(36)
        self._minus = self._cv.create_image(self._btn_w // 2, h // 2, image=gfx.icon(self._cv, "minus", S(16), icol))
        self._plus = self._cv.create_image(0, h // 2, image=gfx.icon(self._cv, "plus", S(16), icol))
        self._hov_img = self._cv.create_image(0, 0, anchor="nw")
        self._unit = self._cv.create_text(0, h // 2, anchor="w", text=unit, font=theme.FONTS["note"],
                                          fill=P["scr_dim"] if dark else P["ink3"])
        self._unit_w = measure(unit, theme.FONTS["note"]) + S(6) if unit else 0
        self.text = tk.StringVar()
        fg = P["scr_ink"] if dark else P["ink"]
        self.entry = tk.Entry(self, textvariable=self.text, relief="flat", bd=0, highlightthickness=0, bg=fill,
                              fg=fg, insertbackground=fg, insertwidth=S(2), justify="right",
                              font=theme.FONTS["digits"],
                              selectbackground=gfx.mix(fill, P["teal_lit"] if dark else P["teal"], 0.38),
                              selectforeground=fg)
        self._hold: str | None = None
        self._focused = False
        for tag, d in (("minus", -1), ("plus", 1)):
            item = self._minus if tag == "minus" else self._plus
            self._cv.tag_bind(item, "<ButtonPress-1>", lambda _e, d=d: self._press(d))
            self._cv.tag_bind(item, "<ButtonRelease-1>", self._release)
        self._cv.bind("<ButtonPress-1>", self._press_zone)
        self._cv.bind("<ButtonRelease-1>", self._release)
        self._cv.configure(cursor="arrow")
        self.entry.bind("<Return>", lambda _e: self._commit())
        self.entry.bind("<FocusOut>", lambda _e: (self._set_focus(False), self._commit()))
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<Up>", lambda _e: (self._bump(1), "break")[1])
        self.entry.bind("<Down>", lambda _e: (self._bump(-1), "break")[1])
        self._trace = self.var.trace_add("write", lambda *_a: self._sync())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._sync()

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            self._release()
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    # -- 数值 ------------------------------------------------------------- #

    def _value(self) -> float:
        try:
            return float(self.var.get())
        except (tk.TclError, ValueError):
            return float(self.lo)

    def _fmt(self, v: float) -> str:
        return str(int(round(v))) if self._integer else f"{v:g}"

    def _write(self, v: float) -> None:
        v = max(self.lo, min(self.hi, v))
        if self._integer:
            v = int(round(v))
        try:
            self.var.set(v)
        except tk.TclError:
            self.var.set(str(v))
        self.text.set(self._fmt(v))
        if self._command:
            self._command()

    def _sync(self) -> None:
        if not self._focused:
            self.text.set(self._fmt(self._value()))

    def _commit(self) -> None:
        try:
            v = float(self.text.get())
        except ValueError:
            v = self._value()
        self._write(v)

    def _bump(self, d: int) -> None:
        self._commit()
        self._write(self._value() + d * self.step)

    def set(self, value: float) -> None:
        self._write(float(value))

    # -- 布局 / 交互 ------------------------------------------------------ #

    def _on_configure(self, e: tk.Event) -> None:
        w, h = e.width, e.height
        self._rbg.resize(w, h)
        self._cv.coords(self._plus, w - self._btn_w // 2, h // 2)
        ex0 = self._btn_w
        ex1 = w - self._btn_w - self._unit_w
        self._cv.coords(self._unit, ex1 + S(4), h // 2)
        self.entry.place(x=ex0, rely=0.5, anchor="w", width=max(S(20), ex1 - ex0))

    def _zone(self, x: int) -> int:
        w = self.winfo_width()
        if x < self._btn_w:
            return -1
        if x > w - self._btn_w:
            return 1
        return 0

    def _press_zone(self, e: tk.Event) -> None:
        d = self._zone(e.x)
        if d:
            self._press(d)

    def _press(self, d: int) -> None:
        self._release()
        self.entry.focus_set()
        self._bump(d)
        self._hold = self.after(380, lambda: self._repeat(d))

    def _repeat(self, d: int) -> None:
        self._bump(d)
        self._hold = self.after(70, lambda: self._repeat(d))

    def _release(self, _e: Any = None) -> None:
        if self._hold is not None:
            try:
                self.after_cancel(self._hold)
            except tk.TclError:
                pass
            self._hold = None

    def _set_focus(self, on: bool) -> None:
        self._focused = on
        if on:
            self._rbg.set(border=P["teal_lit"] if self._dark else P["teal"], bw=S(2))
        else:
            self._rbg.set(border=self._edge, bw=hair())


# --------------------------------------------------------------------------- #
# 滑块
# --------------------------------------------------------------------------- #


class Slider(tk.Canvas):
    """细轨道 + 圆钮。已走过的那一段是墨色，没走的是凹槽色。

    ttk.Scale 在 clam 下的滑块是个带三道竖线的小方块，轨道是一条描边——
    整个设置页里最扎眼的一处。接口保持 ttk.Scale 的 `cget("from")` / `cget("to")` / `command`。
    """

    def __init__(self, master: tk.Misc, *, from_: float, to: float, variable: tk.DoubleVar,
                 command: Callable[[Any], Any] | None = None, bg: str | None = None, dark: bool = False,
                 height: float = 28) -> None:
        self._outer = bg or bg_of(master)
        self._dark = dark
        super().__init__(master, height=S(height), width=S(160), bg=self._outer, highlightthickness=0, bd=0,
                         takefocus=True, cursor="hand2")
        self._lo, self._hi = float(from_), float(to)
        self.var = variable
        self._command = command
        self._d = S(18)
        self._th = S(4)
        self._track = RoundedBg(self, self._th // 2, "#2B383C" if dark else "#CBD3D2", None, 0, tag="trk")
        self._fill = RoundedBg(self, self._th // 2, P["teal_lit"] if dark else P["ink"], None, 0, tag="fil")
        self._thumb = self.create_image(0, 0, anchor="nw")
        self._wd = self._h = 0
        self._dragging = False
        self._kbd = False
        self._by_mouse = False
        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Left>", lambda _e: self._nudge(-1))
        self.bind("<Right>", lambda _e: self._nudge(1))
        self.bind("<Home>", lambda _e: self._set_value(self._lo, user=True))
        self.bind("<End>", lambda _e: self._set_value(self._hi, user=True))
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._trace = self.var.trace_add("write", lambda *_a: self._draw())
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    def cget(self, key: str) -> Any:  # noqa: ANN401
        if key == "from":
            return self._lo
        if key == "to":
            return self._hi
        return super().cget(key)

    def _x_of(self, v: float) -> float:
        span = self._hi - self._lo
        frac = 0.0 if span <= 0 else (v - self._lo) / span
        m = self._d / 2
        return m + max(0.0, min(1.0, frac)) * (self._wd - 2 * m)

    def _value_at(self, x: float) -> float:
        m = self._d / 2
        frac = (x - m) / max(1.0, self._wd - 2 * m)
        return self._lo + max(0.0, min(1.0, frac)) * (self._hi - self._lo)

    def _on_configure(self, e: tk.Event) -> None:
        self._wd, self._h = e.width, e.height
        self._draw()

    def _draw(self) -> None:
        if not self._wd:
            return
        try:
            v = float(self.var.get())
        except (tk.TclError, ValueError):
            v = self._lo
        x = self._x_of(v)
        cy = self._h // 2
        m = self._d // 2
        self._track.place(m, cy - self._th // 2, self._wd - 2 * m, self._th)
        self._fill.place(m, cy - self._th // 2, max(0, int(x) - m), self._th)
        big = self._dragging or self._kbd
        d = self._d
        edge = (P["teal_lit"] if self._dark else P["teal"]) if big else (P["scr_dim"] if self._dark else P["line_strong"])
        face = P["scr"] if self._dark else P["raised"]
        bw = S(2) if big else hair()
        img = gfx.photo(self, ("thumb", d, face, edge, bw), lambda: gfx.rrect_rgba(d, d, d / 2, face, edge, bw))
        self.itemconfigure(self._thumb, image=img)
        self.coords(self._thumb, int(x) - d // 2, cy - d // 2)
        self.tag_raise(self._thumb)

    def _set_value(self, v: float, *, user: bool = False) -> None:
        v = max(self._lo, min(self._hi, v))
        self.var.set(v)
        if user and self._command:
            self._command(v)

    def _press(self, e: tk.Event) -> None:
        self._by_mouse = True
        self._kbd = False
        self.focus_set()
        self._dragging = True
        self._set_value(self._value_at(e.x), user=True)

    def _drag(self, e: tk.Event) -> None:
        if self._dragging:
            self._set_value(self._value_at(e.x), user=True)

    def _release(self, _e: tk.Event) -> None:
        self._dragging = False
        self._draw()

    def _nudge(self, d: int) -> str:
        self._set_value(float(self.var.get()) + d * (self._hi - self._lo) / 50.0, user=True)
        return "break"

    def _focus_in(self, _e: tk.Event) -> None:
        self._kbd = not self._by_mouse
        self._draw()

    def _focus_out(self, _e: tk.Event) -> None:
        self._by_mouse = False
        self._kbd = False
        self._draw()

    def set(self, v: float) -> None:
        self._set_value(v)


__all__ = ["Field", "ScrollFrame", "Scrollbar", "Select", "Slider", "Stepper", "install_wheel_router", "tint"]
