"""「关于」页：版本、路径、依赖、完整体检。出问题时用户来这里复制诊断信息。"""

from __future__ import annotations

import contextlib
import io
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from .. import __version__, bootstrap, events
from . import widgets as W
from .theme import PALETTE as P
from .theme import S

DEPS = [
    ("numpy", "音频处理", True),
    ("sherpa_onnx", "语音识别（含原生 DLL）", True),
    ("sounddevice", "录音（含 PortAudio DLL）", True),
    ("pynput", "全局热键", True),
    ("pypinyin", "中文同音纠错", False),
    ("huggingface_hub", "下载语义层权重", False),
    ("onnxruntime", "语义层推理后端", False),
]

REPO = "https://github.com/xubuchen520/voice-ctl"


class AboutTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["bg"])
        self.app = app
        self._doctor_out = ""
        self._doctor_ready = False
        self._deps_pending: list[tuple[str, str, bool, str]] | None = None
        self._build()

    def _build(self) -> None:
        wrap = W.ScrollFrame(self)
        wrap.pack(fill="both", expand=True)
        root = tk.Frame(wrap.inner, bg=P["bg"])
        root.pack(fill="both", expand=True, padx=S(18), pady=S(18))

        c1 = W.Card(root, title="版本")
        c1.pack(fill="x")
        head = tk.Frame(c1.body, bg=P["surface"])
        head.pack(fill="x")
        tk.Label(head, text=f"voice-ctl {__version__}", bg=P["surface"], fg=P["text"],
                 font=W.theme.FONTS["subtitle"]).pack(side="left")
        tk.Label(
            head, text="  按住热键说话 → 本地离线识别 → 匹配动作 → 执行",
            bg=P["surface"], fg=P["faint"], font=W.theme.FONTS["tiny"],
        ).pack(side="left")
        W.hint(
            c1.body,
            "纯本地离线：识别和判断都在你这台机器上跑，不联网、零调用成本，"
            "说的话不会离开这台电脑。",
        ).pack(fill="x", pady=(S(6), 0))

        # --- 路径 ---------------------------------------------------------
        c2 = W.Card(root, title="路径")
        c2.pack(fill="x", pady=(S(14), 0))
        self._paths = tk.Text(
            c2.body, bg=P["surface2"], fg=P["text"], relief="flat",
            highlightthickness=1, highlightbackground=P["border"],
            padx=S(10), pady=S(8), wrap="none", font=W.theme.FONTS["mono_small"],
            height=7, state="disabled",
        )
        self._paths.pack(fill="x")

        row2 = tk.Frame(c2.body, bg=P["surface"])
        row2.pack(fill="x", pady=(S(8), 0))
        ttk.Button(row2, text="打开数据目录",
                   command=lambda: W.open_folder(str(bootstrap.data_dir()))).pack(side="left")
        ttk.Button(row2, text="打开日志文件",
                   command=lambda: W.open_folder(str(bootstrap.log_path()))).pack(
            side="left", padx=S(8)
        )
        ttk.Button(row2, text="复制路径", command=self._copy_paths).pack(side="left")

        # --- 依赖 ---------------------------------------------------------
        c3 = W.Card(root, title="依赖")
        c3.pack(fill="x", pady=(S(14), 0))
        self._dep_box = tk.Frame(c3.body, bg=P["surface"])
        self._dep_box.pack(fill="x")

        # --- 体检 ---------------------------------------------------------
        c4 = W.Card(root, title="完整体检")
        c4.pack(fill="both", expand=True, pady=(S(14), 0))
        row4 = tk.Frame(c4.body, bg=P["surface"])
        row4.pack(fill="x")
        ttk.Button(row4, text="运行体检", style="Primary.TButton",
                   command=self.run_doctor).pack(side="left")
        ttk.Button(row4, text="复制诊断信息", command=self._copy_doctor).pack(side="left", padx=S(8))
        ttk.Button(row4, text="打开项目主页",
                   command=lambda: W.open_url(REPO)).pack(side="right")

        self._doctor = tk.Text(
            c4.body, bg=P["surface2"], fg=P["text"], relief="flat",
            highlightthickness=1, highlightbackground=P["border"],
            padx=S(10), pady=S(8), wrap="word", font=W.theme.FONTS["mono_small"],
            height=12, state="disabled",
        )
        self._doctor.pack(fill="both", expand=True, pady=(S(10), 0))
        self._write_doctor("点上面的「运行体检」，会把模型、配置、动作、麦克风逐项检查一遍。")

    # -- 数据 ------------------------------------------------------------- #

    def _refresh_paths(self) -> None:
        cfg = self.app.cfg
        lines = [
            f"运行方式    {'打包 exe' if bootstrap.is_frozen() else '源码 / venv'}",
            f"可执行文件  {bootstrap.exe_dir()}",
            f"可写数据    {bootstrap.data_dir()}",
            f"配置文件    {self.app.config_path or '（没有，保存设置时会自动生成）'}",
            f"模型目录    {self.app.model_dir()}",
            f"语义层权重  {cfg.decision_path()}",
            f"运行日志    {bootstrap.log_path()}",
        ]
        self._paths.configure(state="normal")
        self._paths.delete("1.0", "end")
        self._paths.insert("end", "\n".join(lines))
        self._paths.configure(state="disabled")

    def _refresh_deps(self) -> None:
        if self._deps_pending is None:
            return
        for w in self._dep_box.winfo_children():
            w.destroy()
        for name, why, required, got in self._deps_pending:
            row = tk.Frame(self._dep_box, bg=P["surface"])
            row.pack(fill="x", pady=S(1))
            ok = not got.startswith("缺少") and not got.startswith("导入失败")
            if name == "pypinyin" and not ok:
                color, mark = P["muted"], "·"
            else:
                color, mark = (P["ok"], "✓") if ok else (P["error"], "✗")
            tk.Label(row, text=mark, bg=P["surface"], fg=color, font=W.theme.FONTS["small"],
                     width=2).pack(side="left")
            tk.Label(row, text=name, bg=P["surface"], fg=P["text"],
                     font=W.theme.FONTS["mono_small"], width=18, anchor="w").pack(side="left")
            tk.Label(row, text=got, bg=P["surface"], fg=P["muted"],
                     font=W.theme.FONTS["mono_small"], width=14, anchor="w").pack(side="left")
            tk.Label(row, text=why + ("" if required else "（可选）"), bg=P["surface"],
                     fg=P["faint"], font=W.theme.FONTS["tiny"], anchor="w").pack(side="left")
        self._deps_pending = None

    def scan_deps(self) -> None:
        out: list[tuple[str, str, bool, str]] = []
        for name, why, required in DEPS:
            try:
                mod = __import__(name)
                ver = getattr(mod, "__version__", "")
                out.append((name, why, required, str(ver) or "已安装"))
            except Exception as e:  # noqa: BLE001
                out.append((name, why, required, f"缺少（{type(e).__name__}）"))
        self._deps_pending = out

    # -- 体检 ------------------------------------------------------------- #

    def run_doctor(self) -> None:
        if self._doctor_ready:
            return
        self._doctor_ready = False
        self._doctor_out = ""
        self._write_doctor("正在体检 …（加载模型要几秒）")

        def work() -> None:
            from ..cli import build_parser, cmd_doctor

            buf = io.StringIO()
            args = build_parser().parse_args(["doctor"])
            try:
                with contextlib.redirect_stdout(buf):
                    cmd_doctor(args)
            except Exception as e:  # noqa: BLE001
                buf.write(f"\n体检本身出错了：{type(e).__name__}: {e}\n")
            # 只往槽里放结果，不碰 Tk —— 见 window.py 顶部的线程规则
            self._doctor_out = buf.getvalue()
            self._doctor_ready = True

        threading.Thread(target=work, name="voice-ctl-doctor", daemon=True).start()

    def _write_doctor(self, text: str) -> None:
        self._doctor.configure(state="normal")
        self._doctor.delete("1.0", "end")
        self._doctor.insert("end", text)
        self._doctor.configure(state="disabled")

    def _copy_paths(self) -> None:
        W.copy_to_clipboard(self.app.root, self._paths.get("1.0", "end").strip())

    def _copy_doctor(self) -> None:
        body = self._doctor_out or self._doctor.get("1.0", "end").strip()
        header = (
            f"voice-ctl {__version__}\n"
            f"{bootstrap.describe()}\n"
            f"配置文件: {self.app.config_path}\n"
            f"{'-' * 60}\n"
        )
        W.copy_to_clipboard(self.app.root, header + body)

    # -- 刷新 ------------------------------------------------------------- #

    def on_show(self) -> None:
        self._refresh_paths()
        self.scan_deps()

    def on_tick(self) -> None:
        self._refresh_deps()
        if self._doctor_ready:
            self._doctor_ready = False
            self._write_doctor(self._doctor_out)
