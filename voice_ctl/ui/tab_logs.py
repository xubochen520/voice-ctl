"""「日志」页：实时事件流 + 过滤 + 导出。"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog
from typing import Any

from .. import bootstrap, events
from . import widgets as W
from .theme import FONTS
from .theme import PALETTE as P
from .theme import S

# (内部取值, 屏幕上的标签)。取值沿用旧的完整名字：配置/测试里都是用它们去 set 的。
LEVEL_CHOICES = [
    ("全部（含调试）", "debug", "全部"),
    ("常规", "info", "常规"),
    ("只看成功", "ok", "成功"),
    ("只看问题（警告+错误）", "warn", "问题"),
    ("只看错误", "error", "错误"),
]


class LogsTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["chassis"])
        self.app = app
        self._kw = tk.StringVar()
        self._paused = tk.BooleanVar(value=False)
        self._autoscroll = tk.BooleanVar(value=True)
        self._level = tk.StringVar(value="常规")
        self._status = tk.StringVar(value="")
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        wrap = tk.Frame(self, bg=P["chassis"])
        wrap.pack(fill="both", expand=True, padx=S(28), pady=(S(22), S(16)))

        bar = tk.Frame(wrap, bg=P["chassis"])
        bar.pack(fill="x")
        seg = W.Segmented(bar, [(value, short) for value, _key, short in LEVEL_CHOICES], self._level,
                          command=self._apply_filter)
        seg.pack(side="left")
        W.Button(bar, "清空", kind="ghost", icon="trash", size="sm", command=self._clear).pack(side="right")
        W.Button(bar, "导出", kind="ghost", icon="download", size="sm", command=self._export).pack(
            side="right", padx=(0, S(2)))
        self._sw_scroll = W.Switch(bar, "自动滚动", self._autoscroll, font="note",
                                   command=lambda: self.view.set_autoscroll(bool(self._autoscroll.get())))
        self._sw_scroll.pack(side="right", padx=(0, S(14)))
        W.Switch(bar, "暂停", self._paused, font="note", command=self._on_pause).pack(side="right", padx=(0, S(14)))
        search = W.Field(bar, textvariable=self._kw, placeholder="搜索日志", icon="search", height=34)
        search.pack(side="left", fill="x", expand=True, padx=S(14))
        search.entry.bind("<KeyRelease>", lambda _e: self._apply_filter())

        screen = W.Panel(wrap, radius=18, fill=P["scr"], border=P["scr_line"], pad=8)
        screen.pack(fill="both", expand=True, pady=(S(14), 0))
        self.view = W.LogView(screen.body, on_autoscroll=self._autoscroll_off)
        self.view.pack(fill="both", expand=True)

        foot = tk.Frame(wrap, bg=P["chassis"])
        foot.pack(fill="x", pady=(S(10), 0))
        tk.Label(foot, textvariable=self._status, bg=P["chassis"], fg=P["ink3"], font=FONTS["note"],
                 anchor="w").pack(side="left")
        W.Button(foot, "打开日志文件", kind="ghost", icon="file", size="sm", command=self._open_file,
                 tooltip=str(bootstrap.log_path())).pack(side="right")

    # -- 交互 ------------------------------------------------------------- #

    def _current_level(self) -> str:
        for label, key, _short in LEVEL_CHOICES:
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

    def _autoscroll_off(self, _on: bool) -> None:
        """用户往回翻了日志，视图自己停掉了自动滚动——开关得跟着显示出来。"""
        self._autoscroll.set(False)

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
        text = f"缓冲 {self.view.count} 条，当前显示 {self.view.shown} 条"
        dropped = self.app.bus.dropped
        if dropped:
            text += f"（缓冲上限已挤掉 {dropped} 条旧事件）"
        if self._status.get() != text:
            self._status.set(text)

    def on_show(self) -> None:
        self.app.clear_log_badge()
        self._refresh_status()

    def on_tick(self) -> None:
        self._refresh_status()
