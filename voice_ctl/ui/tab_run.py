"""「运行」页：一眼看状态，顺手试一句。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from .. import events
from . import widgets as W
from .theme import PALETTE as P
from .theme import S


class RunTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["bg"])
        self.app = app
        self._model_pill: W.StatusPill
        self._state_pill: W.StatusPill
        self._action_pill: W.StatusPill
        self._report: tk.Text
        self._model_present: bool | None = None
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        pad = S(18)
        wrap = tk.Frame(self, bg=P["bg"])
        wrap.pack(fill="both", expand=True, padx=pad, pady=pad)

        # --- 状态卡 -------------------------------------------------------
        card = W.Card(wrap, title="状态")
        card.pack(fill="x")
        body = card.body

        row = tk.Frame(body, bg=P["surface"])
        row.pack(fill="x", pady=(0, S(12)))
        self._state_pill = W.StatusPill(row, "未启动", color=P["faint"])
        self._state_pill.pack(side="left", padx=(0, S(20)))
        self._action_pill = W.StatusPill(row, "动作 —", color=P["muted"])
        self._action_pill.pack(side="left", padx=(0, S(20)))
        self._model_pill = W.StatusPill(row, "模型未检查", color=P["muted"])
        self._model_pill.pack(side="left")

        hot = tk.Frame(body, bg=P["surface"])
        hot.pack(fill="x", pady=(0, S(6)))
        tk.Label(hot, text="热键", bg=P["surface"], fg=P["muted"], font=W.theme.FONTS["small"]).pack(
            side="left", padx=(0, S(12))
        )
        self._hot_box = tk.Frame(hot, bg=P["surface"])
        self._hot_box.pack(side="left")
        self._render_hotkey()

        btns = tk.Frame(body, bg=P["surface"])
        btns.pack(fill="x", pady=(S(14), 0))
        self._toggle = ttk.Button(btns, text="启动监听", style="Primary.TButton",
                                  command=self.app.toggle_engine)
        self._toggle.pack(side="left")
        self._load_btn = ttk.Button(btns, text="加载识别模型", command=self.app.load_model)
        self._load_btn.pack(side="left", padx=S(8))
        ttk.Button(btns, text="重新加载配置", command=self.app.reload_config).pack(side="left")
        ttk.Button(btns, text="体检", style="Ghost.TButton",
                   command=lambda: self.app.show_tab("about")).pack(side="right")

        # --- 试一句 -------------------------------------------------------
        card2 = W.Card(wrap, title="试一句")
        card2.pack(fill="x", pady=(S(14), 0))
        b2 = card2.body

        line = tk.Frame(b2, bg=P["surface"])
        line.pack(fill="x")
        self._entry = ttk.Entry(line, font=W.theme.FONTS["body"])
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.bind("<Return>", lambda _e: self._send())
        ttk.Button(line, text="发送", style="Primary.TButton", command=self._send).pack(
            side="left", padx=(S(8), 0)
        )

        opt = tk.Frame(b2, bg=P["surface"])
        opt.pack(fill="x", pady=(S(8), 0))
        self._dry = tk.BooleanVar(value=self.app.engine.dry_run)
        W.check(opt, "只报告不执行（dry-run）", self._dry, bg=P["surface"],
                command=self._on_dry).pack(side="left")

        W.hint(
            b2,
            "走的是和说话完全相同的链路（归一化 → 匹配 → 语义层 → 执行），"
            "只是跳过了录音和识别。用来验证「我说这句话会不会命中」。",
        ).pack(fill="x", pady=(S(6), 0))

        # --- 最近一次 -----------------------------------------------------
        card3 = W.Card(wrap, title="最近一次")
        card3.pack(fill="both", expand=True, pady=(S(14), 0))
        self._report = tk.Text(
            card3.body,
            bg=P["surface2"],
            fg=P["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=P["border"],
            padx=S(10),
            pady=S(8),
            wrap="word",
            font=W.theme.FONTS["mono"],
            height=6,
            state="disabled",
        )
        self._report.pack(fill="both", expand=True)

    # -- 交互 ------------------------------------------------------------- #

    def _send(self) -> None:
        text = self._entry.get().strip()
        if not text:
            return
        self._entry.delete(0, "end")
        events.info(f"手动测试：{text}", kind="ui")
        self.app.engine.submit_text(text)

    def _on_dry(self) -> None:
        self.app.engine.dry_run = bool(self._dry.get())
        events.info(
            f"dry-run {'开启' if self._dry.get() else '关闭'}（只报告不执行）", kind="ui"
        )

    # -- 刷新 ------------------------------------------------------------- #

    def on_show(self) -> None:
        self._render_hotkey()
        self._refresh(force=True)

    def on_tick(self) -> None:
        self._refresh()

    def _render_hotkey(self) -> None:
        for w in self._hot_box.winfo_children():
            w.destroy()
        W.key_pills(self._hot_box, self.app.engine.hotkey_spec, bg=P["surface"]).pack()

    def _model_files_present(self) -> bool:
        """模型文件在不在。每 120ms 轮询一次，所以结果缓存住。"""
        if self._model_present is None:
            try:
                md = self.app.model_dir()
                self._model_present = (md / "model.int8.onnx").is_file() and (md / "tokens.txt").is_file()
            except Exception:  # noqa: BLE001
                self._model_present = False
        return self._model_present

    def _refresh(self, *, force: bool = False) -> None:
        eng = self.app.engine
        label = eng.state_label
        if eng.state == "running":
            label = f"运行中 · 按住 {eng.hotkey_spec} 说话"
        self._state_pill.set(label, W.STATE_COLOR.get(eng.state, P["muted"]))
        if eng.state == "running":
            self._toggle.configure(text="停止监听", style="TButton")
        else:
            self._toggle.configure(text="启动监听", style="Primary.TButton")

        n = len(eng.actions)
        total = len(eng.cfg.actions)
        self._action_pill.set(
            f"{n} 个动作" + (f"（共 {total}，{total - n} 个已停用）" if total != n else ""),
            P["accent"] if n else P["error"],
        )

        if eng.model_loaded:
            self._model_pill.set(f"识别模型已加载 {eng.stats.model_load_ms:.0f}ms", P["ok"])
            self._load_btn.state(["disabled"])
        else:
            # 「没加载」和「没有」要分清楚。完整版内置了模型，只是懒加载——
            # 这里如果一律显示橙色的"未加载"，用户会以为缺模型，白白去下 226MB。
            self._load_btn.state(["!disabled"])
            if self._model_files_present():
                self._model_pill.set("识别模型待加载（点启动时会载入）", P["muted"])
            else:
                self._model_pill.set("缺少识别模型，去「设置」页下载", P["warn"])

        out = eng.last_outcome
        if out is not None or force:
            self._report.configure(state="normal")
            self._report.delete("1.0", "end")
            if out is not None:
                self._report.insert("end", out.report(verbose=True))
                self._report.configure(
                    fg=P["ok"] if out.ok else (P["error"] if out.action_id else P["warn"])
                )
            else:
                self._report.insert("end", "还没有记录。按住热键说一句，或在上面输入一句话试试。")
                self._report.configure(fg=P["faint"])
            self._report.configure(state="disabled")
