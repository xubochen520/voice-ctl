"""控件库（一）：表面、按钮、开关、分段、胶囊、提示条。

所有控件都是 `tk.Canvas`/`tk.Frame` 加 gfx 画出来的抗锯齿图片，**不用 ttk**：
clam 主题的按钮、复选框、滚动条的形状是画死在元素里的，不管怎么 configure
都是方角 + 叉号 + 带箭头的滚动条；想要圆角和一致的状态，只能自己画。

两条贯穿的约定：

  * 控件的 `bg` 是**它身后的颜色**（默认取父级的 bg），不是它自己的填充色。
    带 alpha 的圆角图要和这个颜色混合，所以父级必须有显式的 hex bg，
    不能是系统色名——整个界面里没有一个 Frame 是不指定 bg 的。
  * 尺寸参数是**设计像素**（100% 缩放下的值），进来就过 S()。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

from . import gfx, theme
from .textfit import wrap_text
from .theme import PALETTE as P
from .theme import S, hair, measure, tint

_UNSET: Any = object()


def bg_of(widget: tk.Misc, default: str | None = None) -> str:
    """父级的底色。取不到（系统色名之类）就退回机壳色。"""
    try:
        c = str(widget.cget("bg"))
        gfx.to_rgb(c)
        return c
    except (tk.TclError, ValueError):
        return default or P["chassis"]


def frame(master: tk.Misc, **kw: Any) -> tk.Frame:
    """带显式 bg 的 Frame——默认继承父级，免得漏写变成系统灰。"""
    kw.setdefault("bg", bg_of(master))
    return tk.Frame(master, **kw)


def label(master: tk.Misc, text: str = "", *, font: str = "body", fg: str | None = None,
          bg: str | None = None, **kw: Any) -> tk.Label:
    return tk.Label(master, text=text, font=theme.FONTS[font], fg=fg or P["ink"],
                    bg=bg or bg_of(master), **kw)


def ellipsize(text: str, font: tuple, max_px: int) -> str:
    """文字超宽就截断加省略号。Canvas 的 text 不会自己截，会直接画出界。"""
    if measure(text, font) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid] + "…", font) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


# --------------------------------------------------------------------------- #
# 可拉伸的圆角底（九宫格）
# --------------------------------------------------------------------------- #


class RoundedBg:
    """画在 Canvas 里的圆角矩形：四个角用抗锯齿的图，边和中间用色块。

    面板大小随窗口变，整张重画太贵；而圆角只占四个小角——角是固定的小图，
    窗口怎么拉都只是挪几个 item 的坐标。描边画在形状内侧，所以 w×h 就是它占的大小。
    """

    def __init__(self, cv: tk.Canvas, radius: int, fill: str | None, border: str | None = None,
                 bw: int | None = None, *, tag: str = "rbg") -> None:
        self.cv = cv
        self.radius = max(0, int(radius))
        self.fill = fill
        self.border = border
        self.bw = hair() if bw is None else int(bw)
        self.tag = tag
        self.w = self.h = 0
        self.ox = self.oy = 0
        self._ids: dict[str, int] = {}
        self._styled: tuple | None = None

    def set(self, *, fill: Any = _UNSET, border: Any = _UNSET, bw: int | None = None) -> None:
        if fill is not _UNSET:
            self.fill = fill
        if border is not _UNSET:
            self.border = border
        if bw is not None:
            self.bw = int(bw)
        self._layout()

    def resize(self, w: int, h: int) -> None:
        self.place(0, 0, w, h)

    def place(self, x: int, y: int, w: int, h: int) -> None:
        """放到 Canvas 的 (x, y)，大小 w×h。滚动条滑块、滑块轨道这类不贴着画布左上角的用它。"""
        if (x, y, w, h) != (self.ox, self.oy, self.w, self.h):
            self.ox, self.oy, self.w, self.h = x, y, w, h
            self._layout()

    def _ensure(self) -> None:
        if self._ids:
            return
        cv, t = self.cv, (self.tag,)
        for name in ("ba", "bb", "fa", "fb"):
            self._ids[name] = cv.create_rectangle(0, 0, 0, 0, outline="", fill="", tags=t)
        for name in ("tl", "tr", "bl", "br"):
            self._ids[name] = cv.create_image(0, 0, anchor="nw", tags=t)
        cv.tag_lower(self.tag)

    def _layout(self) -> None:
        self._ensure()
        cv, ids = self.cv, self._ids
        w, h = self.w, self.h
        if w < 2 or h < 2:
            for i in ids.values():
                cv.itemconfigure(i, state="hidden")
            return
        r = int(max(0, min(self.radius, w // 2, h // 2)))
        bw = self.bw if self.border else 0
        style = (r, self.fill, self.border, bw)
        if style != self._styled:
            self._styled = style
            self._restyle(r, bw)
        for i in ids.values():
            cv.itemconfigure(i, state="normal")
        x, y = self.ox, self.oy
        # 边框两块（十字形，四角留给图）、填充两块（缩进 bw）。
        cv.coords(ids["ba"], x + r, y, x + w - r, y + h)
        cv.coords(ids["bb"], x, y + r, x + w, y + h - r)
        cv.coords(ids["fa"], x + r, y + bw, x + w - r, y + h - bw)
        cv.coords(ids["fb"], x + bw, y + r, x + w - bw, y + h - r)
        cv.coords(ids["tl"], x, y)
        cv.coords(ids["tr"], x + w - r, y)
        cv.coords(ids["bl"], x, y + h - r)
        cv.coords(ids["br"], x + w - r, y + h - r)

    def _restyle(self, r: int, bw: int) -> None:
        cv, ids = self.cv, self._ids
        border = self.border if bw else None
        edge = border or self.fill or ""
        cv.itemconfigure(ids["ba"], fill=edge if (border or self.fill) else "")
        cv.itemconfigure(ids["bb"], fill=edge if (border or self.fill) else "")
        cv.itemconfigure(ids["fa"], fill=self.fill or "")
        cv.itemconfigure(ids["fb"], fill=self.fill or "")
        if r <= 0:
            for q in ("tl", "tr", "bl", "br"):
                cv.itemconfigure(ids[q], image="")
            return
        fill, bd = self.fill, border
        if not fill and not bd:
            for q in ("tl", "tr", "bl", "br"):
                cv.itemconfigure(ids[q], image="")
            return
        for k, q in enumerate(("tl", "tr", "bl", "br")):
            img = gfx.photo(cv, ("tile", r, fill, bd, bw, k),
                            lambda k=k: gfx.corner_tiles(r, fill, bd, bw)[k])
            cv.itemconfigure(ids[q], image=img)


# --------------------------------------------------------------------------- #
# 说明文字（宽度跟着容器走）
# --------------------------------------------------------------------------- #


_WIDTHS: dict[tuple, int] = {}


def _cached_measure(font: tuple) -> Callable[[str], int]:
    """按 (字体, 字符串) 缓存字宽：折行要把整段话逐字量一遍，不缓存的话
    每次窗口拖一下、每个说明标签都要重量几十次。"""

    def m(text: str) -> int:
        key = (font, text)
        w = _WIDTHS.get(key)
        if w is None:
            if len(_WIDTHS) > 20000:
                _WIDTHS.clear()
            w = _WIDTHS[key] = measure(text, font)
        return w

    return m


class WrapLabel(tk.Label):
    """会自己折行的 Label：宽度跟着容器走，断行遵守中文的避头尾。

    宽度**不写死**：写死的话，窄面板里文字会被右边裁掉（用户看不到后半句），
    宽面板里又会在半路莫名折行；说明文字正是最需要读完整的那部分。

    折行自己算（见 textfit.py）：Tk 自带的 wraplength 只认空格，中文整段没有空格，
    会在放不下的那个字处硬断，常常单独掉下一个「。」。算好之后把 "\n" 写进 text，
    Label 本身 wraplength=0。因此 `cget("text")` 返回的是**原文**（重写了），不是带换行的版本。

    `width=1`（一个字符宽）是关键：它让 Label **请求的**宽度很小，父级才能把它缩窄，
    再由 <Configure> 拿到实际宽度重新折行。否则窗口缩小时它卡在原来的宽度上被裁掉，永远不会重排。
    """

    def __init__(self, master: tk.Misc, text: str = "", *, font: str = "note", fg: str | None = None,
                 bg: str | None = None, **kw: Any) -> None:
        self._fnt = theme.FONTS[font]
        super().__init__(master, text="", font=self._fnt, fg=fg or P["ink3"], bg=bg or bg_of(master),
                         anchor="w", justify="left", width=1, wraplength=0, padx=0, pady=0, **kw)
        self._raw = text
        self._avail = 0
        self.bind("<Configure>", self._on_configure)
        self._relayout()

    def cget(self, key: str) -> Any:  # noqa: ANN401
        if key == "text":
            return self._raw
        return super().cget(key)

    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # noqa: ANN401
        dirty = False
        if "text" in kw:
            self._raw = str(kw.pop("text"))
            dirty = True
        if isinstance(kw.get("font"), tuple):
            self._fnt = kw["font"]
            dirty = True
        out = super().configure(cnf, **kw) if (cnf or kw) else None
        if dirty:
            self._relayout()
        return out

    config = configure

    def _on_configure(self, e: tk.Event) -> None:
        if abs(e.width - self._avail) > 1:
            self._avail = e.width
            self._relayout()

    def _relayout(self) -> None:
        em = abs(int(self._fnt[1]))
        room = max(S(40), (self._avail or S(400)) - em)  # 留一格给悬挂的标点
        laid = wrap_text(self._raw, _cached_measure(self._fnt), room, em)
        if laid != tk.Label.cget(self, "text"):
            super().configure(text=laid)


def wrap_label(master: tk.Misc, text: str = "", *, font: str = "note", fg: str | None = None,
               bg: str | None = None, **kw: Any) -> WrapLabel:
    return WrapLabel(master, text, font=font, fg=fg, bg=bg, **kw)


def hline(master: tk.Misc, *, color: str | None = None, bg: str | None = None, **pack: Any) -> tk.Frame:
    f = tk.Frame(master, bg=color or P["line"], height=hair())
    f.pack(**({"fill": "x"} | pack))
    return f


# --------------------------------------------------------------------------- #
# 面板 / 卡片 / 设置行
# --------------------------------------------------------------------------- #


class Panel(tk.Frame):
    """圆角面板。`body` 是往里面塞东西的地方。

    背景是一块 Canvas `place` 在最底下，body `pack` 在上面：面板的尺寸由 body 决定
    （place 不参与尺寸协商，所以背景不会把面板撑成 1px），背景只负责画圆角和描边。
    body 的内边距 ≥ 圆角半径，所以它不会盖住四个角。
    """

    def __init__(self, master: tk.Misc, *, radius: float = 16, fill: str | None = None,
                 border: str | None = _UNSET, pad: float = 20, pady: float | None = None,
                 bg: str | None = None) -> None:
        outer = bg or bg_of(master)
        fill = fill or P["panel"]
        if border is _UNSET:
            border = P["line"]
        super().__init__(master, bg=outer)
        self.fill = fill
        self._cv = tk.Canvas(self, bg=outer, highlightthickness=0, bd=0)
        self._cv.place(x=0, y=0, relwidth=1, relheight=1)
        self._rbg = RoundedBg(self._cv, S(radius), fill, border)
        self._cv.bind("<Configure>", lambda e: self._rbg.resize(e.width, e.height))
        self.body = tk.Frame(self, bg=fill)
        self.body.pack(fill="both", expand=True, padx=S(pad), pady=S(pad if pady is None else pady))

    def set_style(self, *, fill: Any = _UNSET, border: Any = _UNSET, bw: int | None = None) -> None:
        if fill is not _UNSET:
            self.fill = fill
            self.body.configure(bg=fill)
        self._rbg.set(fill=fill, border=border, bw=bw)


class Card(Panel):
    """带标题的面板。`row()` 往里加「左边是说明、右边是控件」的设置行，行与行之间自动画发丝线。"""

    def __init__(self, master: tk.Misc, *, title: str = "", pad: float = 20, **kw: Any) -> None:
        super().__init__(master, pad=pad, **kw)
        self._rows = 0
        self.last_row: Row | None = None
        self.actions: tk.Frame | None = None
        if title:
            head = tk.Frame(self.body, bg=self.fill)
            head.pack(fill="x", pady=(0, S(6)))
            tk.Label(head, text=title, bg=self.fill, fg=P["ink"], font=theme.FONTS["heading"],
                     anchor="w").pack(side="left")
            self.actions = tk.Frame(head, bg=self.fill)
            self.actions.pack(side="right")

    def row(self, title: str, note: str = "", *, stack: bool = False) -> tk.Frame:
        """加一行，返回右侧（stack=True 时是下方）放控件的槽。"""
        r = Row(self.body, title, note, divider=self._rows > 0, stack=stack)
        r.pack(fill="x")
        self._rows += 1
        self.last_row = r
        return r.slot


class Row(tk.Frame):
    """设置行：左边标题 + 说明（会折行），右边放控件；`stack` 时控件换到下面通栏。"""

    def __init__(self, master: tk.Misc, title: str, note: str = "", *, divider: bool = True,
                 stack: bool = False) -> None:
        bg = bg_of(master)
        super().__init__(master, bg=bg)
        if divider:
            hline(self, color=P["line"])
        inner = tk.Frame(self, bg=bg)
        inner.pack(fill="x", pady=S(13))
        inner.columnconfigure(0, weight=1)
        text = tk.Frame(inner, bg=bg)
        text.grid(row=0, column=0, sticky="ew", padx=(0, S(20)))
        self.title = tk.Label(text, text=title, bg=bg, fg=P["ink"], font=theme.FONTS["body"], anchor="w")
        self.title.pack(fill="x")
        self.note = wrap_label(text, note, bg=bg) if note else None
        if self.note is not None:
            self.note.pack(fill="x", pady=(S(3), 0))
        self.slot = tk.Frame(inner, bg=bg)
        if stack:
            self.slot.grid(row=1, column=0, sticky="ew", pady=(S(10), 0))
        else:
            self.slot.grid(row=0, column=1, sticky="e")


class Flow(tk.Frame):
    """从左到右排，放不下就换行；`right=True` 的那一类靠右。

    运行页的控制条要放四个按钮、两个状态胶囊和一个「体检」，最小窗口里一行放不下——
    以前就是被硬裁掉（「缺少识别模型」只剩「缺少识别」）。pack 做不到"放不下换行"，
    这里自己算：子控件仍然创建在 Flow 上，用 `add()` 登记顺序，由 Flow 用 place 摆。
    子控件宽度变了（胶囊文字更新）会自动重排。
    """

    def __init__(self, master: tk.Misc, *, hgap: float = 8, vgap: float = 8, bg: str | None = None) -> None:
        super().__init__(master, bg=bg or bg_of(master), height=1)
        self._hg, self._vg = S(hgap), S(vgap)
        self._items: list[tuple[tk.Misc, bool]] = []
        self._hidden: set[tk.Misc] = set()
        self._pending: str | None = None
        self.bind("<Configure>", lambda _e: self._schedule())

    def add(self, widget: tk.Misc, *, right: bool = False) -> None:
        self._items.append((widget, right))
        widget.bind("<Configure>", lambda _e: self._schedule(), add="+")
        self._schedule()

    def set_visible(self, widget: tk.Misc, on: bool) -> None:
        if on == (widget not in self._hidden):
            return
        if on:
            self._hidden.discard(widget)
        else:
            self._hidden.add(widget)
            widget.place_forget()
        self._schedule()

    def _schedule(self) -> None:
        if self._pending is None:
            self._pending = self.after_idle(self.relayout)

    def relayout(self) -> None:
        self._pending = None
        width = self.winfo_width()
        if width <= 1:
            return
        hg, vg = self._hg, self._vg
        left = [w for w, r in self._items if not r and w not in self._hidden]
        right = [w for w, r in self._items if r and w not in self._hidden]
        lines: list[list[tk.Misc]] = [[]]
        used = 0
        for w in left:
            ww = w.winfo_reqwidth()
            if lines[-1] and used + ww > width:
                lines.append([])
                used = 0
            lines[-1].append(w)
            used += ww + hg
        right_w = sum(w.winfo_reqwidth() for w in right) + hg * max(0, len(right) - 1)
        right_line = len(lines) - 1
        if right and lines[-1] and used + right_w > width:
            lines.append([])
            right_line = len(lines) - 1
        y = 0
        for i, line in enumerate(lines):
            group = line + (right if i == right_line else [])
            lh = max((w.winfo_reqheight() for w in group), default=0)
            x = 0
            for w in line:
                w.place(x=x, y=y + (lh - w.winfo_reqheight()) // 2)
                x += w.winfo_reqwidth() + hg
            if i == right_line:
                rx = width - right_w
                for w in right:
                    w.place(x=rx, y=y + (lh - w.winfo_reqheight()) // 2)
                    rx += w.winfo_reqwidth() + hg
            y += lh + vg
        total = max(1, y - vg)
        if int(str(self.cget("height"))) != total:
            self.configure(height=total)


# --------------------------------------------------------------------------- #
# 悬浮提示
# --------------------------------------------------------------------------- #


class Tooltip:
    """图标按钮没有文字，悬停 0.5 秒后说清它是干什么的（键盘用户靠焦点环 + 这里的文字）。"""

    def __init__(self, widget: tk.Misc, text: str, *, delay: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _enter(self, _e: tk.Event) -> None:
        self._cancel()
        if self.text:
            self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        if self._tip is not None or not self.text:
            return
        try:
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_attributes("-topmost", True)
            tip.configure(bg=P["ink"])
            tk.Label(tip, text=self.text, bg=P["ink"], fg=P["white"], font=theme.FONTS["note"],
                     padx=S(10), pady=S(5), justify="left").pack()
            tip.update_idletasks()
            x = self.widget.winfo_rootx() + max(0, (self.widget.winfo_width() - tip.winfo_reqwidth()) // 2)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + S(6)
            sw = self.widget.winfo_screenwidth()
            x = max(S(4), min(x, sw - tip.winfo_reqwidth() - S(4)))
            tip.wm_geometry(f"+{x}+{y}")
            theme.round_popup_corners(tip, border=P["ink"])
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _e: Any = None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


# --------------------------------------------------------------------------- #
# 按钮
# --------------------------------------------------------------------------- #

# kind → 各状态下的 (填充, 描边, 文字)。None = 透明/无。函数而不是常量表：
# ghost 的悬停色要由它身后的底色算出来，同一个 ghost 放在机壳和面板上颜色不同。


def _button_colors(kind: str, state: str, bg: str) -> tuple[str | None, str | None, str]:
    ink, white = P["ink"], P["white"]
    if kind == "primary":
        fill = {"normal": ink, "hover": gfx.mix(ink, white, 0.16), "pressed": gfx.mix(ink, "#000000", 0.4),
                "disabled": "#C9D0CF"}[state]
        return fill, None, ("#8A9697" if state == "disabled" else white)
    if kind == "secondary":
        if state == "disabled":
            return None, P["line"], P["ink4"]
        fill = {"normal": P["raised"], "hover": gfx.mix(P["raised"], ink, 0.04),
                "pressed": gfx.mix(P["raised"], ink, 0.10)}[state]
        edge = gfx.mix(P["line_strong"], ink, 0.22 if state != "normal" else 0.0)
        return fill, edge, ink
    if kind == "danger":
        if state == "disabled":
            return None, P["line"], P["ink4"]
        fill = {"normal": P["raised"], "hover": P["err_bg"], "pressed": gfx.mix(P["err_bg"], P["err"], 0.12)}[state]
        return fill, gfx.mix(P["err"], P["raised"], 0.55), P["err"]
    if kind == "light":  # 深色屏幕上的主按钮：反过来用亮色
        fill = {"normal": P["scr_ink"], "hover": white, "pressed": gfx.mix(P["scr_ink"], P["scr"], 0.25),
                "disabled": P["scr3"]}[state]
        return fill, None, (P["scr_faint"] if state == "disabled" else P["scr"])
    if kind == "screen":  # 深色屏幕上的次要按钮
        if state == "disabled":
            return None, P["scr_line"], P["scr_faint"]
        fill = {"normal": P["scr3"], "hover": gfx.mix(P["scr3"], white, 0.08),
                "pressed": gfx.mix(P["scr3"], "#000000", 0.3)}[state]
        return fill, P["scr_line"], P["scr_ink"]
    if kind == "screen_ghost":
        if state == "disabled":
            return None, None, P["scr_faint"]
        fill = {"normal": None, "hover": P["scr3"], "pressed": gfx.mix(P["scr3"], "#000000", 0.3)}[state]
        return fill, None, P["scr_ink"] if state != "normal" else P["scr_dim"]
    # ghost
    if state == "disabled":
        return None, None, P["ink4"]
    fill = {"normal": None, "hover": tint(bg, 0.07), "pressed": tint(bg, 0.13)}[state]
    return fill, None, P["ink"] if state != "normal" else P["ink2"]


class Button(tk.Canvas):
    """药丸按钮。kind: primary / secondary / ghost / danger / light / screen / screen_ghost。

    为什么是 Canvas 而不是 Label：要圆角抗锯齿（图）+ 图标 + 文字叠在一起，
    还要有悬停/按下/键盘焦点四种外观。键盘焦点环只在**用键盘 Tab 进来**时才画，
    鼠标点一下不画——否则每点一次按钮都留一圈青框，像是出错了。
    """

    HEIGHTS = {"sm": 30, "md": 36, "lg": 44}

    def __init__(self, master: tk.Misc, text: str = "", *, kind: str = "secondary", icon: str | None = None,
                 command: Callable[[], Any] | None = None, size: str = "md", width: float = 0,
                 bg: str | None = None, tooltip: str = "") -> None:
        self._bg = bg or bg_of(master)
        self._kind = kind
        self._text = text
        self._icon = icon
        self._command = command
        self._size = size
        self._min_w = S(width) if width else 0
        self._h = S(self.HEIGHTS[size])
        super().__init__(master, width=1, height=self._h, bg=self._bg, highlightthickness=0, bd=0,
                         takefocus=True, cursor="hand2")
        self._wd = 1
        self._state = "normal"
        self._enabled = True
        self._kbd = False
        self._by_mouse = False
        self._img = self.create_image(0, 0, anchor="nw")
        self._ico = self.create_image(0, 0, anchor="nw")
        self._txt = self.create_text(0, 0, anchor="w", text=text)
        self._measure()
        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<space>", self._key_invoke)
        self.bind("<Return>", self._key_invoke)
        if tooltip:
            self.tooltip = Tooltip(self, tooltip)

    # -- 外部接口 --------------------------------------------------------- #

    def set(self, *, text: Any = _UNSET, icon: Any = _UNSET, kind: Any = _UNSET) -> None:
        # 运行页每 120ms 都会调它；没变化时必须什么都不做，不然每次都要重量文字宽度、重画一遍
        if ((text is _UNSET or text == self._text) and (icon is _UNSET or icon == self._icon)
                and (kind is _UNSET or kind == self._kind)):
            return
        if text is not _UNSET:
            self._text = text
            self.itemconfigure(self._txt, text=text)
        if icon is not _UNSET:
            self._icon = icon
        if kind is not _UNSET:
            self._kind = kind
        self._measure()

    def set_enabled(self, on: bool) -> None:
        if on != self._enabled:
            self._enabled = on
            self._state = "normal"
            self.configure(cursor="hand2" if on else "arrow", takefocus=on)
            self._paint()

    def set_command(self, command: Callable[[], Any] | None) -> None:
        self._command = command

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def text(self) -> str:
        return self._text

    @property
    def kind(self) -> str:
        return self._kind

    def invoke(self) -> None:
        if self._enabled and self._command:
            self._command()

    # -- 布局 ------------------------------------------------------------- #

    def _font(self) -> tuple:
        return theme.FONTS["button_b"] if self._kind in ("primary", "light") else theme.FONTS["button"]

    def _icon_px(self) -> int:
        return S(16) if self._icon else 0

    def _natural_width(self) -> int:
        tw = measure(self._text, self._font()) if self._text else 0
        isz = self._icon_px()
        gap = S(7) if (isz and tw) else 0
        padx = S(14 if self._size == "sm" else 18) if self._text else S(10)
        return max(self._min_w, 2 * padx + isz + gap + tw)

    def _measure(self) -> None:
        nat = self._natural_width()
        self.configure(width=nat, height=self._h)
        self._wd = max(nat, self.winfo_width() if self.winfo_width() > 1 else nat)
        self._place()

    def _on_configure(self, e: tk.Event) -> None:
        if e.width > 1 and e.width != self._wd:
            self._wd = e.width
            self._place()

    def _place(self) -> None:
        font = self._font()
        tw = measure(self._text, font) if self._text else 0
        isz = self._icon_px()
        gap = S(7) if (isz and tw) else 0
        x = (self._wd - (isz + gap + tw)) // 2
        self.coords(self._ico, x, (self._h - isz) // 2)
        self.coords(self._txt, x + isz + gap, self._h // 2)
        self.itemconfigure(self._txt, font=font)
        self._paint()

    def _paint(self) -> None:
        state = "disabled" if not self._enabled else self._state
        fill, border, fg = _button_colors(self._kind, state, self._bg)
        bw = hair()
        if self._kbd and self._enabled:
            border, bw = (P["teal_lit"] if self._kind in ("light", "screen", "screen_ghost") else P["teal"]), S(2)
        w, h = self._wd, self._h
        if fill is None and border is None:
            self.itemconfigure(self._img, state="hidden")
        else:
            img = gfx.photo(self, ("btn", w, h, fill, border, bw),
                            lambda: gfx.rrect_rgba(w, h, h / 2, fill, border, bw))
            self.itemconfigure(self._img, image=img, state="normal")
        self.itemconfigure(self._txt, fill=fg)
        if self._icon:
            self.itemconfigure(self._ico, image=gfx.icon(self, self._icon, self._icon_px(), fg), state="normal")
        else:
            self.itemconfigure(self._ico, state="hidden")

    # -- 交互 ------------------------------------------------------------- #

    def _hover(self, on: bool) -> None:
        if not self._enabled:
            return
        self._state = "hover" if on else "normal"
        self._paint()

    def _press(self, _e: tk.Event) -> None:
        if not self._enabled:
            return
        self._by_mouse = True
        self._kbd = False
        self._state = "pressed"
        self._paint()
        self.focus_set()

    def _release(self, e: tk.Event) -> None:
        if not self._enabled:
            return
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        self._state = "hover" if inside else "normal"
        self._paint()
        if inside and self._command:
            self._command()

    def _focus_in(self, _e: tk.Event) -> None:
        self._kbd = not self._by_mouse
        self._paint()

    def _focus_out(self, _e: tk.Event) -> None:
        self._by_mouse = False
        self._kbd = False
        self._paint()

    def _key_invoke(self, _e: tk.Event) -> str:
        self.invoke()
        return "break"


class IconButton(Button):
    """只有图标的圆形按钮。`toggle=True` 时有"按下去"的选中态（暂停、自动滚动这类开关）。"""

    def __init__(self, master: tk.Misc, icon: str, command: Callable[[], Any] | None = None, *,
                 tooltip: str = "", kind: str = "ghost", size: float = 34, bg: str | None = None,
                 toggle: bool = False) -> None:
        self._d = S(size)
        self._toggle = toggle
        self._selected = False
        super().__init__(master, "", kind=kind, icon=icon, command=command, bg=bg, tooltip=tooltip)
        self._h = self._d
        self.configure(height=self._d, width=self._d)
        self._wd = self._d
        self._place()

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        if on != self._selected:
            self._selected = on
            self._paint()

    def _natural_width(self) -> int:
        return self._d

    def _icon_px(self) -> int:
        return S(18)

    def _release(self, e: tk.Event) -> None:
        if self._enabled and self._toggle:
            inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
            if inside:
                self._selected = not self._selected
        super()._release(e)

    def _paint(self) -> None:
        super()._paint()
        if self._selected and self._enabled:
            dark = self._kind.startswith("screen")
            fill = P["scr3"] if dark else tint(self._bg, 0.11)
            fg = P["scr_ink"] if dark else P["ink"]
            w = h = self._d
            img = gfx.photo(self, ("btn", w, h, fill, None, 0), lambda: gfx.rrect_rgba(w, h, h / 2, fill, None, 0))
            self.itemconfigure(self._img, image=img, state="normal")
            self.itemconfigure(self._ico, image=gfx.icon(self, self._icon or "dot", self._icon_px(), fg))


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #


class Switch(tk.Frame):
    """开关 + 文字，整行都能点。圆钮滑过去 ~120ms（系统关了动画就直接跳）。

    为什么不用复选框：这里每一项都是"开/关"，开关比方框更直接；
    而且 clam 的复选框选中标记是个叉号，在一堆「✓ 已就绪」中间像出错了。
    """

    TRACK_W, TRACK_H = 40, 22

    def __init__(self, master: tk.Misc, text: str = "", variable: tk.BooleanVar | None = None, *,
                 command: Callable[[], Any] | None = None, bg: str | None = None, dark: bool = False,
                 font: str = "body", fg: str | None = None) -> None:
        self._bg = bg or bg_of(master)
        super().__init__(master, bg=self._bg, cursor="hand2", takefocus=True)
        self.var = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._dark = dark
        self._kbd = False
        self._by_mouse = False
        self._enabled = True
        self._tw, self._th = S(self.TRACK_W), S(self.TRACK_H)
        self._pad = S(4)
        self._d = self._th - 2 * S(3)
        self._pos = 1.0 if self.var.get() else 0.0
        self._anim: str | None = None
        self._cv = tk.Canvas(self, width=self._tw + 2 * self._pad, height=self._th + 2 * self._pad,
                             bg=self._bg, highlightthickness=0, bd=0, cursor="hand2")
        self._cv.pack(side="left")
        self._ring = self._cv.create_image(0, 0, anchor="nw")
        self._track = self._cv.create_image(self._pad, self._pad, anchor="nw")
        self._knob = self._cv.create_image(0, self._pad + S(3), anchor="nw")
        self._label: tk.Label | None = None
        if text:
            self._label = tk.Label(self, text=text, bg=self._bg, font=theme.FONTS[font], cursor="hand2",
                                   fg=fg or (P["scr_ink"] if dark else P["ink"]), anchor="w")
            self._label.pack(side="left", padx=(S(8), 0))
        for w in (self, self._cv, self._label):
            if w is not None:
                w.bind("<ButtonPress-1>", self._click)
        self.bind("<space>", self._key)
        self.bind("<Return>", self._key)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._trace = self.var.trace_add("write", lambda *_a: self._sync())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._paint()

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        cur = "hand2" if on else "arrow"
        for w in (self, self._cv, self._label):
            if w is not None:
                w.configure(cursor=cur)
        self._paint()

    # -- 绘制 ------------------------------------------------------------- #

    def _colors(self) -> tuple[str, str | None]:
        on = bool(self.var.get())
        if self._dark:
            fill = P["teal_lit"] if on else P["scr3"]
            edge = None if on else P["scr_line"]
        else:
            fill = P["ink"] if on else "#C3CBCA"
            edge = None
        if not self._enabled:
            fill = gfx.mix(fill, self._bg, 0.55)
        return fill, edge

    def _paint(self) -> None:
        tw, th, pad = self._tw, self._th, self._pad
        fill, edge = self._colors()
        # 动画过半就换成"开"的轨道色，避免两张轨道图做 alpha 渐变
        if self._anim is not None:
            on_like = self._pos >= 0.5
            if on_like != bool(self.var.get()):
                fill = (P["teal_lit"] if self._dark else P["ink"]) if on_like else ("#C3CBCA" if not self._dark else P["scr3"])
        img = gfx.photo(self._cv, ("sw", tw, th, fill, edge), lambda: gfx.rrect_rgba(tw, th, th / 2, fill, edge, hair()))
        self._cv.itemconfigure(self._track, image=img)
        # 圆钮：浅底上永远是白的；深屏幕上"开"时是深色（落在亮青轨道上），"关"时是暗灰
        if not self._dark:
            knob_fill = "#FFFFFF"
        else:
            knob_fill = P["scr"] if self._pos >= 0.5 else P["scr_dim"]
        d = self._d
        kimg = gfx.photo(self._cv, ("knob", d, knob_fill),
                         lambda: gfx.rrect_rgba(d, d, d / 2, knob_fill, gfx.mix(knob_fill, "#000000", 0.16), 1))
        self._cv.itemconfigure(self._knob, image=kimg)
        left, right = pad + S(3), pad + tw - S(3) - d
        self._cv.coords(self._knob, left + (right - left) * self._pos, pad + S(3))
        if self._kbd and self._enabled:
            ring = P["teal_lit"] if self._dark else P["teal"]
            rw, rh = tw + 2 * pad, th + 2 * pad
            rimg = gfx.photo(self._cv, ("swring", rw, rh, ring),
                             lambda: gfx.rrect_rgba(rw, rh, rh / 2, None, ring, S(2)))
            self._cv.itemconfigure(self._ring, image=rimg, state="normal")
        else:
            self._cv.itemconfigure(self._ring, state="hidden")

    def _sync(self) -> None:
        target = 1.0 if self.var.get() else 0.0
        if theme.reduced_motion() or not self.winfo_viewable():
            self._pos = target
            self._anim = None
            self._paint()
            return
        self._animate(target)

    def _animate(self, target: float) -> None:
        if self._anim is not None:
            self.after_cancel(self._anim)
        start, t0 = self._pos, 0

        def step() -> None:
            nonlocal t0
            t0 += 1
            t = min(1.0, t0 / 7)
            self._pos = start + (target - start) * (1 - (1 - t) ** 3)
            if t >= 1.0:
                self._anim = None
                self._pos = target
            else:
                self._anim = self.after(16, step)
            try:
                self._paint()
            except tk.TclError:
                self._anim = None

        step()

    # -- 交互 ------------------------------------------------------------- #

    def toggle(self) -> None:
        if not self._enabled:
            return
        self.var.set(not bool(self.var.get()))
        if self._command:
            self._command()

    def _click(self, _e: tk.Event) -> str:
        self._by_mouse = True
        self._kbd = False
        self.focus_set()
        self.toggle()
        return "break"

    def _key(self, _e: tk.Event) -> str:
        self.toggle()
        return "break"

    def _focus_in(self, _e: tk.Event) -> None:
        self._kbd = not self._by_mouse
        self._paint()

    def _focus_out(self, _e: tk.Event) -> None:
        self._by_mouse = False
        self._kbd = False
        self._paint()

    # 兼容旧的 Check：调用方用 ttk 的 state()/configure(state=) 写法
    def state(self, spec: Any = None) -> Any:  # noqa: ANN401
        if spec:
            self.set_enabled("disabled" not in list(spec))
        return ()


def check(master: tk.Misc, text: str, var: tk.BooleanVar, *, bg: str | None = None,
          command: Callable[[], Any] | None = None, dark: bool = False) -> Switch:
    """旧接口：保持 `W.check(parent, 文字, 变量, bg=, command=)` 的写法，返回开关。"""
    return Switch(master, text, var, bg=bg, command=command, dark=dark)


# --------------------------------------------------------------------------- #
# 分段控件
# --------------------------------------------------------------------------- #


class Segmented(tk.Canvas):
    """几选一的分段控件：选中的那段浮起来（白色药丸），其余沉在凹槽里。

    日志的级别过滤用它而不是下拉框：五个选项一眼全看见，切换是一次点击而不是两次。
    """

    def __init__(self, master: tk.Misc, options: list[tuple[str, str]], variable: tk.StringVar, *,
                 command: Callable[[], Any] | None = None, bg: str | None = None) -> None:
        self._bg = bg or bg_of(master)
        self.var = variable
        self._command = command
        self._opts = options
        font = theme.FONTS["note"]
        self._h = S(34)
        self._pad = S(3)
        self._widths = [measure(lab, font) + S(26) for _v, lab in options]
        total = sum(self._widths) + 2 * self._pad
        super().__init__(master, width=total, height=self._h, bg=self._bg, highlightthickness=0, bd=0,
                         takefocus=True, cursor="hand2")
        self._total = total
        self._rbg = RoundedBg(self, self._h // 2, P["well"], None, 0)
        self._rbg.resize(total, self._h)
        self._pill = self.create_image(0, 0, anchor="nw")
        self._texts: list[int] = []
        x = self._pad
        for (_v, lab), w in zip(options, self._widths, strict=True):
            self._texts.append(self.create_text(x + w // 2, self._h // 2, text=lab, font=font))
            x += w
        self._kbd = False
        self._by_mouse = False
        self.bind("<ButtonPress-1>", self._click)
        self.bind("<Left>", lambda _e: self._step(-1))
        self.bind("<Right>", lambda _e: self._step(1))
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._trace = self.var.trace_add("write", lambda *_a: self._paint())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._paint()

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    def _index(self) -> int:
        cur = self.var.get()
        for i, (v, _l) in enumerate(self._opts):
            if v == cur:
                return i
        return 0

    def _paint(self) -> None:
        i = self._index()
        x = self._pad + sum(self._widths[:i])
        w, h = self._widths[i], self._h - 2 * self._pad
        edge = P["teal"] if self._kbd else P["line_strong"]
        bw = S(2) if self._kbd else hair()
        img = gfx.photo(self, ("seg", w, h, edge, bw),
                        lambda: gfx.rrect_rgba(w, h, h / 2, P["raised"], edge, bw))
        self.itemconfigure(self._pill, image=img)
        self.coords(self._pill, x, self._pad)
        for k, t in enumerate(self._texts):
            self.itemconfigure(t, fill=P["ink"] if k == i else P["ink3"])

    def _select(self, i: int) -> None:
        i = max(0, min(len(self._opts) - 1, i))
        if self._opts[i][0] != self.var.get():
            self.var.set(self._opts[i][0])
            if self._command:
                self._command()

    def _step(self, d: int) -> str:
        self._select(self._index() + d)
        return "break"

    def _click(self, e: tk.Event) -> None:
        self._by_mouse = True
        self._kbd = False
        self.focus_set()
        x = self._pad
        for i, w in enumerate(self._widths):
            if x <= e.x < x + w:
                self._select(i)
                return
            x += w

    def _focus_in(self, _e: tk.Event) -> None:
        self._kbd = not self._by_mouse
        self._paint()

    def _focus_out(self, _e: tk.Event) -> None:
        self._by_mouse = False
        self._kbd = False
        self._paint()


# --------------------------------------------------------------------------- #
# 状态胶囊
# --------------------------------------------------------------------------- #


def tone_color(tone: str, dark: bool = False) -> str:
    """语义色 → 具体颜色。浅底和深底上的"绿"不是同一个绿，对比度才都够。"""
    table = {
        "idle": (P["ink4"], P["scr_faint"]),
        "muted": (P["ink3"], P["scr_dim"]),
        "ok": (P["ok"], P["ok_lit"]),
        "warn": (P["warn"], P["warn_lit"]),
        "error": (P["err"], P["err_lit"]),
        "info": (P["teal"], P["teal_lit"]),
        "live": (P["live_ink"], P["live"]),
    }
    light, lit = table.get(tone, table["muted"])
    return lit if dark else light


class StatusPill(tk.Canvas):
    """一个色点 + 一行字，胶囊底色是该语义色在底色里淡淡的一抹。

    以前这个控件的底是写死的 P["bg"]，放进卡片里就是一块和卡片不一样的黑方块。
    现在底色取自它身后的颜色再叠一点语义色，放哪儿都对。
    """

    def __init__(self, master: tk.Misc, text: str = "", *, tone: str = "muted", bg: str | None = None,
                 dark: bool = False, font: str = "note") -> None:
        self._bg = bg or bg_of(master)
        self._dark = dark
        self._tone = tone
        self._text = text
        self._font = theme.FONTS[font]
        self._h = S(26)
        super().__init__(master, width=1, height=self._h, bg=self._bg, highlightthickness=0, bd=0)
        self._img = self.create_image(0, 0, anchor="nw")
        self._dot = self.create_image(0, 0, anchor="nw")
        self._txt = self.create_text(0, self._h // 2, anchor="w", font=self._font)
        self._apply()

    @property
    def text(self) -> str:
        return self._text

    def set(self, text: str, tone: str | None = None) -> None:
        if text == self._text and (tone is None or tone == self._tone):
            return
        self._text = text
        if tone is not None:
            self._tone = tone
        self._apply()

    def _apply(self) -> None:
        color = tone_color(self._tone, self._dark)
        d, padx, gap = S(7), S(11), S(7)
        tw = measure(self._text, self._font)
        w = padx + d + gap + tw + padx
        h = self._h
        self.configure(width=w)
        fill = gfx.mix(self._bg, color, 0.16 if self._dark else 0.12)
        img = gfx.photo(self, ("pill", w, h, fill), lambda: gfx.rrect_rgba(w, h, h / 2, fill, None, 0))
        self.itemconfigure(self._img, image=img)
        dimg = gfx.photo(self, ("pdot", d, color), lambda: gfx.dot_rgba(d, 0, color, 0.0))
        self.itemconfigure(self._dot, image=dimg)
        self.coords(self._dot, padx, (h - d) // 2)
        self.coords(self._txt, padx + d + gap, h // 2)
        self.itemconfigure(self._txt, text=self._text, fill=P["scr_ink"] if self._dark else P["ink2"])


# --------------------------------------------------------------------------- #
# 提示条
# --------------------------------------------------------------------------- #

_CALLOUT = {
    "ok": ("ok", "ok_bg", "ok"),
    "warn": ("warn", "warn_bg", "warn"),
    "error": ("err", "err_bg", "err"),
    "info": ("teal", "info_bg", "info"),
}


class Callout(tk.Frame):
    """一条带图标的提示：成功 / 警告 / 错误 / 说明。

    文字为空时自己收成 1px（连同上下间距一起），有文字时再展开——
    调用方只管 `pack` 一次，不用每次手工 forget/重新 pack。快捷键页的校验提示就是这么用的。
    间距放在 Callout 里面（`gap`）而不是调用方的 pady：外面的 pady 在收起时仍会留一块空白。
    """

    def __init__(self, master: tk.Misc, text: str = "", kind: str = "info", *, gap: tuple[float, float] = (0, 0),
                 bg: str | None = None) -> None:
        outer = bg or bg_of(master)
        super().__init__(master, bg=outer, height=1)
        self._outer = outer
        self._kind = ""
        self._text = ""
        self._gap = (S(gap[0]), S(gap[1]))
        self._holder = tk.Frame(self, bg=outer)
        self._cv = tk.Canvas(self._holder, bg=outer, highlightthickness=0, bd=0)
        self._cv.place(x=0, y=0, relwidth=1, relheight=1)
        self._rbg = RoundedBg(self._cv, S(12), P["info_bg"], P["line"])
        self._cv.bind("<Configure>", lambda e: self._rbg.resize(e.width, e.height))
        self._inner = tk.Frame(self._holder, bg=P["info_bg"])
        self._inner.pack(fill="x", padx=S(14), pady=S(11))
        self._icon = tk.Label(self._inner, bg=P["info_bg"], bd=0)
        self._icon.pack(side="left", anchor="n", padx=(0, S(10)), pady=(S(1), 0))
        self._msg = wrap_label(self._inner, "", font="note", fg=P["ink"], bg=P["info_bg"])
        self._msg.pack(side="left", fill="x", expand=True)
        self.set(text, kind)

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def text(self) -> str:
        return self._text

    def set(self, text: str, kind: str = "info") -> None:
        self._text = text
        if not text:
            self._holder.pack_forget()
            self.configure(height=1)
            return
        fg_key, bg_key, icon_name = _CALLOUT[kind]
        fg, fill = P[fg_key], P[bg_key]
        self._kind = kind
        self._rbg.set(fill=fill, border=gfx.mix(fill, fg, 0.28))
        self._inner.configure(bg=fill)
        self._msg.configure(text=text, bg=fill)
        self._icon.configure(bg=fill, image=gfx.icon(self, icon_name, S(18), fg))
        if not self._holder.winfo_manager():
            self._holder.pack(fill="x", pady=self._gap)
            self.configure(height=1)


__all__ = [
    "Flow", "WrapLabel", "Button", "Callout", "Card", "IconButton", "Panel", "RoundedBg", "Row", "Segmented", "StatusPill",
    "Switch", "Tooltip", "bg_of", "check", "ellipsize", "frame", "hline", "label", "tone_color", "wrap_label",
]
