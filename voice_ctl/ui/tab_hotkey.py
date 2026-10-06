"""「快捷键」页：录制、校验、保存热键，以及按键时长与提示音。"""

from __future__ import annotations

import tkinter as tk
from typing import Any

from .. import events
from ..recorder import play_beep
from . import widgets as W
from .theme import PALETTE as P
from .theme import S

# 这些组合键系统自己占了，钩子根本收不到。提前说清楚，省得用户配完
# 发现"没反应"却以为是程序坏了。
# 键是归一化后的形式（小写、去掉尖括号、按字母序），和 _canon() 的输出对齐。
RESERVED: dict[str, str] = {
    "alt+ctrl+delete": "Ctrl+Alt+Del 是安全注意序列，任何程序都截不到。",
    "alt+f4": "Alt+F4 是关闭当前窗口，被系统占用。",
    "alt+tab": "Alt+Tab 是切换窗口，被系统占用。",
    "ctrl+esc": "Ctrl+Esc 是打开开始菜单，被系统占用。",
    "cmd+l": "Win+L 是锁定屏幕，被系统占用。",
    "cmd+d": "Win+D 是显示桌面，被系统占用。",
    "cmd+tab": "Win+Tab 是任务视图，被系统占用。",
    "ctrl+space": "纯 Ctrl+Space 在中文 Windows 上是输入法切换键，会被系统抢走。加个 Alt 就好。",
}

MODS = {"ctrl", "alt", "shift", "cmd"}

# 级别：BLOCK = 存了也用不了，直接拒绝；WARN = 能存，但大概不好用
BLOCK = "block"
WARN = "warn"


def _canon(spec: str) -> str:
    """把热键归一成可比较的形式：'<ctrl>+<alt>+Space' → 'alt+ctrl+space'。"""
    parts = [p.strip().strip("<>").lower() for p in spec.split("+") if p.strip()]
    return "+".join(sorted(parts))


def warnings_for(spec: str) -> list[tuple[str, str]]:
    """给一个热键挑毛病。空列表 = 看起来没问题。"""
    if not spec.strip():
        return [(BLOCK, "热键不能为空。")]
    msgs: list[tuple[str, str]] = []
    norm = _canon(spec)
    if norm in RESERVED:
        msgs.append((BLOCK, RESERVED[norm]))
    plain = [p for p in norm.split("+") if p]
    if plain and all(p in MODS for p in plain):
        msgs.append((BLOCK, "只有修饰键当不了热键——至少要有一个普通键（字母、空格、F1-F12 等）。"))
    if len(plain) > 3:
        msgs.append((WARN, f"按了 {len(plain)} 个键，太多了：单手很难同时按住，建议最多 3 个。"))
    return msgs


class HotkeyTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["chassis"])
        self.app = app
        self._pending: str | None = None
        self._was_running = False

        self._min_ms = tk.IntVar()
        self._max_ms = tk.IntVar()
        self._beep = tk.BooleanVar()
        self._beep_start = tk.IntVar()
        self._beep_end = tk.IntVar()
        self._beep_ms = tk.IntVar()
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        wrap = W.ScrollFrame(self)
        wrap.pack(fill="both", expand=True)
        root = tk.Frame(wrap.inner, bg=P["chassis"])
        root.pack(fill="both", expand=True, padx=S(28), pady=S(26))

        # --- 录制 ---------------------------------------------------------
        card = W.Card(root, title="全局热键（按住说话）")
        card.pack(fill="x")
        b = card.body

        self.capture = W.HotkeyCapture(
            b,
            on_capture=self._on_captured,
            on_capture_start=self._on_capture_start,
            on_capture_end=self._on_capture_end,
        )
        self.capture.pack(fill="x", pady=(S(8), 0))
        W.hint(
            b,
            "点上面的框，然后按下想用的组合键。松手即录入，Esc 取消。\n"
            "录入期间会自动暂停监听——否则你按到当前热键就会触发一次录音。",
        ).pack(fill="x", pady=(S(12), 0))

        self._msg = W.Callout(b, "", "info", gap=(12, 0))
        self._msg.pack(fill="x")

        row = tk.Frame(b, bg=card.fill)
        row.pack(fill="x", pady=(S(16), 0))
        # 变量名别叫 _save：那会把下面的 _save() 方法覆盖成 Button，
        # 之后 self._save() 就成了 "Button is not callable"
        self._save_btn = W.Button(row, "保存并立即生效", kind="primary", icon="check", command=self._save)
        self._save_btn.pack(side="left")
        W.Button(row, "放弃修改", command=self._reset).pack(side="left", padx=S(8))
        self._dirty_pill = W.StatusPill(row, "有未保存的修改", tone="warn")

        # --- 时长 ---------------------------------------------------------
        card2 = W.Card(root, title="按键行为")
        card2.pack(fill="x", pady=(S(18), 0))
        slot = card2.row("最短时长", "按键短于这个时长就当误触，不做识别。手快的人可以调到 120ms 左右。")
        W.Stepper(slot, variable=self._min_ms, from_=0, to=2000, step=50, unit="ms").pack()
        slot = card2.row("最长时长", "到点强制停止录音，防止按键卡住（比如被别的程序抢了松开事件）导致无限录。")
        W.Stepper(slot, variable=self._max_ms, from_=1000, to=60000, step=1000, unit="ms").pack()
        foot2 = tk.Frame(card2.body, bg=card2.fill)
        foot2.pack(fill="x", pady=(S(4), 0))
        W.Button(foot2, "保存这两项", command=self._save_timing).pack(side="right")

        # --- 提示音 -------------------------------------------------------
        card3 = W.Card(root, title="提示音")
        card3.pack(fill="x", pady=(S(18), 0))
        slot = card3.row("开始 / 结束录音时响一声", "听到「嘀」再开口，就知道麦克风已经开了。")
        W.Switch(slot, "", self._beep).pack()
        slot = card3.row("起始频率", "Hz。开始录音那一声。")
        W.Stepper(slot, variable=self._beep_start, from_=200, to=3000, step=20, unit="Hz").pack()
        slot = card3.row("结束频率", "Hz。结束那一声。比起始高一点，更符合「说完了」的直觉。")
        W.Stepper(slot, variable=self._beep_end, from_=200, to=3000, step=20, unit="Hz").pack()
        slot = card3.row("时长", "毫秒。太长会盖住你说话的开头。")
        W.Stepper(slot, variable=self._beep_ms, from_=20, to=500, step=10, unit="ms").pack()
        foot3 = tk.Frame(card3.body, bg=card3.fill)
        foot3.pack(fill="x", pady=(S(4), 0))
        W.Button(foot3, "保存提示音设置", command=self._save_feedback).pack(side="right")
        W.Button(foot3, "试听", kind="ghost", icon="speaker", command=self._preview).pack(side="right", padx=(0, S(8)))

        W.hint(
            root,
            "热键是全局的：任何程序前台时都能用。它靠 Windows 低级键盘钩子实现，"
            "所以偶尔会和同样用钩子的程序（部分录屏、远程控制软件）冲突——"
            "症状是热键时灵时不灵，换个组合键通常就好了。",
            font="caption",
        ).pack(fill="x", pady=(S(18), 0))

    # -- 录制回调 --------------------------------------------------------- #

    def _on_capture_start(self) -> None:
        eng = self.app.engine
        self._was_running = eng.running
        if self._was_running:
            eng.stop()
            events.info("录制热键中，已暂停监听", kind="hotkey")

    def _on_capture_end(self) -> None:
        if self._was_running:
            self._was_running = False
            self.app.engine.start()

    def _show_warnings(self, msgs: list[tuple[str, str]]) -> None:
        if not msgs:
            self._msg.set("", "info")
            return
        worst = BLOCK if any(lv == BLOCK for lv, _ in msgs) else WARN
        body = "\n".join(m for _, m in msgs)
        if worst == BLOCK:
            self._msg.set("这组键用不了：" + body, "error")
        else:
            self._msg.set(body, "warn")

    def _set_dirty(self, dirty: bool) -> None:
        if dirty:
            self._dirty_pill.pack(side="left", padx=S(8))
        else:
            self._dirty_pill.pack_forget()

    def _on_captured(self, spec: str) -> None:
        self._pending = spec
        msgs = warnings_for(spec)
        self._show_warnings(msgs)
        if not msgs:
            self._msg.set("这个组合看起来没问题，点「保存并立即生效」。", "ok")
        self._set_dirty(True)

    # -- 保存 ------------------------------------------------------------- #

    def _save(self) -> None:
        spec = self._pending
        if not spec:
            events.warn("还没有录制新的热键。先点上面的框按一组键。", kind="hotkey")
            return
        msgs = warnings_for(spec)
        if any(lv == BLOCK for lv, _ in msgs):
            events.error("这组键系统会抢走，换了才能保存", kind="hotkey")
            self._show_warnings(msgs)
            return
        for _lv, m in msgs:
            events.warn(m, kind="hotkey")
        if self.app.save_settings({("hotkey", "keys"): spec}, note=f"热键改为 {spec}"):
            self.app.engine.apply_hotkey(spec)
            self._pending = None
            self._set_dirty(False)
            self._msg.set(f"已保存并生效：{W.hotkey_text(spec)}", "ok")
            self.capture.set_spec(spec)

    def _save_timing(self) -> None:
        lo, hi = int(self._min_ms.get()), int(self._max_ms.get())
        if hi <= lo:
            events.error(f"最长时长（{hi}ms）必须大于最短时长（{lo}ms）", kind="hotkey")
            return
        self.app.save_settings(
            {("hotkey", "min_duration_ms"): lo, ("hotkey", "max_duration_ms"): hi},
            note="更新按键时长",
        )
        if self.app.engine.running:
            events.warn("时长改动会在下次启动监听时生效", kind="hotkey")

    def _save_feedback(self) -> None:
        self.app.save_settings(
            {
                ("feedback", "beep"): bool(self._beep.get()),
                ("feedback", "beep_start_hz"): int(self._beep_start.get()),
                ("feedback", "beep_end_hz"): int(self._beep_end.get()),
                ("feedback", "beep_ms"): int(self._beep_ms.get()),
            },
            note="更新提示音",
        )

    def _preview(self) -> None:
        play_beep(int(self._beep_start.get()), int(self._beep_ms.get()))
        self.after(180, lambda: play_beep(int(self._beep_end.get()), int(self._beep_ms.get())))

    def _reset(self) -> None:
        self._pending = None
        self._set_dirty(False)
        self._msg.set("", "info")
        self.load_from_config()

    # -- 刷新 ------------------------------------------------------------- #

    def load_from_config(self) -> None:
        hk = self.app.cfg.hotkey
        self.capture.set_spec(self.app.engine.hotkey_spec or hk.keys)
        self._min_ms.set(hk.min_duration_ms)
        self._max_ms.set(hk.max_duration_ms)
        fb = self.app.cfg.feedback
        self._beep.set(fb.beep)
        self._beep_start.set(fb.beep_start_hz)
        self._beep_end.set(fb.beep_end_hz)
        self._beep_ms.set(fb.beep_ms)

    def on_show(self) -> None:
        if not self._pending:
            self.load_from_config()
            self._show_warnings(warnings_for(self.app.engine.hotkey_spec))

    def on_tick(self) -> None:
        """热键可能被别处改掉（重新加载配置、命令行改了文件），保持显示同步。"""
        if self._pending:
            return
        spec = self.app.engine.hotkey_spec
        if spec and spec != self.capture.spec:
            self.capture.set_spec(spec)
            self._show_warnings(warnings_for(spec))
