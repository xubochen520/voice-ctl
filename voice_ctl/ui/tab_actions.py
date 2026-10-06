"""「功能」页：看清有哪些能力、改说法、试跑、增删。

左边是动作列表（每行：状态灯 + id + 说法 + 就地开关），右边是选中动作的详情表单；
「保存修改 / 试运行 / 真的执行」固定在右栏底部——表单很长，按钮不该跟着滚走
（以前它们就是被裁在窗口外面的）。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox
from typing import Any

from .. import events
from ..config import ActionConfig
from . import gfx
from . import widgets as W
from .kit import RoundedBg, ellipsize
from .theme import FONTS
from .theme import PALETTE as P
from .theme import S, hair, tint

HANDLER_LABEL = {
    "open_app": "启动程序",
    "open_target": "打开你说的应用",
    "open_path": "打开路径",
    "open_url": "打开网址",
    "sysctl": "系统操作",
    "keys": "发送按键",
    "shell": "执行命令",
    "close_app": "关闭应用",
    "schedule": "记日程",
}

HANDLER_TARGET_HINT = {
    "open_app": "exe 名（notepad.exe）、程序名或留空自动查找",
    "open_target": "留空 = 按你说的名字从已安装的应用里找；填了就固定打开这个程序",
    "open_path": r"要打开的文件夹或文件，支持环境变量如 %USERPROFILE%",
    "open_url": "https://… 或协议式地址（ms-settings:）",
    "sysctl": "volume_up / volume_down / mute / lock / screenshot / show_desktop / sleep / explorer",
    "keys": "键序列，如 ctrl+alt+w",
    "shell": "要执行的命令行（默认关闭，有风险）",
    "close_app": "留空 = 按你说的名字解析要关谁；或写进程名（如 notepad.exe）",
    "schedule": "不用填——时间和标题取自你说的那句话",
}

DANGEROUS_SYSCTL = {"lock", "sleep"}

# 预检结果 → 提示条的类型
_STATUS_KIND = {"ok": "ok", "bad": "error", "soft": "info", "?": "info"}


def split_list(text: str) -> list[str]:
    """把'微信, 威信 聊天'这种输入切成列表。中英文逗号、分号、空白都算分隔。"""
    out: list[str] = []
    for chunk in text.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").split(","):
        for piece in chunk.split():
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


# --------------------------------------------------------------------------- #
# 左栏：动作列表
# --------------------------------------------------------------------------- #


class _Row(tk.Canvas):
    """列表里的一行。整行一块 Canvas 自己画：状态灯、两行字、右边一个小开关。

    一行用一个控件而不是 6 个：二三十个动作 × 6 个小控件，每次搜索/刷新都要整批重建。
    """

    def __init__(self, master: tk.Misc, item: dict[str, Any], *, selected: bool,
                 on_click: Callable[[int], None], on_toggle: Callable[[int], None]) -> None:
        self._bg = W.bg_of(master)
        super().__init__(master, height=S(56), bg=self._bg, highlightthickness=0, bd=0, cursor="hand2")
        self.item = item
        self._selected = selected
        self._hover = False
        self._on_click, self._on_toggle = on_click, on_toggle
        self._wd = 0
        self._tile = RoundedBg(self, S(10), None, None, 0)
        self._dot = self.create_image(S(16), S(28), anchor="center")
        self._title = self.create_text(S(32), S(20), anchor="w", font=FONTS["body"])
        self._sub = self.create_text(S(32), S(39), anchor="w", font=FONTS["note"])
        self._track = self.create_image(0, S(28), anchor="e")
        self._knob = self.create_image(0, S(28), anchor="center")
        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)

    def set_selected(self, on: bool) -> None:
        if on != self._selected:
            self._selected = on
            self._paint()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._paint()

    def _on_configure(self, e: tk.Event) -> None:
        self._wd = e.width
        self._tile.resize(e.width, e.height)
        self._paint()

    def _switch_left(self) -> int:
        return self._wd - S(14) - S(30) - S(6)

    def _press(self, e: tk.Event) -> None:
        idx = self.item["index"]
        if e.x >= self._switch_left():
            self._on_toggle(idx)
        else:
            self._on_click(idx)

    def _paint(self) -> None:
        if not self._wd:
            return
        it, w = self.item, self._wd
        on = it["enabled"]
        if self._selected:
            self._tile.set(fill=P["raised"], border=P["line_strong"], bw=hair())
        elif self._hover:
            self._tile.set(fill=tint(self._bg, 0.05), border=None, bw=0)
        else:
            self._tile.set(fill=None, border=None, bw=0)
        # 状态灯：能跑通绿、有问题红、没装（正常状态）灰；停用的画成空心
        status = it["status"]
        color = {"ok": P["ok"], "bad": P["err"], "soft": P["ink4"]}.get(status, P["line_strong"])
        d = S(9)
        if on:
            img = gfx.photo(self, ("adot", d, color), lambda: gfx.dot_rgba(d, 0, color, 0.0))
        else:
            img = gfx.photo(self, ("adot-off", d), lambda: gfx.rrect_rgba(d, d, d / 2, None, P["ink4"], hair() + 1))
        self.itemconfigure(self._dot, image=img)
        room = self._switch_left() - S(32) - S(8)
        self.itemconfigure(self._title, text=ellipsize(it["id"], FONTS["body"], room),
                           fill=P["ink"] if on else P["ink4"])
        self.itemconfigure(self._sub, text=ellipsize(it["aliases"] or "（没有说法）", FONTS["note"], room),
                           fill=P["ink3"] if on else P["ink4"])
        tw, th = S(30), S(16)
        tfill = P["ink"] if on else "#C3CBCA"
        self.itemconfigure(self._track, image=gfx.photo(
            self, ("mini-sw", tw, th, tfill), lambda: gfx.rrect_rgba(tw, th, th / 2, tfill, None, 0)))
        self.coords(self._track, w - S(14), S(28))
        kd = th - S(4)
        self.itemconfigure(self._knob, image=gfx.photo(
            self, ("mini-knob", kd), lambda: gfx.rrect_rgba(kd, kd, kd / 2, "#FFFFFF", None, 0)))
        self.coords(self._knob, w - S(14) - tw + (tw - S(2) - kd // 2 if on else S(2) + kd // 2), S(28))
        for it_ in (self._dot, self._title, self._sub, self._track, self._knob):
            self.tag_raise(it_)


class ActionList(tk.Frame):
    """动作列表。选中项是一块浮起的白色瓷砖；点右边的小开关直接启用/停用。"""

    def __init__(self, master: tk.Misc, *, on_select: Callable[[int], None],
                 on_toggle: Callable[[int], None]) -> None:
        bg = W.bg_of(master)
        super().__init__(master, bg=bg, takefocus=True)
        self._on_select, self._on_toggle = on_select, on_toggle
        self._scroll = W.ScrollFrame(self, bg=bg)
        self._scroll.pack(fill="both", expand=True)
        self._rows: dict[int, _Row] = {}
        self._order: list[int] = []
        self._shown: list[dict[str, Any]] = []
        self.selected: int | None = None
        self.bind("<Up>", lambda _e: self._step(-1))
        self.bind("<Down>", lambda _e: self._step(1))
        self.bind("<space>", lambda _e: self._space())

    @property
    def count(self) -> int:
        return len(self._order)

    @property
    def indices(self) -> list[int]:
        return list(self._order)

    def set_items(self, items: list[dict[str, Any]], selected: int | None) -> None:
        # 内容没变就别重建：每次切到这一页、每次预检结果回来都会调这里，
        # 销毁再创建二三十个 Canvas 是肉眼可见的卡顿
        if items == self._shown and self._rows:
            if selected in self._rows and selected != self.selected:
                self.select(selected, notify=False)
            return
        self._shown = [dict(it) for it in items]
        for w in self._scroll.inner.winfo_children():
            w.destroy()
        self._rows = {}
        self._order = [it["index"] for it in items]
        self.selected = selected if selected in self._order else None
        for it in items:
            row = _Row(self._scroll.inner, it, selected=it["index"] == self.selected,
                       on_click=self._clicked, on_toggle=self._on_toggle)
            row.pack(fill="x", pady=1)
            self._rows[it["index"]] = row

    def _clicked(self, index: int) -> None:
        self.focus_set()
        self.select(index)

    def select(self, index: int, *, notify: bool = True) -> None:
        if index not in self._rows:
            return
        if self.selected in self._rows:
            self._rows[self.selected].set_selected(False)
        self.selected = index
        self._rows[index].set_selected(True)
        self._scroll.scroll_into_view(self._rows[index])
        if notify:
            self._on_select(index)

    def _step(self, d: int) -> str:
        if not self._order:
            return "break"
        pos = self._order.index(self.selected) if self.selected in self._order else -1
        self.select(self._order[max(0, min(len(self._order) - 1, pos + d))])
        return "break"

    def _space(self) -> str:
        if self.selected is not None:
            self._on_toggle(self.selected)
        return "break"


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #


class ActionsTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["chassis"])
        self.app = app
        self._index: int | None = None
        self._status: dict[str, tuple[str, str]] = {}
        self._pending: list[tuple[str, str, str]] | None = None
        self._building = False

        v = tk.StringVar
        self._f_id = v()
        self._f_handler = v()
        self._f_aliases = v()
        self._f_describe = v()
        self._f_target = v()
        self._f_args = v()
        self._f_enabled = tk.BooleanVar(value=True)
        self._search = v()
        self._inputs: list[Any] = []
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        outer = tk.Frame(self, bg=P["chassis"])
        outer.pack(fill="both", expand=True, padx=S(28), pady=S(26))

        left = tk.Frame(outer, bg=P["chassis"], width=S(320))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = tk.Frame(outer, bg=P["chassis"])
        right.pack(side="left", fill="both", expand=True, padx=(S(20), 0))

        # --- 左：列表 -----------------------------------------------------
        panel = W.Panel(left, radius=18, pad=10)
        panel.pack(fill="both", expand=True)
        body = panel.body
        search = W.Field(body, textvariable=self._search, placeholder="搜索动作、说法", icon="search", height=36)
        search.pack(fill="x")
        search.entry.bind("<KeyRelease>", lambda _e: self.refresh_list())
        tools = tk.Frame(body, bg=panel.fill)
        tools.pack(side="bottom", fill="x", pady=(S(8), 0))
        W.hline(body, color=P["line"], side="bottom", pady=(S(8), 0))
        W.IconButton(tools, "plus", self._new, tooltip="新建动作", size=32).pack(side="left")
        W.IconButton(tools, "trash", self._delete, tooltip="删除这个动作", size=32).pack(side="left", padx=(S(2), 0))
        W.IconButton(tools, "arrow_up", lambda: self._move(-1), tooltip="上移（越靠前越先匹配）", size=32).pack(
            side="left", padx=(S(10), 0))
        W.IconButton(tools, "arrow_down", lambda: self._move(1), tooltip="下移", size=32).pack(
            side="left", padx=(S(2), 0))
        W.IconButton(tools, "refresh", self.check_all, tooltip="重新检查每个动作能不能跑通", size=32).pack(side="right")
        self.list = ActionList(body, on_select=self._select_index, on_toggle=self._toggle_index)
        self.list.pack(fill="both", expand=True, pady=(S(8), 0))

        # --- 右：详情 -----------------------------------------------------
        bar = tk.Frame(right, bg=P["chassis"])
        bar.pack(side="bottom", fill="x")
        W.hline(bar, color=P["line"])
        bar_in = tk.Frame(bar, bg=P["chassis"])
        bar_in.pack(fill="x", pady=(S(12), 0))
        self._save_btn = W.Button(bar_in, "保存修改", kind="primary", icon="check", command=self._save)
        self._save_btn.pack(side="left")
        self._dry_btn = W.Button(bar_in, "试运行", icon="flask", command=lambda: self._try(True))
        self._dry_btn.pack(side="left", padx=(S(8), 0))
        self._run_btn = W.Button(bar_in, "真的执行", kind="ghost", icon="play", command=lambda: self._try(False))
        self._run_btn.pack(side="left", padx=(S(4), 0))
        W.hint(
            bar,
            "改了别忘点「保存修改」——它会写回 config.toml（只动这几行，注释全部保留，"
            "并留一份 config.toml.bak 备份）。",
            bg=P["chassis"], font="caption",
        ).pack(fill="x", pady=(S(8), 0))

        head = tk.Frame(right, bg=P["chassis"])
        head.pack(fill="x")
        self._title = tk.Label(head, text="选一个动作", bg=P["chassis"], fg=P["ink"], font=FONTS["title"],
                               anchor="w")
        self._title.pack(side="left")
        self._pre = W.Callout(right, "", "info", gap=(10, 0))
        self._pre.pack(fill="x")

        self.scroll = W.ScrollFrame(right)
        self.scroll.pack(fill="both", expand=True, pady=(S(10), S(0)))
        pane = tk.Frame(self.scroll.inner, bg=P["chassis"])
        pane.pack(fill="both", expand=True, padx=(0, S(6)))

        card = W.Card(pane, title="匹配方式")
        card.pack(fill="x")
        slot = card.row("标识 id", "唯一名字，只影响配置文件，不影响说话。改它不会动到别的动作。")
        f = W.Field(slot, textvariable=self._f_id, mono=True, width=200)
        f.pack()
        self._inputs.append(f)
        slot = card.row(
            "说法",
            "你会怎么念它，用逗号分隔。这是别名匹配的依据——写得越像你平时的说法，越不需要靠语义层。",
            stack=True,
        )
        f = W.Field(slot, textvariable=self._f_aliases)
        f.pack(fill="x")
        self._inputs.append(f)
        slot = card.row("描述", "一句话说明它是干什么的。开了语义层后，这句话会被当成判断依据。", stack=True)
        f = W.Field(slot, textvariable=self._f_describe)
        f.pack(fill="x")
        self._inputs.append(f)

        card2 = W.Card(pane, title="做什么")
        card2.pack(fill="x", pady=(S(18), 0))
        slot = card2.row("类型")
        self._handler_box = W.Select(
            slot, variable=self._f_handler, width=320,
            values=[f"{k}（{v}）" for k, v in HANDLER_LABEL.items()],
            command=lambda _v: self._on_handler_change(),
        )
        self._handler_box.pack()
        self._inputs.append(self._handler_box)
        # target 的含义完全取决于 handler，所以提示跟在「目标」那一行里、并且随类型切换——
        # 写死一句通用的说明等于没说明
        slot = card2.row("目标", HANDLER_TARGET_HINT["open_app"], stack=True)
        self._target_note = card2.last_row.note
        f = W.Field(slot, textvariable=self._f_target, mono=True)
        f.pack(fill="x")
        self._inputs.append(f)
        slot = card2.row("参数", "命令行参数或键序列，逗号分隔。一般留空。", stack=True)
        f = W.Field(slot, textvariable=self._f_args, mono=True)
        f.pack(fill="x")
        self._inputs.append(f)
        slot = card2.row("启用（参与语音匹配）", "停用后这个动作还在配置里，只是语音听不到它。")
        sw = W.Switch(slot, "", self._f_enabled)
        sw.pack()
        self._inputs.append(sw)

        self._set_enabled_form(False)

    # -- 列表 ------------------------------------------------------------- #

    def _visible(self) -> list[tuple[int, ActionConfig]]:
        kw = self._search.get().strip().lower()
        out = []
        for i, a in enumerate(self.app.cfg.actions):
            if kw and kw not in a.id.lower() and kw not in " ".join(a.aliases).lower() \
                    and kw not in a.describe.lower():
                continue
            out.append((i, a))
        return out

    def refresh_list(self, *, keep: int | None = None) -> None:
        want = keep if keep is not None else self._index
        items = []
        for i, a in self._visible():
            st = self._status.get(a.id)
            items.append({
                "index": i,
                "id": a.id,
                "aliases": "、".join(a.aliases),
                "enabled": a.enabled,
                "status": st[0] if st else "?",
            })
        self.list.set_items(items, want)

    def _selected_index(self) -> int | None:
        return self.list.selected

    def _select_index(self, idx: int) -> None:
        self._index = idx
        self._load_form(self.app.cfg.actions[idx])
        self._check_one(idx)

    def _on_select(self, _e: tk.Event | None = None) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self._select_index(idx)

    # -- 表单 ------------------------------------------------------------- #

    def _load_form(self, a: ActionConfig) -> None:
        self._building = True
        self._title.configure(text=a.id)
        self._f_id.set(a.id)
        self._f_handler.set(f"{a.handler}（{HANDLER_LABEL.get(a.handler, a.handler)}）")
        self._f_aliases.set("，".join(a.aliases))
        self._f_describe.set(a.describe)
        self._f_target.set(a.target)
        self._f_args.set("，".join(a.args))
        self._f_enabled.set(a.enabled)
        self._target_note.configure(text=HANDLER_TARGET_HINT.get(a.handler, ""))
        self._set_enabled_form(True)
        self._building = False

    def _set_enabled_form(self, on: bool) -> None:
        for w in self._inputs:
            if isinstance(w, W.Field):
                w.set_state("normal" if on else "disabled")
            else:
                w.set_enabled(on)
        for b in (self._save_btn, self._dry_btn, self._run_btn):
            b.set_enabled(on)

    def _on_handler_change(self) -> None:
        if self._building:
            return
        h = self._f_handler.get().split("（")[0].strip()
        self._target_note.configure(text=HANDLER_TARGET_HINT.get(h, ""))

    def _form_fields(self) -> dict[str, Any]:
        return {
            "id": self._f_id.get().strip(),
            "handler": self._f_handler.get().split("（")[0].strip(),
            "aliases": split_list(self._f_aliases.get()),
            "describe": self._f_describe.get().strip(),
            "target": self._f_target.get().strip(),
            "args": split_list(self._f_args.get()),
            "enabled": bool(self._f_enabled.get()),
        }

    # -- 操作 ------------------------------------------------------------- #

    def _save(self) -> None:
        idx = self._index
        if idx is None:
            return
        fields = self._form_fields()
        old = self.app.cfg.actions[idx]
        if not fields["id"]:
            events.error("动作的 id 不能为空", kind="ui")
            return
        if fields["id"] != old.id and any(a.id == fields["id"] for a in self.app.cfg.actions):
            events.error(f"已经有叫 {fields['id']!r} 的动作了，换个名字", kind="ui")
            return
        if not fields["aliases"] and not fields["target"]:
            events.error("至少要给一个「说法」或一个「目标」，否则它永远匹配不到", kind="ui")
            return

        changed = {k: v for k, v in fields.items() if getattr(old, k) != v}
        if not changed:
            events.info("没有改动需要保存", kind="ui")
            return
        if self.app.save_action(idx, changed):
            self.refresh_list(keep=idx)
            self._on_select()

    def _new(self) -> None:
        from ..confedit import new_action_template

        existing = {a.id for a in self.app.cfg.actions}
        n, aid = 1, "custom.new"
        while aid in existing:
            n += 1
            aid = f"custom.new{n}"
        action = new_action_template(aid)
        idx = self.app.add_action(action)
        if idx is None:
            return
        self._index = idx
        self._search.set("")
        self.refresh_list(keep=idx)
        self._on_select()
        events.info(
            f"已新建 {aid}：把「说法」改成本来会怎么念、把「目标」填成要启动的东西，再点保存修改",
            kind="ui",
        )

    def _delete(self) -> None:
        idx = self._index
        if idx is None:
            return
        a = self.app.cfg.actions[idx]
        if len(self.app.cfg.actions) <= 1:
            events.error("只剩一个动作了，删掉就没得匹配了", kind="ui")
            return
        if not messagebox.askyesno(
            "删除动作", f"确定删除 {a.id}（{a.describe or '无描述'}）？\n\n"
            "会从 config.toml 里移除这个 [[action]] 块，并留下 .bak 备份。",
            parent=self.app.root,
        ):
            return
        if self.app.remove_action(idx):
            self._index = None
            self.refresh_list(keep=None)
            self._clear_form()

    def _clear_form(self) -> None:
        self._building = True
        for var in (self._f_id, self._f_aliases, self._f_describe, self._f_target, self._f_args):
            var.set("")
        self._title.configure(text="选一个动作")
        self._pre.set("", "info")
        self._set_enabled_form(False)
        self._building = False

    def _move(self, delta: int) -> None:
        idx = self._index
        if idx is None:
            return
        if self.app.move_action(idx, delta):
            self._index = idx + delta if 0 <= idx + delta < len(self.app.cfg.actions) else idx
            self.refresh_list(keep=self._index)
            self._on_select()

    def _toggle_index(self, idx: int) -> None:
        """列表里点某一行的小开关：启用 ↔ 停用。不改变当前选中的是哪一行。"""
        a = self.app.cfg.actions[idx]
        if self.app.save_action(idx, {"enabled": not a.enabled}):
            if idx == self._index:
                self._f_enabled.set(not a.enabled)
            self.refresh_list(keep=self._index)
            self._pre.set(
                f"{'已启用' if not a.enabled else '已停用'} {a.id}",
                "ok" if not a.enabled else "warn",
            )

    def _toggle_enabled(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self._toggle_index(idx)

    # -- 预检 ------------------------------------------------------------- #

    def check_all(self) -> None:
        if self._pending is not None:
            return
        actions = list(self.app.cfg.actions)
        self._pre.set("正在检查每个动作能不能跑通 …", "info")

        def work() -> None:
            out: list[tuple[str, str, str]] = []
            for a in actions:
                if not a.enabled:
                    out.append((a.id, "?", "已停用"))
                    continue
                r = self.app.engine.preflight(a.id)
                if r.ok:
                    out.append((a.id, "ok", r.message))
                elif a.handler == "open_app":
                    # 第三方软件没装是正常状态，不该显示成红色的失败
                    out.append((a.id, "soft", r.message))
                else:
                    out.append((a.id, "bad", r.message))
            self._pending = out

        threading.Thread(target=work, name="voice-ctl-preflight", daemon=True).start()

    def _show_status(self, a: ActionConfig) -> None:
        st = self._status.get(a.id)
        if st is None:
            return
        self._pre.set(st[1], _STATUS_KIND[st[0]])

    def _check_one(self, idx: int) -> None:
        if idx >= len(self.app.cfg.actions):
            return
        a = self.app.cfg.actions[idx]
        if not a.enabled:
            self._pre.set("这个动作已停用：它还在配置里，但语音不会匹配到它。", "info")
            return
        if a.id not in self._status:
            self.check_all()
            return
        self._show_status(a)

    # -- 试跑 ------------------------------------------------------------- #

    def _try(self, dry: bool) -> None:
        idx = self._index
        if idx is None:
            return
        a = self.app.cfg.actions[idx]
        handler = self._f_handler.get().split("（")[0].strip()
        target = self._f_target.get().strip()
        if not dry:
            if handler == "shell" or (handler == "sysctl" and target.lower() in DANGEROUS_SYSCTL):
                if not messagebox.askyesno(
                    "确认真的执行",
                    f"{a.id} 会立即生效：\n\n{handler} → {target}\n\n确定吗？",
                    parent=self.app.root,
                ):
                    return
        events.info(f"{'试运行' if dry else '执行'} {a.id} …", kind="ui")
        self.app.run_action_form(idx, self._form_fields(), dry_run=dry)

    # -- 刷新 ------------------------------------------------------------- #

    def on_show(self) -> None:
        if not self._status:
            self.check_all()
        self.refresh_list()
        # 一进来就选中第一个：否则右边是一整套空表单，看着像坏了
        if self._index is None and self.app.cfg.actions:
            first = self.list.indices
            if first:
                self.list.select(first[0])
        elif self._index is not None:
            self.list.select(self._index, notify=False)

    def on_tick(self) -> None:
        if self._pending is not None:
            for aid, level, msg in self._pending:
                self._status[aid] = (level, msg)
            self._pending = None
            self.refresh_list()
            idx = self._index
            if idx is not None and idx < len(self.app.cfg.actions):
                self._show_status(self.app.cfg.actions[idx])
