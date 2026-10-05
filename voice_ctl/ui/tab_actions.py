"""「功能」页：看清有哪些能力、改说法、试跑、增删。"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .. import events
from ..config import ActionConfig
from . import widgets as W
from .theme import PALETTE as P
from .theme import S

HANDLER_LABEL = {
    "open_app": "启动程序",
    "open_path": "打开路径",
    "open_url": "打开网址",
    "sysctl": "系统操作",
    "keys": "发送按键",
    "shell": "执行命令",
}

HANDLER_TARGET_HINT = {
    "open_app": "exe 名（notepad.exe）、程序名或留空自动查找",
    "open_path": r"要打开的文件夹或文件，支持环境变量如 %USERPROFILE%",
    "open_url": "https://… 或协议式地址（ms-settings:）",
    "sysctl": "volume_up / volume_down / mute / lock / screenshot / show_desktop / sleep / explorer",
    "keys": "键序列，如 ctrl+alt+w",
    "shell": "要执行的命令行（默认关闭，有风险）",
}

DANGEROUS_SYSCTL = {"lock", "sleep"}
STATUS_GLYPH = {"ok": "✓", "bad": "✗", "soft": "·", "?": "?"}


def split_list(text: str) -> list[str]:
    """把'微信, 威信 聊天'这种输入切成列表。中英文逗号、分号、空白都算分隔。"""
    out: list[str] = []
    for chunk in text.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").split(","):
        for piece in chunk.split():
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


class ActionsTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["bg"])
        self.app = app
        self._index: int | None = None
        self._status: dict[str, tuple[str, str]] = {}
        self._pending: list[tuple[str, str, str]] | None = None
        self._building = False
        self._rows: dict[str, int] = {}

        v = tk.StringVar
        self._f_id = v()
        self._f_handler = v()
        self._f_aliases = v()
        self._f_describe = v()
        self._f_target = v()
        self._f_args = v()
        self._f_enabled = tk.BooleanVar(value=True)
        self._search = v()
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        outer = tk.Frame(self, bg=P["bg"])
        outer.pack(fill="both", expand=True, padx=S(18), pady=S(18))

        left = tk.Frame(outer, bg=P["bg"], width=S(330))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = tk.Frame(outer, bg=P["bg"])
        right.pack(side="left", fill="both", expand=True, padx=(S(14), 0))

        # --- 左：列表 -----------------------------------------------------
        tk.Label(left, text="动作列表", bg=P["bg"], fg=P["muted"], font=W.theme.FONTS["small"],
                 anchor="w").pack(fill="x")
        tk.Label(
            left, text="勾选 = 参与语音匹配；取消勾选后这个动作还在，只是听不到。",
            bg=P["bg"], fg=P["faint"], font=W.theme.FONTS["tiny"], anchor="w",
            justify="left", wraplength=S(320),
        ).pack(fill="x", pady=(S(4), S(8)))

        srow = tk.Frame(left, bg=P["bg"])
        srow.pack(fill="x", pady=(0, S(8)))
        ent = ttk.Entry(srow, textvariable=self._search)
        ent.pack(fill="x")
        ent.bind("<KeyRelease>", lambda _e: self.refresh_list())

        box = tk.Frame(left, bg=P["border"], highlightthickness=0)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            box, columns=("on", "id", "st"), show="headings", selectmode="browse"
        )
        self.tree.heading("on", text="")
        self.tree.column("on", width=S(30), anchor="center", stretch=False)
        self.tree.heading("id", text="动作")
        self.tree.column("id", width=S(190), anchor="w")
        self.tree.heading("st", text="状态")
        self.tree.column("st", width=S(48), anchor="center", stretch=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<space>", lambda _e: self._toggle_enabled())
        self.tree.bind("<Double-1>", lambda _e: self._toggle_enabled())

        lbtn = tk.Frame(left, bg=P["bg"])
        lbtn.pack(fill="x", pady=(S(8), 0))
        ttk.Button(lbtn, text="新建", command=self._new).pack(side="left")
        ttk.Button(lbtn, text="删除", command=self._delete).pack(side="left", padx=S(6))
        ttk.Button(lbtn, text="↑", width=3, command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(lbtn, text="↓", width=3, command=lambda: self._move(1)).pack(side="left", padx=(S(4), 0))
        ttk.Button(lbtn, text="重新检查", style="Ghost.TButton",
                   command=self.check_all).pack(side="right")

        # --- 右：详情 -----------------------------------------------------
        self._title = tk.Label(right, text="选一个动作", bg=P["bg"], fg=P["text"],
                               font=W.theme.FONTS["subtitle"], anchor="w")
        self._title.pack(fill="x", pady=(0, S(8)))

        self.scroll = W.ScrollFrame(right)
        self.scroll.pack(fill="both", expand=True)
        pane = tk.Frame(self.scroll.inner, bg=P["bg"])
        pane.pack(fill="both", expand=True)

        card = W.Card(pane, title="匹配方式")
        card.pack(fill="x")
        self.form = W.Form(card.body)
        self.form.pack(fill="x")
        self.form.add("标识 id", ttk.Entry(self.form, textvariable=self._f_id),
                      note="唯一名字，只影响配置文件，不影响说话。改它不会动到别的动作。")
        self.form.add(
            "说法", ttk.Entry(self.form, textvariable=self._f_aliases),
            note="你会怎么念它，用逗号分隔。这是别名匹配的依据——写得越像你平时的说法，越不需要靠语义层。",
        )
        self.form.add("描述", ttk.Entry(self.form, textvariable=self._f_describe),
                      note="一句话说明它是干什么的。开了语义层后，这句话会被当成判断依据。")

        card2 = W.Card(pane, title="做什么")
        card2.pack(fill="x", pady=(S(14), 0))
        f2 = W.Form(card2.body)
        f2.pack(fill="x")
        self._target_note = W.hint(f2, HANDLER_TARGET_HINT["open_app"], bg=P["surface"])
        self._handler_box = ttk.Combobox(
            f2,
            textvariable=self._f_handler,
            values=[f"{k}（{v}）" for k, v in HANDLER_LABEL.items()],
            state="readonly",
        )
        f2.add("类型", self._handler_box)
        # target 的含义完全取决于 handler，所以提示行紧跟在「类型」下面、
        # 并且随类型切换而变——写死一句通用的说明等于没说明
        self._target_note.grid(row=f2._row, column=1, sticky="ew", pady=(0, S(8)))  # noqa: SLF001
        f2._row += 1  # noqa: SLF001
        f2.add("目标", ttk.Entry(f2, textvariable=self._f_target))
        f2.add("参数", ttk.Entry(f2, textvariable=self._f_args),
               note="命令行参数或键序列，逗号分隔。一般留空。")
        W.check(f2, "启用（参与语音匹配）", self._f_enabled, bg=P["surface"]).grid(
            row=f2._row, column=1, sticky="w", pady=(0, S(10))  # noqa: SLF001
        )
        f2._row += 1  # noqa: SLF001
        self._handler_box.bind("<<ComboboxSelected>>", lambda _e: self._on_handler_change())

        row = tk.Frame(card2.body, bg=P["surface"])
        row.pack(fill="x", pady=(S(4), 0))
        ttk.Button(row, text="保存修改", style="Primary.TButton", command=self._save).pack(side="left")
        ttk.Button(row, text="试运行", command=lambda: self._try(True)).pack(side="left", padx=S(8))
        ttk.Button(row, text="真的执行", command=lambda: self._try(False)).pack(side="left")

        self._pre = tk.Label(card2.body, text="", bg=P["surface"], fg=P["muted"],
                             font=W.theme.FONTS["small"], anchor="w", justify="left",
                             wraplength=S(420))
        self._pre.pack(fill="x", pady=(S(10), 0))

        W.hint(
            pane,
            "改了别忘点「保存修改」——它会写回 config.toml（只动这几行，注释全部保留，"
            "并留一份 config.toml.bak 备份）。",
        ).pack(fill="x", pady=(S(14), 0))

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
        self.tree.tag_configure("off", foreground=P["faint"])
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        for i, a in self._visible():
            st = self._status.get(a.id)
            mark = STATUS_GLYPH.get(st[0], "?") if st else "?"
            iid = self.tree.insert(
                "", "end",
                values=("●" if a.enabled else "○", a.id, mark),
                tags=("off",) if not a.enabled else (),
            )
            self._rows[iid] = i
        if want is not None and want in self._rows.values():
            for iid, idx in self._rows.items():
                if idx == want:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._rows.get(sel[0])

    def _on_select(self, _e: tk.Event | None = None) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self._index = idx
        self._load_form(self.app.cfg.actions[idx])
        self._check_one(idx)

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
        state = "normal" if on else "disabled"
        for child in self.form.winfo_children():
            if isinstance(child, ttk.Entry):
                child.configure(state=state)

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
        self._pre.configure(text="")
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

    def _toggle_enabled(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        a = self.app.cfg.actions[idx]
        if self.app.save_action(idx, {"enabled": not a.enabled}):
            self._f_enabled.set(not a.enabled)
            self.refresh_list(keep=idx)
            self._pre.configure(
                text=f"{'已启用' if not a.enabled else '已停用'} {a.id}",
                fg=P["ok"] if not a.enabled else P["warn"],
            )

    # -- 预检 ------------------------------------------------------------- #

    def check_all(self) -> None:
        if self._pending is not None:
            return
        actions = list(self.app.cfg.actions)
        self._pre.configure(text="正在检查每个动作能不能跑通 …", fg=P["muted"])

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

    def _check_one(self, idx: int) -> None:
        if idx >= len(self.app.cfg.actions):
            return
        a = self.app.cfg.actions[idx]
        if not a.enabled:
            self._pre.configure(text="这个动作已停用：它还在配置里，但语音不会匹配到它。", fg=P["muted"])
            return
        st = self._status.get(a.id)
        if st is None:
            self.check_all()
            return
        color = {"ok": P["ok"], "bad": P["error"], "soft": P["muted"], "?": P["faint"]}[st[0]]
        self._pre.configure(text=f"{STATUS_GLYPH[st[0]]} {st[1]}", fg=color)

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
            first = self.tree.get_children()
            if first:
                self.tree.selection_set(first[0])
                self.tree.focus(first[0])
                self._on_select()

    def on_tick(self) -> None:
        if self._pending is not None:
            for aid, level, msg in self._pending:
                self._status[aid] = (level, msg)
            self._pending = None
            self.refresh_list()
            idx = self._index
            if idx is not None and idx < len(self.app.cfg.actions):
                a = self.app.cfg.actions[idx]
                st = self._status.get(a.id)
                if st:
                    color = {"ok": P["ok"], "bad": P["error"], "soft": P["muted"], "?": P["faint"]}[st[0]]
                    self._pre.configure(text=f"{STATUS_GLYPH[st[0]]} {st[1]}", fg=color)
