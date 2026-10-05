"""「设置」页：把 config.toml 里那些值得调的项做成控件。

原则：**只暴露用户真的会调的**。像 samplerate / channels 这种改了就坏的，
只读展示 + 说明为什么不给改——给出一个能改坏的旋钮是不负责任。
"""

from __future__ import annotations

import copy
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import ttk
from typing import Any

from .. import events
from ..config import AppConfig
from . import widgets as W
from .theme import PALETTE as P
from .theme import S

LANGUAGES = ["auto", "zh", "en", "ja", "ko", "yue"]
PROVIDERS = ["cpu", "cuda", "coreml"]
DECISION_MODELS = ["multilingual", "english", "typed"]


class NumberRow(tk.Frame):
    """滑块 + 输入框，双向同步。

    只给滑块的话，用户没法精确输入；只给输入框的话，用户不知道合理范围在哪。
    两个都给，并且互相跟随。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        from_: float,
        to: float,
        step: float = 1,
        fmt: str = "{:.3f}",
        width: int = 8,
        unit: str = "",
        command: Any = None,
    ) -> None:
        super().__init__(master, bg=P["surface"])
        self.var = tk.DoubleVar()
        self.text = tk.StringVar()
        self._fmt = fmt
        self._step = step
        self._command = command

        self.scale = ttk.Scale(
            self, from_=from_, to=to, orient="horizontal", variable=self.var,
            command=self._from_scale,
        )
        self.scale.pack(side="left", fill="x", expand=True)
        self.entry = ttk.Entry(self, textvariable=self.text, width=width)
        self.entry.pack(side="left", padx=(S(8), 0))
        self.entry.bind("<Return>", self._to_scale)
        self.entry.bind("<FocusOut>", self._to_scale)
        if unit:
            tk.Label(self, text=unit, bg=P["surface"], fg=P["faint"],
                     font=W.theme.FONTS["tiny"]).pack(side="left", padx=(S(4), 0))

    def set(self, value: float) -> None:
        self.var.set(float(value))
        self.text.set(self._fmt.format(float(value)))

    def get(self) -> float:
        return float(self.var.get())

    def _from_scale(self, _v: Any = None) -> None:
        self.text.set(self._fmt.format(self.var.get()))
        if self._command:
            self._command()

    def _to_scale(self, _e: Any = None) -> None:
        try:
            value = float(self.text.get())
        except ValueError:
            self.text.set(self._fmt.format(self.var.get()))
            return
        lo, hi = self.scale.cget("from"), self.scale.cget("to")
        value = max(float(lo), min(float(hi), value))
        self.var.set(value)
        self.text.set(self._fmt.format(value))
        if self._command:
            self._command()


class SettingsTab(tk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=P["bg"])
        self.app = app
        self._devices: list[tuple[int, str, int, bool]] = []

        self._device = tk.StringVar()
        self._min_peak: NumberRow
        self._model_dir = tk.StringVar()
        self._language = tk.StringVar()
        self._itn = tk.BooleanVar()
        self._threads = tk.IntVar()
        self._provider = tk.StringVar()
        self._threshold: NumberRow
        self._prefixes = tk.StringVar()
        self._suffixes = tk.StringVar()
        self._decision_on = tk.BooleanVar()
        self._decision_model = tk.StringVar()
        self._confidence: NumberRow
        self._onnx_dir = tk.StringVar()
        self._print_result = tk.BooleanVar()
        self._status = tk.StringVar()
        self._reload_after: str | None = None
        """后台任务的结果槽。worker 只写它，主线程在 on_tick 里消费——
        见 window.py 顶部的线程规则。"""

        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        wrap = W.ScrollFrame(self)
        wrap.pack(fill="both", expand=True)
        self._scroll = wrap
        root = tk.Frame(wrap.inner, bg=P["bg"])
        root.pack(fill="both", expand=True, padx=S(18), pady=S(18))

        # --- 音频 ---------------------------------------------------------
        c1 = W.Card(root, title="麦克风")
        c1.pack(fill="x")
        f1 = W.Form(c1.body)
        f1.pack(fill="x")
        drow = tk.Frame(f1, bg=P["surface"])
        self._device_box = ttk.Combobox(drow, textvariable=self._device, state="readonly")
        self._device_box.pack(side="left", fill="x", expand=True)
        ttk.Button(drow, text="刷新", command=self.reload_devices).pack(side="left", padx=(S(8), 0))
        f1.add("输入设备", drow, note="留「系统默认」就用 Windows 当前默认麦克风。")
        self._min_peak = NumberRow(f1, from_=0.0, to=0.1, fmt="{:.3f}")
        f1.add(
            "静音阈值", self._min_peak,
            note="整段录音的「峰值」低于它就当没说话，直接跳过识别。0 = 关闭过滤。\n"
                 "为什么用峰值不用音量均值：安静环境里正常说话的均值可能只有 0.002，和底噪同级，"
                 "用它当阈值会把小声说话静默丢掉——表现是「有时候说了没反应」，极难排查。",
        )

        # --- 识别 ---------------------------------------------------------
        c2 = W.Card(root, title="识别模型")
        c2.pack(fill="x", pady=(S(14), 0))
        f2 = W.Form(c2.body)
        f2.pack(fill="x")
        mrow = tk.Frame(f2, bg=P["surface"])
        ttk.Entry(mrow, textvariable=self._model_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(mrow, text="打开目录", command=lambda: self._open(self._model_dir.get())).pack(
            side="left", padx=(S(8), 0)
        )
        f2.add("模型目录", mrow)
        self._model_status = tk.Label(f2, text="", bg=P["surface"], fg=P["muted"],
                                      font=W.theme.FONTS["small"], anchor="w")
        self._model_status.grid(row=f2._row, column=1, sticky="ew", pady=(0, S(10)))  # noqa: SLF001
        f2._row += 1  # noqa: SLF001

        f2.add("语言", ttk.Combobox(f2, textvariable=self._language, values=LANGUAGES,
                                    state="readonly"),
               note="实测对本模型输出无影响（zh/auto/en 结果完全一致），保持 auto 即可。")
        W.check(f2, "数字与标点正常化（use_itn）", self._itn, bg=P["surface"]).grid(
            row=f2._row, column=1, sticky="w", pady=(0, S(10))  # noqa: SLF001
        )
        f2._row += 1  # noqa: SLF001
        f2.add("线程数", ttk.Spinbox(f2, from_=1, to=16, textvariable=self._threads, width=8),
               note="CPU 推理线程。本机实测 2 线程已经比实时快 20 倍以上，调高收益很小。")
        f2.add("推理后端", ttk.Combobox(f2, textvariable=self._provider, values=PROVIDERS,
                                        state="readonly"),
               note="cpu 最省事。cuda 需要装 onnxruntime-gpu，本机实测没必要。")
        drow2 = tk.Frame(c2.body, bg=P["surface"])
        drow2.pack(fill="x", pady=(S(4), 0))
        self._dl_btn = ttk.Button(drow2, text="下载识别模型（约 226MB）", command=self._download_model)
        self._dl_btn.pack(side="left")
        ttk.Button(drow2, text="用样例音频验证", command=self._test_asr).pack(side="left", padx=S(8))

        # --- 匹配 ---------------------------------------------------------
        c3 = W.Card(root, title="匹配")
        c3.pack(fill="x", pady=(S(14), 0))
        f3 = W.Form(c3.body)
        f3.pack(fill="x")
        self._threshold = NumberRow(f3, from_=0, to=100, fmt="{:.0f}", width=6)
        f3.add(
            "相似度阈值", self._threshold,
            note="0-100，越高越严格。调低会更容易命中，但也更容易把不相干的话认成命令。",
        )
        f3.add("剥离前缀", ttk.Entry(f3, textvariable=self._prefixes),
               note="识别结果开头要丢掉的客气话，逗号分隔。")
        f3.add("剥离后缀", ttk.Entry(f3, textvariable=self._suffixes),
               note="结尾的语气词，逗号分隔。")

        # --- 语义层 -------------------------------------------------------
        c4 = W.Card(root, title="语义层（可选，让口语化说法也能命中）")
        c4.pack(fill="x", pady=(S(14), 0))
        f4 = W.Form(c4.body)
        f4.pack(fill="x")
        W.check(f4, "启用语义决策", self._decision_on, bg=P["surface"]).grid(
            row=f4._row, column=1, sticky="w", pady=(0, S(10))  # noqa: SLF001
        )
        f4._row += 1  # noqa: SLF001
        f4.add("权重目录", ttk.Entry(f4, textvariable=self._onnx_dir),
               note="相对路径按 config.toml 所在目录解析。")
        f4.add("checkpoint", ttk.Combobox(f4, textvariable=self._decision_model,
                                          values=DECISION_MODELS, state="readonly"),
               note="中文务必用 multilingual——用 english 的中文判断接近随机。")
        self._confidence = NumberRow(f4, from_=0.0, to=1.0, fmt="{:.2f}", width=6)
        f4.add("置信度下限", self._confidence,
               note="低于它就不执行，只在日志里说明。实测负样本落在 0.589，所以默认 0.6。")
        self._decision_status = tk.Label(f4, text="", bg=P["surface"], fg=P["muted"],
                                         font=W.theme.FONTS["small"], anchor="w",
                                         justify="left", wraplength=S(520))
        self._decision_status.grid(row=f4._row, column=1, sticky="ew", pady=(0, S(10)))  # noqa: SLF001
        f4._row += 1  # noqa: SLF001
        drow4 = tk.Frame(c4.body, bg=P["surface"])
        drow4.pack(fill="x")
        ttk.Button(drow4, text="下载语义层权重（约 900MB）",
                   command=self._download_decision).pack(side="left")
        ttk.Button(drow4, text="重新检查", style="Ghost.TButton",
                   command=self._check_decision).pack(side="left", padx=S(8))
        W.hint(
            c4.body,
            "语义层是第 1 层：第 0 层的别名匹配搞不定的口语才走它。"
            "它只该兜住漏网的输入，不该当主判据——官方 benchmark 里 20 选项意图任务只有 0.451。",
        ).pack(fill="x", pady=(S(8), 0))

        # --- 输出 ---------------------------------------------------------
        c5 = W.Card(root, title="输出")
        c5.pack(fill="x", pady=(S(14), 0))
        f5 = W.Form(c5.body)
        f5.pack(fill="x")
        W.check(f5, "把识别结果和执行情况写进日志", self._print_result, bg=P["surface"]).grid(
            row=0, column=1, sticky="w", pady=(0, S(10))
        )
        f5._row = 1  # noqa: SLF001
        W.hint(
            c5.body,
            "提示音（开始/结束那一声）在「快捷键」页里调。",
        ).pack(fill="x")

        # --- 底部 ---------------------------------------------------------
        bar = tk.Frame(root, bg=P["bg"])
        bar.pack(fill="x", pady=(S(18), 0))
        ttk.Button(bar, text="保存全部修改", style="Primary.TButton",
                   command=self._save).pack(side="left")
        ttk.Button(bar, text="放弃修改", command=self.load_from_config).pack(side="left", padx=S(8))
        ttk.Button(bar, text="重新加载配置文件", command=self.app.reload_config).pack(side="left")
        ttk.Button(bar, text="打开 config.toml", style="Ghost.TButton",
                   command=self._open_config).pack(side="right")
        ttk.Button(bar, text="打开所在文件夹", style="Ghost.TButton",
                   command=self._open_config_dir).pack(side="right", padx=S(8))

        tk.Label(root, textvariable=self._status, bg=P["bg"], fg=P["faint"],
                 font=W.theme.FONTS["tiny"], anchor="w", justify="left",
                 wraplength=S(760)).pack(fill="x", pady=(S(10), 0))

    # -- 数据 ------------------------------------------------------------- #

    def reload_devices(self) -> None:
        from ..recorder import RecorderError, list_input_devices

        try:
            self._devices = list_input_devices()
        except RecorderError as e:
            self._devices = []
            self._device_box.configure(values=["（枚举设备失败）"])
            self._device.set("（枚举设备失败）")
            events.error(f"枚举麦克风失败：{e}", kind="ui")
            return
        labels = ["系统默认"]
        for idx, name, rate, default in self._devices:
            labels.append(f"{'★ ' if default else ''}[{idx}] {name}")
        self._device_box.configure(values=labels)
        cur = self.app.cfg.audio.device
        if cur is None:
            self._device.set("系统默认")
        else:
            for lab in labels[1:]:
                if lab.split("]")[0].endswith(str(cur)):
                    self._device.set(lab)
                    break
            else:
                self._device.set("系统默认")

    def _device_value(self) -> int | None:
        sel = self._device.get()
        if not sel or sel == "系统默认":
            return None
        for idx, _name, _rate, _default in self._devices:
            if f"[{idx}]" in sel:
                return idx
        return None

    def load_from_config(self) -> None:
        cfg = self.app.cfg
        self.reload_devices()
        self._min_peak.set(cfg.audio.min_peak)
        self._model_dir.set(cfg.model.dir)
        self._language.set(cfg.model.language)
        self._itn.set(bool(cfg.model.use_itn))
        self._threads.set(cfg.model.num_threads)
        self._provider.set(cfg.model.provider)
        self._threshold.set(cfg.match.threshold)
        self._prefixes.set("，".join(cfg.match.strip_prefixes))
        self._suffixes.set("，".join(cfg.match.strip_suffixes))
        self._decision_on.set(bool(cfg.decision.enabled))
        self._decision_model.set(cfg.decision.model)
        self._confidence.set(cfg.decision.min_confidence)
        self._onnx_dir.set(cfg.decision.onnx_dir)
        self._print_result.set(bool(cfg.feedback.print_result))
        self._refresh_facts()

    def _refresh_facts(self) -> None:
        md = self.app.model_dir()
        model = md / "model.int8.onnx"
        if model.is_file():
            mb = model.stat().st_size / 1024 / 1024
            self._model_status.configure(text=f"✓ 已就绪：{md}（{mb:.1f} MB）", fg=P["ok"])
        else:
            self._model_status.configure(
                text=f"✗ 缺少 model.int8.onnx（当前指向 {md}）——点下面的按钮下载", fg=P["error"]
            )
        self._dl_btn.state(["!disabled"])
        self._check_decision()
        self._status.set(
            f"配置文件：{self.app.config_path or '（没有配置文件，改动会新建一份）'}"
        )

    def _check_decision(self) -> None:
        from ..decision import available

        root = self.app.cfg.decision_path()
        ok, reason = available(root)
        color = P["ok"] if ok else P["muted"]
        extra = ""
        if not self.app.cfg.decision.enabled:
            extra = "（配置里没启用，不参与判断）"
        self._decision_status.configure(text=f"{'✓' if ok else '·'} {reason} {extra}".strip(),
                                        fg=color)

    # -- 组装新配置 ------------------------------------------------------- #

    def _collect(self) -> AppConfig:
        cfg = copy.deepcopy(self.app.cfg)
        cfg.audio = replace(cfg.audio, device=self._device_value(), min_peak=self._min_peak.get())
        cfg.model = replace(
            cfg.model,
            dir=self._model_dir.get().strip(),
            language=self._language.get(),
            use_itn=bool(self._itn.get()),
            num_threads=int(self._threads.get()),
            provider=self._provider.get(),
        )
        cfg.match = replace(
            cfg.match,
            threshold=int(round(self._threshold.get())),
            strip_prefixes=_split(self._prefixes.get()),
            strip_suffixes=_split(self._suffixes.get()),
        )
        cfg.decision = replace(
            cfg.decision,
            enabled=bool(self._decision_on.get()),
            model=self._decision_model.get(),
            min_confidence=round(self._confidence.get(), 3),
            onnx_dir=self._onnx_dir.get().strip(),
        )
        cfg.feedback = replace(cfg.feedback, print_result=bool(self._print_result.get()))
        return cfg

    def _save(self) -> None:
        try:
            cfg = self._collect()
            cfg.validate()
        except Exception as e:  # noqa: BLE001 - 校验消息本身就是给用户看的
            events.error(f"这些设置有问题，没有保存：{e}", kind="ui")
            return
        if not cfg.model.dir:
            events.error("[model].dir 不能为空", kind="ui")
            return
        if not cfg.decision.onnx_dir and cfg.decision.enabled:
            events.error("启用了语义层就必须写权重目录", kind="ui")
            return
        self.app.save_cfg(cfg)

    # -- 动作 ------------------------------------------------------------- #

    def _open(self, path: str) -> None:
        from pathlib import Path

        p = Path(path).expanduser()
        if not p.is_absolute() and self.app.config_path:
            p = self.app.config_path.parent / p
        if not p.exists():
            events.warn(f"目录不存在：{p}", kind="ui")
            return
        W.open_folder(str(p))

    def _open_config(self) -> None:
        path = self.app.config_path
        if not path or not path.is_file():
            events.warn("还没有配置文件，先保存一次设置就会生成", kind="ui")
            return
        W.open_folder(str(path))

    def _open_config_dir(self) -> None:
        path = self.app.config_path
        if path:
            W.open_folder(str(path.parent))

    def _download_model(self) -> None:
        from ..fetch import download_asr_model

        target = self.app.model_dir()
        self._dl_btn.state(["disabled"])

        def work() -> None:
            try:
                download_asr_model(target)
            except Exception as e:  # noqa: BLE001
                events.error(f"下载出错：{type(e).__name__}: {e}", kind="download")
            finally:
                # 只置标志，不碰 Tk：见 window.py 顶部的线程规则
                self._reload_after = "model"

        events.info("开始下载识别模型，进度会打到「日志」页", kind="download")
        threading.Thread(target=work, name="voice-ctl-download", daemon=True).start()

    def _after_download(self) -> None:
        self.app.reload_config(quiet=True)
        self._refresh_facts()

    def _download_decision(self) -> None:
        from ..decision import WEIGHTS, fetch_weights

        which = self._decision_model.get() or "multilingual"
        spec = WEIGHTS.get(which)
        if spec is None:
            events.error(f"不认识 {which!r}；可选：{', '.join(WEIGHTS)}", kind="download")
            return
        root = self._onnx_dir.get().strip() or "models/laya-onnx/multilingual"

        def work() -> None:
            try:
                fetch_weights(root, which)
            except Exception as e:  # noqa: BLE001
                events.error(f"语义层权重下载失败：{type(e).__name__}: {e}", kind="download")
                events.warn("网络不通时把 HF_ENDPOINT 设成 https://hf-mirror.com 再试", kind="download")
            finally:
                self._reload_after = "decision"

        events.info(
            f"开始下载语义层权重 {which}（约 900MB，来自 {spec['repo']}），进度见「日志」页",
            kind="download",
        )
        threading.Thread(target=work, name="voice-ctl-fetch-decision", daemon=True).start()

    def _test_asr(self) -> None:
        events.info("用自带样例音频验证识别（要加载模型，几秒钟）…", kind="ui")
        self.app.verify_asr()

    # -- 刷新 ------------------------------------------------------------- #

    def on_show(self) -> None:
        self.load_from_config()
        self._scroll.to_top()

    def on_tick(self) -> None:
        flag, self._reload_after = self._reload_after, None
        if flag == "model":
            self._after_download()
        elif flag == "decision":
            self._check_decision()


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
