"""「设置」页：把 config.toml 里那些值得调的项做成控件。

原则：**只暴露用户真的会调的**。像 samplerate / channels 这种改了就坏的，
不给改——给出一个能改坏的旋钮是不负责任。

「保存」条固定在页面底部（不随内容滚动）：页面很长，保存按钮要是跟着滚走，
用户改完最后一项还得先找一遍按钮在哪。
"""

from __future__ import annotations

import copy
import threading
import tkinter as tk
from dataclasses import replace
from typing import Any

from .. import events
from ..config import AppConfig
from . import widgets as W
from .theme import FONTS
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
        slider_width: int = 200,
    ) -> None:
        bg = W.bg_of(master)
        super().__init__(master, bg=bg)
        self.var = tk.DoubleVar()
        self.text = tk.StringVar()
        self._fmt = fmt
        self._step = step
        self._command = command

        self.scale = W.Slider(self, from_=from_, to=to, variable=self.var, command=self._from_scale)
        self.scale.configure(width=S(slider_width))
        self.scale.pack(side="left")
        self.entry = W.Field(self, textvariable=self.text, width=max(64, width * 9), height=34, justify="right",
                             font="digits")
        self.entry.pack(side="left", padx=(S(12), 0))
        self.entry.entry.bind("<Return>", self._to_scale)
        self.entry.entry.bind("<FocusOut>", self._to_scale)
        if unit:
            tk.Label(self, text=unit, bg=bg, fg=P["ink3"], font=FONTS["note"]).pack(side="left", padx=(S(6), 0))

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
        super().__init__(master, bg=P["chassis"])
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
        self._web_on = tk.BooleanVar()
        self._web_search = tk.BooleanVar()
        self._web_search_url = tk.StringVar()
        self._print_result = tk.BooleanVar()
        self._status = tk.StringVar()
        self._reload_after: str | None = None
        """后台任务的结果槽。worker 只写它，主线程在 on_tick 里消费——
        见 window.py 顶部的线程规则。"""
        self._decision_result: tuple[bool, str] | None = None
        self._decision_busy = False

        self._build()

    # -- 构建 ------------------------------------------------------------- #

    def _build(self) -> None:
        # 保存条先 pack 到底部，滚动区再吃掉剩下的高度
        bar = tk.Frame(self, bg=P["chassis"])
        bar.pack(side="bottom", fill="x")
        W.hline(bar, color=P["line"])
        inner = tk.Frame(bar, bg=P["chassis"])
        inner.pack(fill="x", padx=S(28), pady=(S(12), S(12)))
        W.Button(inner, "保存全部修改", kind="primary", icon="check", command=self._save).pack(side="left")
        W.Button(inner, "放弃修改", icon="undo", command=self.load_from_config).pack(side="left", padx=S(8))
        W.Button(inner, "重新加载配置文件", kind="ghost", icon="refresh", command=self.app.reload_config).pack(
            side="left")
        W.Button(inner, "打开所在文件夹", kind="ghost", icon="folder", command=self._open_config_dir).pack(side="right")
        W.Button(inner, "打开 config.toml", kind="ghost", icon="file", command=self._open_config).pack(
            side="right", padx=(0, S(4)))
        tk.Label(bar, textvariable=self._status, bg=P["chassis"], fg=P["ink3"], font=FONTS["caption"],
                 anchor="w").pack(fill="x", padx=S(28), pady=(0, S(10)))

        wrap = W.ScrollFrame(self)
        wrap.pack(fill="both", expand=True)
        self._scroll = wrap
        root = tk.Frame(wrap.inner, bg=P["chassis"])
        root.pack(fill="both", expand=True, padx=S(28), pady=S(26))

        # --- 音频 ---------------------------------------------------------
        c1 = W.Card(root, title="麦克风")
        c1.pack(fill="x")
        slot = c1.row("输入设备", "留「系统默认」就用 Windows 当前默认麦克风。")
        self._device_box = W.Select(slot, variable=self._device, width=300)
        self._device_box.pack(side="left")
        W.Button(slot, "刷新", icon="refresh", command=self.reload_devices).pack(side="left", padx=(S(8), 0))
        slot = c1.row(
            "静音阈值",
            "整段录音的「峰值」低于它就当没说话，直接跳过识别。0 = 关闭过滤。\n"
            "为什么用峰值不用音量均值：安静环境里正常说话的均值可能只有 0.002，和底噪同级，"
            "用它当阈值会把小声说话静默丢掉——表现是「有时候说了没反应」，极难排查。",
        )
        self._min_peak = NumberRow(slot, from_=0.0, to=0.1, fmt="{:.3f}")
        self._min_peak.pack()

        # --- 识别 ---------------------------------------------------------
        c2 = W.Card(root, title="识别模型")
        c2.pack(fill="x", pady=(S(18), 0))
        slot = c2.row("模型目录", stack=True)
        mrow = tk.Frame(slot, bg=c2.fill)
        mrow.pack(fill="x")
        self._model_field = W.Field(mrow, textvariable=self._model_dir, mono=True)
        self._model_field.pack(side="left", fill="x", expand=True)
        W.Button(mrow, "打开目录", icon="folder",
                 command=lambda: self._open(self._model_dir.get())).pack(side="left", padx=(S(8), 0))
        self._model_status = W.Callout(slot, "", "info", gap=(10, 0))
        self._model_status.pack(fill="x")
        slot = c2.row("语言", "实测对本模型输出无影响（zh/auto/en 结果完全一致），保持 auto 即可。")
        W.Select(slot, variable=self._language, values=LANGUAGES, width=180).pack()
        slot = c2.row("数字与标点正常化（use_itn）", "把「九点」写成「9点」这类。")
        W.Switch(slot, "", self._itn).pack()
        slot = c2.row("线程数", "CPU 推理线程。本机实测 2 线程已经比实时快 20 倍以上，调高收益很小。")
        W.Stepper(slot, variable=self._threads, from_=1, to=16, step=1, width=130).pack()
        slot = c2.row("推理后端", "cpu 最省事。cuda 需要装 onnxruntime-gpu，本机实测没必要。")
        W.Select(slot, variable=self._provider, values=PROVIDERS, width=180).pack()
        foot = tk.Frame(c2.body, bg=c2.fill)
        foot.pack(fill="x", pady=(S(4), 0))
        self._dl_btn = W.Button(foot, "下载识别模型（约 226MB）", icon="download", command=self._download_model)
        self._dl_btn.pack(side="right")
        W.Button(foot, "用样例音频验证", kind="ghost", icon="play", command=self._test_asr).pack(
            side="right", padx=(0, S(8)))

        # --- 匹配 ---------------------------------------------------------
        c3 = W.Card(root, title="匹配")
        c3.pack(fill="x", pady=(S(18), 0))
        slot = c3.row("相似度阈值", "0-100，越高越严格。调低会更容易命中，但也更容易把不相干的话认成命令。")
        self._threshold = NumberRow(slot, from_=0, to=100, fmt="{:.0f}", width=6)
        self._threshold.pack()
        slot = c3.row("剥离前缀", "识别结果开头要丢掉的客气话，逗号分隔。", stack=True)
        W.Field(slot, textvariable=self._prefixes).pack(fill="x")
        slot = c3.row("剥离后缀", "结尾的语气词，逗号分隔。", stack=True)
        W.Field(slot, textvariable=self._suffixes).pack(fill="x")

        # --- 语义层 -------------------------------------------------------
        c4 = W.Card(root, title="语义层（可选，让口语化说法也能命中）")
        c4.pack(fill="x", pady=(S(18), 0))
        slot = c4.row("启用语义决策", "第 0 层的别名匹配搞不定的口语才走它。")
        W.Switch(slot, "", self._decision_on).pack()
        slot = c4.row("权重目录", "相对路径按 config.toml 所在目录解析。", stack=True)
        W.Field(slot, textvariable=self._onnx_dir, mono=True).pack(fill="x")
        slot = c4.row("checkpoint", "中文务必用 multilingual——用 english 的中文判断接近随机。")
        W.Select(slot, variable=self._decision_model, values=DECISION_MODELS, width=180).pack()
        slot = c4.row("置信度下限", "低于它就不执行，只在日志里说明。实测负样本落在 0.589，所以默认 0.6。")
        self._confidence = NumberRow(slot, from_=0.0, to=1.0, fmt="{:.2f}", width=6)
        self._confidence.pack()
        self._decision_status = W.Callout(c4.body, "", "info", gap=(6, 0))
        self._decision_status.pack(fill="x")
        foot4 = tk.Frame(c4.body, bg=c4.fill)
        foot4.pack(fill="x", pady=(S(14), 0))
        W.Button(foot4, "下载语义层权重（约 900MB）", icon="download", command=self._download_decision).pack(
            side="right")
        W.Button(foot4, "重新检查", kind="ghost", icon="refresh", command=self._check_decision).pack(
            side="right", padx=(0, S(8)))
        W.hint(
            c4.body,
            "语义层是第 1 层：第 0 层的别名匹配搞不定的口语才走它。"
            "它只该兜住漏网的输入，不该当主判据——官方 benchmark 里 20 选项意图任务只有 0.451。",
            font="caption",
        ).pack(fill="x", pady=(S(12), 0))

        # --- 网页 ---------------------------------------------------------
        c5w = W.Card(root, title="网页（说得出名字就能开）")
        c5w.pack(fill="x", pady=(S(18), 0))
        slot = c5w.row("认出网站名字就打开", "「打开百度」这类。站点表是内置的，不联网。")
        W.Switch(slot, "", self._web_on).pack()
        slot = c5w.row("没收录的名字退一步用搜索", "这一步要联网——彻底离线用就把它关掉，「打开百度」照样能用。")
        W.Switch(slot, "", self._web_search).pack()
        slot = c5w.row("搜索地址", "必须有 {q} 占位符，查询词会填进去。", stack=True)
        W.Field(slot, textvariable=self._web_search_url, mono=True).pack(fill="x")
        W.hint(
            c5w.body,
            "站点表约 70 个常见站（voice_ctl/web.py）。"
            "本机装了同名的程序时以程序为先：说「打开微信」开的是微信，不是网页。",
            font="caption",
        ).pack(fill="x", pady=(S(12), 0))

        # --- 输出 ---------------------------------------------------------
        c5 = W.Card(root, title="输出")
        c5.pack(fill="x", pady=(S(18), 0))
        slot = c5.row("把识别结果和执行情况写进日志", "提示音（开始/结束那一声）在「快捷键」页里调。")
        W.Switch(slot, "", self._print_result).pack()

    # -- 数据 ------------------------------------------------------------- #

    def reload_devices(self) -> None:
        from ..recorder import RecorderError, list_input_devices

        try:
            self._devices = list_input_devices()
        except RecorderError as e:
            self._devices = []
            self._device_box.set_values(["（枚举设备失败）"])
            self._device.set("（枚举设备失败）")
            events.error(f"枚举麦克风失败：{e}", kind="ui")
            return
        labels = ["系统默认"]
        for idx, name, _rate, default in self._devices:
            labels.append(f"{'★ ' if default else ''}[{idx}] {name}")
        self._device_box.set_values(labels)
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
        self._web_on.set(bool(cfg.web.enabled))
        self._web_search.set(bool(cfg.web.search_fallback))
        self._web_search_url.set(cfg.web.search_url)
        self._print_result.set(bool(cfg.feedback.print_result))
        self._refresh_facts()

    def _refresh_facts(self) -> None:
        md = self.app.model_dir()
        model = md / "model.int8.onnx"
        if model.is_file():
            mb = model.stat().st_size / 1024 / 1024
            self._model_status.set(f"已就绪：{md}（{mb:.1f} MB）", "ok")
        else:
            self._model_status.set(
                f"缺少 model.int8.onnx（当前指向 {md}）——点下面的按钮下载", "error"
            )
        self._dl_btn.set_enabled(True)
        self._check_decision()
        self._status.set(
            f"配置文件：{self.app.config_path or '（没有配置文件，改动会新建一份）'}"
        )

    def _check_decision(self) -> None:
        """重新检查语义层能不能用——**放在后台线程**。

        `decision.available()` 为了判断"能不能用"会真的 import laya → torch，
        实测第一次要 2–3 秒。放在 UI 线程里，就是用户第一次点「设置」整个界面冻几秒。
        结果放进槽里，由 on_tick 画到提示条上（见 window.py 顶部的线程规则）。
        """
        from ..runner import decision_dir_for

        if self._decision_busy:
            return
        self._decision_busy = True
        # 用 decision_dir_for 而不是 cfg.decision_path()：后者不知道权重可能
        # 被内嵌在 _MEIPASS 里，会报"缺少权重"而实际是好的。
        root = decision_dir_for(self.app.cfg)
        self._decision_status.set("正在检查语义层 …（第一次要几秒）", "info")

        def work() -> None:
            from ..decision import available

            try:
                self._decision_result = available(root)
            except Exception as e:  # noqa: BLE001 - 检查本身出错也要让用户看到
                self._decision_result = (False, f"检查语义层时出错：{type(e).__name__}: {e}")
            finally:
                self._decision_busy = False

        threading.Thread(target=work, name="voice-ctl-decision-check", daemon=True).start()

    def _show_decision(self, ok: bool, reason: str) -> None:
        extra = ""
        if not self.app.cfg.decision.enabled:
            extra = "（配置里没启用，不参与判断）"
        self._decision_status.set(f"{reason} {extra}".strip(), "ok" if ok else "info")

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
        cfg.web = replace(
            cfg.web,
            enabled=bool(self._web_on.get()),
            search_fallback=bool(self._web_search.get()),
            search_url=self._web_search_url.get().strip() or cfg.web.search_url,
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
        self._dl_btn.set_enabled(False)

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
        """下载语义层权重。

        判断全部走 decision.preflight_fetch —— CLI 的 fetch-decision 调的是同一个
        函数。以前这里自己写了一遍，而且漏了两件：
          1. 没判断"这个构建加载不了语义层"。实测撞到过：完整版（不带 torch）
             从界面点下载，老实下完 906MB，用户开语义层才看到 No module named 'torch'。
          2. 落盘用的是 self._onnx_dir.get() **原始字符串**——那是相对路径，
             相对配置文件所在目录才对，直接 mkdir 会相对**当前工作目录**。
        """
        from ..decision import WEIGHTS, fetch_weights, preflight_fetch

        which = self._decision_model.get() or "multilingual"
        spec = WEIGHTS.get(which)
        if spec is None:
            events.error(f"不认识 {which!r}；可选：{', '.join(WEIGHTS)}", kind="download")
            return

        plan = preflight_fetch(self._onnx_dir.get().strip())

        if plan.already_ready:
            where = "（打包内嵌）" if plan.bundled else ""
            events.ok(f"语义层权重已就绪，无需下载{where}：{plan.target}", kind="download")
            return

        if not plan.allowed:
            # 不能弹一个"要不要继续"的对话框然后照样下 906MB——界面上的按钮
            # 点下去就该是有效动作。这里直接拒绝，并说清换哪个构建。
            events.error(f"这个构建加载不了语义层：{plan.reason}", kind="download")
            events.warn(
                "这 906MB 下完仍然用不了——缺的是 torch/laya，不是权重。"
                "要用语义层请换带语义层的构建（VOICE_CTL_BUNDLE_DECISION=1 打包），"
                "或直接跑源码版。",
                kind="download",
            )
            return

        root = plan.target

        def work() -> None:
            try:
                fetch_weights(root, which)
            except Exception as e:  # noqa: BLE001
                events.error(f"语义层权重下载失败：{type(e).__name__}: {e}", kind="download")
                events.warn("网络不通时把 HF_ENDPOINT 设成 https://hf-mirror.com 再试", kind="download")
            finally:
                self._reload_after = "decision"

        events.info(
            f"开始下载语义层权重 {which}（约 900MB，来自 {spec['repo']}）→ {root}，进度见「日志」页",
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
        res, self._decision_result = self._decision_result, None
        if res is not None:
            self._show_decision(*res)
        flag, self._reload_after = self._reload_after, None
        if flag == "model":
            self._after_download()
        elif flag == "decision":
            self._check_decision()


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
