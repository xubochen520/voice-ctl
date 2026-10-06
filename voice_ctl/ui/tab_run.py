"""「运行」页：一眼看状态，顺手试一句。

页面的核心是顶上那块深色「屏幕」：
    左上   状态大字（待命 / 正在听 / 加载中 / 出错）和一行说明
    右上   热键画成键帽——按住热键说话时，屏幕上的键帽也真的按下去
    中间   点阵电平——录音时是一条滚动的橙色声波，出结果时涟漪一圈
    底部   最近一句话 + 结果；以及一个输入框，打字就能走一遍完整链路

以前这里「有没有在录」只能靠 120ms 一次的轮询去猜；现在按下的那一瞬间，键帽、大字、
电平三处同时变，用户不用再靠反复点「启动监听」来确认。
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Any

from .. import events
from . import widgets as W
from .theme import FONTS
from .theme import PALETTE as P
from .theme import S

FAST_MODES = ("rec", "busy", "flash")
"""这些模式下电平表要 ~30fps 地刷；其余模式由 120ms 的页面轮询顺带刷就够了。"""


class RunTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["chassis"])
        self.app = app
        self._model_present: bool | None = None
        self._seen: Any = None
        """上一次画进结果行的 Outcome（按身份比较）——换了才闪一下涟漪。"""
        self._prev_live = False
        self._busy_until = 0.0
        self._busy_base = 0
        self._fast: str | None = None
        self._last_head = ("", "")
        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        scroll = W.ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        wrap = tk.Frame(scroll.inner, bg=P["chassis"])
        wrap.pack(fill="both", expand=True, padx=S(28), pady=S(26))
        scr = P["scr"]

        # --- 屏幕 ---------------------------------------------------------
        self._screen = W.Panel(wrap, radius=22, fill=scr, border=P["scr_line"], pad=24)
        self._screen.pack(fill="x")
        b = self._screen.body

        top = tk.Frame(b, bg=scr)
        top.pack(fill="x")
        self._caps = W.KeyCaps(top, self.app.engine.hotkey_spec, size="md", dark=True, bg=scr)
        self._caps.pack(side="right", anchor="n", pady=(S(8), 0))
        left = tk.Frame(top, bg=scr)
        left.pack(side="left", fill="x", expand=True)
        self._headline = tk.Label(left, text="未启动", bg=scr, fg=P["scr_ink"], font=FONTS["hero"], anchor="w")
        self._headline.pack(anchor="w")
        self._sub = W.wrap_label(left, "", font="note", fg=P["scr_dim"], bg=scr)
        self._sub.pack(fill="x", pady=(S(2), 0))

        self.meter = W.DotMeter(b, bg=scr)
        self.meter.pack(fill="x", pady=(S(14), S(12)))

        res = tk.Frame(b, bg=scr)
        res.pack(fill="x")
        chips = tk.Frame(res, bg=scr)
        chips.pack(side="right", anchor="n")
        self._ms = tk.Label(chips, text="", bg=scr, fg=P["scr_dim"], font=FONTS["digits"])
        self._ms.pack(side="right", padx=(S(10), 0))
        self._chip_act = W.StatusPill(chips, "", tone="muted", dark=True)
        self._chip_res = W.StatusPill(chips, "", tone="muted", dark=True)
        self._heard = W.wrap_label(res, "", font="lead", fg=P["scr_ink"], bg=scr)
        self._heard.pack(side="left", fill="x", expand=True, padx=(0, S(16)))
        self._set_heard(None)

        line = tk.Frame(b, bg=scr)
        line.pack(fill="x", pady=(S(18), 0))
        self._entry = W.Field(line, dark=True, placeholder="打字试一句，回车发送 …")
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.entry.bind("<Return>", lambda _e: self._send())
        W.Button(line, "发送", kind="light", icon="send", command=self._send, width=84).pack(
            side="left", padx=(S(8), 0))

        opt = tk.Frame(b, bg=scr)
        opt.pack(fill="x", pady=(S(12), 0))
        self._dry = tk.BooleanVar(value=self.app.engine.dry_run)
        W.Switch(opt, "只预演，不真的执行", self._dry, bg=scr, dark=True, font="note",
                 command=self._on_dry).pack(side="left")
        W.wrap_label(
            b,
            "打字走的是和说话完全相同的链路（归一化 → 匹配 → 语义层 → 执行），只是跳过了录音和识别。",
            font="caption", fg=P["scr_faint"], bg=scr,
        ).pack(fill="x", pady=(S(6), 0))

        # --- 控制条 -------------------------------------------------------
        # Flow：窗口窄时胶囊自动换到下一行，而不是被裁掉
        self._ctl = W.Flow(wrap, hgap=8, vgap=10)
        self._ctl.pack(fill="x", pady=(S(18), 0))
        ctl = self._ctl
        self._toggle = W.Button(ctl, "开始监听", kind="primary", icon="play", width=132,
                                command=self.app.toggle_engine)
        self._load_btn = W.Button(ctl, "加载识别模型", icon="download", command=self.app.load_model)
        self._reload_btn = W.Button(ctl, "重新加载配置", kind="ghost", icon="refresh",
                                    command=self.app.reload_config)
        self._model_pill = W.StatusPill(ctl, "模型未检查", tone="muted")
        self._action_pill = W.StatusPill(ctl, "动作 —", tone="muted")
        self._check_btn = W.Button(ctl, "体检", kind="ghost", icon="shield",
                                   command=lambda: self.app.show_tab("about"))
        for w in (self._toggle, self._load_btn, self._reload_btn, self._model_pill, self._action_pill):
            ctl.add(w)
        ctl.add(self._check_btn, right=True)
        self._model_pill.bind("<Button-1>", lambda _e: self._on_model_pill())

        # --- 最近一次 -----------------------------------------------------
        card = W.Card(wrap, title="最近一次")
        card.pack(fill="both", expand=True, pady=(S(18), 0))
        self._stat_vals: dict[str, tk.Label] = {}
        stats = tk.Frame(card.actions, bg=card.fill)
        stats.pack()
        for key, text in (("outcomes", "识别"), ("ok", "成功"), ("failed", "失败"), ("no_match", "未命中")):
            cell = tk.Frame(stats, bg=card.fill)
            cell.pack(side="left", padx=(S(16), 0))
            tk.Label(cell, text=text, bg=card.fill, fg=P["ink3"], font=FONTS["note"]).pack(side="left")
            val = tk.Label(cell, text="0", bg=card.fill, fg=P["ink"], font=FONTS["digits"])
            val.pack(side="left", padx=(S(6), 0))
            self._stat_vals[key] = val

        well = W.Panel(card.body, radius=14, fill=P["scr"], border=None, pad=14)
        well.pack(fill="both", expand=True)
        self._report = tk.Text(
            well.body, bg=P["scr"], fg=P["scr_ink"], relief="flat", bd=0, highlightthickness=0, wrap="word",
            font=FONTS["mono_s"], height=6, state="disabled", spacing1=S(2), spacing3=S(2),
            insertbackground=P["scr_ink"], padx=0, pady=0, cursor="arrow",
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

    def _on_model_pill(self) -> None:
        if not self.app.engine.model_loaded and not self._model_files_present():
            self.app.show_tab("settings")

    # -- 刷新 ------------------------------------------------------------- #

    def on_show(self) -> None:
        self._caps.set_spec(self.app.engine.hotkey_spec)
        self._refresh(force=True)

    def on_tick(self) -> None:
        self._refresh()
        if self.meter.mode not in FAST_MODES and self.winfo_viewable():
            self.meter.step()

    def _model_files_present(self) -> bool:
        """模型文件在不在。每 120ms 轮询一次，所以结果缓存住。"""
        if self._model_present is None:
            try:
                md = self.app.model_dir()
                self._model_present = (md / "model.int8.onnx").is_file() and (md / "tokens.txt").is_file()
            except Exception:  # noqa: BLE001
                self._model_present = False
        return self._model_present

    def _set_heard(self, out: Any) -> None:
        """屏幕底部那一行：听到了什么 + 结果。"""
        if out is None:
            self._heard.configure(text="还没有记录。按住热键说一句，或在下面打字试试。", fg=P["scr_faint"],
                                  font=FONTS["note"])
            for chip in (self._chip_res, self._chip_act):
                chip.pack_forget()
            self._ms.configure(text="")
            return
        self._heard.configure(text=f"“{out.text}”" if out.text else "（没听到内容）",
                              fg=P["scr_ink"] if out.text else P["scr_dim"], font=FONTS["lead"])
        if out.ok:
            kind, label = "ok", "已执行"
        elif out.action_id:
            kind, label = "error", "失败"
        else:
            kind, label = "warn", "未命中"
        self._chip_res.set(label, kind)
        self._chip_res.pack(side="right", padx=(S(8), 0))
        if out.action_id:
            self._chip_act.set(out.action_id, "muted")
            self._chip_act.pack(side="right", padx=(S(8), 0))
        else:
            self._chip_act.pack_forget()
        self._ms.configure(text=f"{out.timing.total_ms:.0f} ms" if out.timing.total_ms else "")

    def _head(self, headline: str, sub: str, color: str) -> None:
        if (headline, sub) != self._last_head:
            self._last_head = (headline, sub)
            self._headline.configure(text=headline, fg=color)
            self._sub.configure(text=sub)
        else:
            self._headline.configure(fg=color)

    def _refresh(self, *, force: bool = False) -> None:
        eng = self.app.engine
        live, ms, level = eng.capture_state()
        live = bool(live and eng.running)
        spec = W.hotkey_text(eng.hotkey_spec)
        now = time.monotonic()

        # 松手后到结果出来之间的"识别中"：没结果也没被门拦下就最多等 2 秒
        ss = eng.session_stats
        gate_count = ss.gated + ss.too_short
        if self._prev_live and not live:
            self._busy_until = now + 2.0
            self._busy_base = gate_count
        self._prev_live = live
        if gate_count != self._busy_base:
            self._busy_until = 0.0  # 被静音门/太短挡掉了，不会有结果

        out = eng.last_outcome
        fresh = out is not None and out is not self._seen
        if fresh:
            self._seen = out
            self._busy_until = 0.0
            self._set_heard(out)
            self.meter.flash("ok" if out.ok else ("fail" if out.action_id else "nomatch"))

        # 状态 → 大字 / 说明 / 电平表模式
        if eng.state == "error":
            self._head("出错", eng.error[:160] or "引擎出错，详见日志页", P["err_lit"])
            mode = "off"
        elif eng.state == "loading":
            self._head("加载中", "正在加载识别模型（几秒钟，界面不会卡）", P["warn_lit"])
            mode = "busy"
        elif eng.running:
            if live:
                self._head("正在听", f"已录 {ms / 1000:.1f}s · 松手即识别", P["live"])
                mode = "rec"
            elif now < self._busy_until:
                self._head("识别中", "松手了，正在识别 …", P["teal_lit"])
                mode = "busy"
            else:
                self._head("待命", f"按住 {spec} 说话，松手执行", P["scr_ink"])
                mode = "ready"
        else:
            self._head("未启动", f"点「开始监听」，然后按住 {spec} 说话", P["scr_dim"])
            mode = "off"

        self._caps.set_spec(eng.hotkey_spec)
        self._caps.set_pressed(live)
        if self.meter.mode == "flash" and not self.meter.flash_done and mode in ("ready", "off"):
            pass  # 涟漪还没放完，别被"待命"打断
        elif mode != self.meter.mode:
            self.meter.set_mode(mode)
        if self.meter.mode in FAST_MODES and self._fast is None and self.winfo_viewable():
            self._fast = self.after(33, self._fast_tick)
        if live and self._fast is None:
            # 30fps 的快速循环没在跑（窗口不可见、或刚进入录音）：自己送一个样本并画出来
            self.meter.push(level)
            self.meter.step()

        # 按钮
        if eng.running:
            self._toggle.set(text="停止监听", icon="stop", kind="secondary")
        else:
            self._toggle.set(text="开始监听", icon="play", kind="primary")
        self._toggle.set_enabled(eng.state != "loading")

        n, total = len(eng.actions), len(eng.cfg.actions)
        self._action_pill.set(
            f"{n} 个动作" + (f"（{total - n} 个已停用）" if total != n else ""),
            "info" if n else "error",
        )

        if eng.model_loaded:
            self._model_pill.set(f"模型已加载 {eng.stats.model_load_ms:.0f}ms", "ok")
            self._ctl.set_visible(self._load_btn, False)
        else:
            # 「没加载」和「没有」要分清楚。完整版内置了模型，只是懒加载——
            # 这里如果一律显示警告色的"未加载"，用户会以为缺模型，白白去下 226MB。
            self._ctl.set_visible(self._load_btn, True)
            self._load_btn.set_enabled(eng.state != "loading")
            if self._model_files_present():
                self._model_pill.set("模型待加载，点启动时会载入", "muted")
            else:
                self._model_pill.set("缺少模型，去「设置」页下载", "warn")
        self._model_pill.configure(cursor="hand2" if (not eng.model_loaded and not self._model_files_present()) else "")

        s = eng.stats
        for key, lbl in self._stat_vals.items():
            txt = str(getattr(s, key))
            if lbl.cget("text") != txt:
                lbl.configure(text=txt)

        if fresh or force:
            self._report.configure(state="normal")
            self._report.delete("1.0", "end")
            if out is not None:
                self._report.insert("end", out.report(verbose=True))
                self._report.configure(
                    fg=P["ok_lit"] if out.ok else (P["err_lit"] if out.action_id else P["warn_lit"])
                )
            else:
                self._report.insert("end", "还没有记录。按住热键说一句，或在上面输入一句话试试。")
                self._report.configure(fg=P["scr_faint"])
            self._report.configure(state="disabled")

    def _fast_tick(self) -> None:
        """录音 / 识别 / 涟漪期间的 30fps 刷新。一旦回到慢速模式就自己停。"""
        self._fast = None
        try:
            if self.meter.mode not in FAST_MODES or not self.winfo_viewable():
                return
            if self.meter.mode == "rec":
                self.meter.push(self.app.engine.capture_state()[2])
            self.meter.step()
            self._fast = self.after(33, self._fast_tick)
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            self._fast = None
