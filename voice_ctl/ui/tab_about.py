"""「关于」页：版本、路径、依赖、完整体检。出问题时用户来这里复制诊断信息。"""

from __future__ import annotations

import contextlib
import io
import threading
import tkinter as tk
from typing import Any

from .. import __version__, bootstrap
from . import gfx
from . import widgets as W
from .theme import FONTS
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


def _screen_text(master: tk.Misc, *, height: int, wrap: str) -> tuple[W.Panel, tk.Text]:
    """深色屏幕里的只读等宽文本——路径、体检输出都放在这种框里。"""
    box = W.Panel(master, radius=14, fill=P["scr"], border=None, pad=14)
    text = tk.Text(
        box.body, bg=P["scr"], fg=P["scr_ink"], relief="flat", bd=0, highlightthickness=0, wrap=wrap,
        font=FONTS["mono_s"], height=height, state="disabled", spacing1=S(2), spacing3=S(2),
        insertbackground=P["scr_ink"], padx=0, pady=0, cursor="arrow",
        selectbackground=gfx.mix(P["scr"], P["teal_lit"], 0.32), selectforeground=P["scr_ink"],
    )
    text.pack(fill="both", expand=True)
    return box, text


class AboutTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["chassis"])
        self.app = app
        self._doctor_out = ""
        self._doctor_ready = False
        self._deps_pending: list[tuple[str, str, bool, str]] | None = None
        self._build()

    def _build(self) -> None:
        wrap = W.ScrollFrame(self)
        wrap.pack(fill="both", expand=True)
        root = tk.Frame(wrap.inner, bg=P["chassis"])
        root.pack(fill="both", expand=True, padx=S(28), pady=S(26))
        bg = P["chassis"]

        # --- 品牌 ---------------------------------------------------------
        head = tk.Frame(root, bg=bg)
        head.pack(fill="x")
        tk.Label(head, image=gfx.app_icon(self, S(68)), bg=bg, bd=0).pack(side="left")
        text = tk.Frame(head, bg=bg)
        text.pack(side="left", padx=(S(18), 0))
        line = tk.Frame(text, bg=bg)
        line.pack(anchor="w")
        tk.Label(line, text="voice-ctl", bg=bg, fg=P["ink"], font=FONTS["brand_l"]).pack(side="left")
        W.StatusPill(line, __version__, tone="muted").pack(side="left", padx=(S(12), 0), pady=(S(8), 0))
        tk.Label(text, text="按住热键说话 → 本地离线识别 → 匹配动作 → 执行", bg=bg, fg=P["ink3"],
                 font=FONTS["note"]).pack(anchor="w", pady=(S(2), 0))
        W.Callout(
            root,
            "纯本地离线：识别和判断都在你这台机器上跑，不联网、零调用成本，说的话不会离开这台电脑。",
            "info",
        ).pack(fill="x", pady=(S(18), 0))

        # --- 路径 ---------------------------------------------------------
        c2 = W.Card(root, title="路径")
        c2.pack(fill="x", pady=(S(18), 0))
        box, self._paths = _screen_text(c2.body, height=7, wrap="none")
        box.pack(fill="x")
        self._paths.configure(tabs=(S(120),), tabstyle="tabular")
        self._paths.tag_configure("k", foreground=P["scr_dim"])
        row2 = tk.Frame(c2.body, bg=c2.fill)
        row2.pack(fill="x", pady=(S(12), 0))
        W.Button(row2, "打开数据目录", icon="folder",
                 command=lambda: W.open_folder(str(bootstrap.data_dir()))).pack(side="left")
        W.Button(row2, "打开日志文件", icon="file",
                 command=lambda: W.open_folder(str(bootstrap.log_path()))).pack(side="left", padx=S(8))
        W.Button(row2, "复制路径", kind="ghost", icon="copy", command=self._copy_paths).pack(side="left")

        # --- 依赖 ---------------------------------------------------------
        c3 = W.Card(root, title="依赖")
        c3.pack(fill="x", pady=(S(18), 0))
        self._dep_box = tk.Frame(c3.body, bg=c3.fill)
        self._dep_box.pack(fill="x")

        # --- 体检 ---------------------------------------------------------
        c4 = W.Card(root, title="完整体检")
        c4.pack(fill="both", expand=True, pady=(S(18), 0))
        row4 = tk.Frame(c4.body, bg=c4.fill)
        row4.pack(fill="x")
        W.Button(row4, "运行体检", kind="primary", icon="play", command=self.run_doctor).pack(side="left")
        W.Button(row4, "复制诊断信息", icon="copy", command=self._copy_doctor).pack(side="left", padx=S(8))
        W.Button(row4, "打开项目主页", kind="ghost", icon="external",
                 command=lambda: W.open_url(REPO)).pack(side="right")

        dbox, self._doctor = _screen_text(c4.body, height=12, wrap="word")
        dbox.pack(fill="both", expand=True, pady=(S(12), 0))
        self._write_doctor("点上面的「运行体检」，会把模型、配置、动作、麦克风逐项检查一遍。")

    # -- 数据 ------------------------------------------------------------- #

    def _refresh_paths(self) -> None:
        cfg = self.app.cfg
        from ..runner import decision_dir_for

        # 内嵌权重时把解包目录标出来，否则用户会照着配置里的路径去找、然后找不到
        ddir = decision_dir_for(cfg)
        bundled = "（打包内嵌）" if ddir != cfg.decision_path() else ""
        rows = [
            ("运行方式", "打包 exe" if bootstrap.is_frozen() else "源码 / venv"),
            ("可执行文件", str(bootstrap.exe_dir())),
            ("可写数据", str(bootstrap.data_dir())),
            ("配置文件", str(self.app.config_path or "（没有，保存设置时会自动生成）")),
            ("模型目录", str(self.app.model_dir())),
            ("语义层权重", f"{ddir}{bundled}"),
            ("运行日志", str(bootstrap.log_path())),
        ]
        t = self._paths
        t.configure(state="normal")
        t.delete("1.0", "end")
        for i, (k, v) in enumerate(rows):
            t.insert("end", k, ("k",))
            t.insert("end", f"\t{v}" + ("\n" if i < len(rows) - 1 else ""))
        t.configure(state="disabled")

    def _refresh_deps(self) -> None:
        if self._deps_pending is None:
            return
        bg = self._dep_box.cget("bg")
        for w in self._dep_box.winfo_children():
            w.destroy()
        for name, why, required, got in self._deps_pending:
            row = tk.Frame(self._dep_box, bg=bg)
            row.pack(fill="x", pady=S(5))
            ok = not got.startswith("缺少") and not got.startswith("导入失败")
            if ok:
                icon, color = "ok", P["ok"]
            elif not required:
                icon, color = "dot", P["ink4"]  # 可选依赖没装不是问题，别画成红叉
            else:
                icon, color = "err", P["err"]
            tk.Label(row, image=gfx.icon(self, icon, S(16), color), bg=bg, bd=0).pack(side="left", padx=(0, S(10)))
            tk.Label(row, text=name, bg=bg, fg=P["ink"], font=FONTS["mono"], width=17, anchor="w").pack(side="left")
            tk.Label(row, text=got, bg=bg, fg=P["ink2"], font=FONTS["digits_s"], width=13, anchor="w").pack(side="left")
            tk.Label(row, text=why + ("" if required else "（可选）"), bg=bg, fg=P["ink3"], font=FONTS["note"],
                     anchor="w").pack(side="left")
        self._deps_pending = None

    def scan_deps(self) -> None:
        """逐个 import 依赖看版本（同步）。import sherpa_onnx / onnxruntime 这类要几百毫秒，
        所以界面里用 `scan_deps_async`；这个同步版留给测试和命令行。"""
        out: list[tuple[str, str, bool, str]] = []
        for name, why, required in DEPS:
            try:
                mod = __import__(name)
                ver = getattr(mod, "__version__", "")
                out.append((name, why, required, str(ver) or "已安装"))
            except Exception as e:  # noqa: BLE001
                out.append((name, why, required, f"缺少（{type(e).__name__}）"))
        self._deps_pending = out

    def scan_deps_async(self) -> None:
        threading.Thread(target=self.scan_deps, name="voice-ctl-scan-deps", daemon=True).start()

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
        W.copy_to_clipboard(self.app.root, self._paths.get("1.0", "end").strip().replace("\t", "  "))

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
        self.scan_deps_async()

    def on_tick(self) -> None:
        self._refresh_deps()
        if self._doctor_ready:
            self._doctor_ready = False
            self._write_doctor(self._doctor_out)
