"""「日志」页：实时事件流 + 过滤 + 导出。"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any

from .. import bootstrap, events
from . import widgets as W
from .theme import PALETTE as P
from .theme import S

LEVEL_CHOICES = [
    ("全部（含调试）", "debug"),
    ("常规", "info"),
    ("只看成功", "ok"),
    ("只看问题（警告+错误）", "warn"),
    ("只看错误", "error"),
]


class LogsTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["bg"])
        self.app = app
        self.view = W.LogView(self)
        self._kw = tk.StringVar()
        self._paused = tk.BooleanVar(value=False)
        self._autoscroll = tk.BooleanVar(value=True)
        self._level = tk.StringVar(value="常规")
        self._status = tk.StringVar(value="")
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        bar = tk.Frame(self, bg=P["surface"])
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=P["surface"])
        inner.pack(fill="x", padx=S(12), pady=S(7))

        tk.Label(
            inner, text="级别", bg=P["surface"], fg=P["muted"], font=W.theme.FONTS["small"]
        ).pack(side="left", padx=(0, S(6)))
        cb = ttk.Combobox(
            inner, textvariable=self._level, values=[c[0] for c in LEVEL_CHOICES],
            state="readonly", width=18,
        )
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        tk.Label(
            inner, text="搜索", bg=P["surface"], fg=P["muted"], font=W.theme.FONTS["small"]
        ).pack(side="left", padx=(S(12), S(6)))
        ent = ttk.Entry(inner, textvariable=self._kw, width=22)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda _e: self._apply_filter())

        W.check(inner, "暂停", self._paused, bg=P["surface"], command=self._on_pause).pack(
            side="left", padx=(S(12), 0)
        )
        W.check(
            inner, "自动滚动", self._autoscroll, bg=P["surface"],
            command=lambda: self.view.set_autoscroll(bool(self._autoscroll.get())),
        ).pack(side="left", padx=(S(8), 0))

        ttk.Button(inner, text="导出", command=self._export).pack(side="right")
        ttk.Button(inner, text="打开日志文件", command=self._open_file).pack(
            side="right", padx=(0, S(8))
        )
        ttk.Button(inner, text="清空", style="Ghost.TButton", command=self._clear).pack(
            side="right", padx=(0, S(8))
        )

        self.view.pack(fill="both", expand=True, padx=S(12), pady=(S(10), 0))

        foot = tk.Frame(self, bg=P["bg"])
        foot.pack(fill="x", padx=S(12), pady=S(8))
        tk.Label(
            foot, textvariable=self._status, bg=P["bg"], fg=P["faint"],
            font=W.theme.FONTS["tiny"], anchor="w",
        ).pack(side="left")

    # -- 交互 ------------------------------------------------------------- #

    def _current_level(self) -> str:
        for label, key in LEVEL_CHOICES:
            if label == self._level.get():
                return key
        return "info"

    def _apply_filter(self) -> None:
        self.view.set_filter(min_level=self._current_level(), keyword=self._kw.get())
        self._refresh_status()

    def _on_pause(self) -> None:
        paused = bool(self._paused.get())
        self.view.set_paused(paused)
        if not paused:
            # 暂停期间事件仍在缓冲里攒着，恢复时一次性重画补齐，而不是丢掉
            self.view.rerender()
            self._refresh_status()
        events.info("日志已暂停显示" if paused else "日志已恢复显示", kind="ui")

    def _clear(self) -> None:
        self.view.clear()
        self._refresh_status()

    def _export(self) -> None:
        default = f"voice-ctl-log-{W.human_time().replace(':', '').replace(' ', '-')}.txt"
        path = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="导出日志",
            initialfile=default,
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("全部文件", "*.*")],
        )
        if not path:
            return
        try:
            n = self.view.export(path)
        except OSError as e:
            events.error(f"导出失败：{e}", kind="ui")
            return
        events.ok(f"已导出 {n} 条日志 → {path}", kind="ui")

    def _open_file(self) -> None:
        path = bootstrap.log_path()
        if not path.is_file():
            events.warn(f"日志文件还没生成：{path}", kind="ui")
            return
        W.open_folder(str(path))
        events.info(f"已用默认程序打开 {path}", kind="ui")

    # -- 事件泵 ----------------------------------------------------------- #

    def feed(self, incoming: list[events.Event]) -> None:
        """由主窗口的轮询循环喂进来——只有一个消费者，游标才不会打架。"""
        self.view.feed(incoming)

    def _refresh_status(self) -> None:
        shown = int(self.view.text.index("end-1c").split(".")[0]) - 1
        parts = [f"缓冲 {self.view.count} 条，当前显示 {max(0, shown)} 条"]
        dropped = self.app.bus.dropped
        if dropped:
            parts.append(f"（缓冲上限已挤掉 {dropped} 条旧事件）")
        parts.append(f"· 日志文件 {bootstrap.log_path()}")
        self._status.set("  ".join(parts))

    def on_show(self) -> None:
        self.app.clear_log_badge()
        self._refresh_status()

    def on_tick(self) -> None:
        self._refresh_status()
