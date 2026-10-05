"""命令行入口。所有能力都从这里暴露，方便脚本化诊断。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .actions import build_registry
from .app import Pipeline
from .asr import Asr, AsrError
from .config import AppConfig, ConfigError, load_config
from .matcher import Matcher
from .normalize import NormalizeConfig, Normalizer
from .recorder import Recorder, RecorderError, list_input_devices, play_beep
from .session import SessionController

HF_BASE = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/resolve/main"
)
MODEL_FILES = [
    ("model.int8.onnx", 226.0),
    ("tokens.txt", 0.3),
    ("test_wavs/zh.wav", 0.17),
    ("test_wavs/en.wav", 0.22),
]


# --------------------------------------------------------------------------- #
# 组装运行时
# --------------------------------------------------------------------------- #


@dataclass
class Runtime:
    cfg: AppConfig
    normalizer: Normalizer
    matcher: Matcher
    registry: object
    asr: Asr
    decider: object | None = None

    def pipeline(self) -> Pipeline:
        return Pipeline(
            asr=self.asr,
            matcher=self.matcher,
            registry=self.registry,  # type: ignore[arg-type]
            actions=self.cfg.enabled_actions,
            normalizer=self.normalizer,
            decider=self.decider,
            min_confidence=self.cfg.decision.min_confidence,
        )


def build_runtime(cfg: AppConfig, *, with_asr: bool = True, with_decision: bool | None = None) -> Runtime:
    norm = Normalizer(
        NormalizeConfig(
            strip_prefixes=cfg.match.strip_prefixes,
            strip_suffixes=cfg.match.strip_suffixes,
            inline_fillers=cfg.match.inline_fillers,
            substitutions=cfg.normalize.substitutions,
            use_pinyin=cfg.normalize.use_pinyin,
        )
    )
    matcher = Matcher(cfg.enabled_actions, normalizer=norm, threshold=cfg.match.threshold)
    registry = build_registry(cfg.enabled_actions)
    asr = Asr(
        cfg.model_path(),
        language=cfg.model.language,
        use_itn=cfg.model.use_itn,
        num_threads=cfg.model.num_threads,
        provider=cfg.model.provider,
    )

    decider = None
    want = cfg.decision.enabled if with_decision is None else with_decision
    if want:
        from .decision import DecisionUnavailable, SemanticDecider

        try:
            decider = SemanticDecider(
                cfg.enabled_actions, cfg.decision, root=cfg.decision_path()
            )
            decider.load()
        except DecisionUnavailable as e:
            print(f"⚠ 语义层启用失败，已回落到仅别名匹配：{e}", file=sys.stderr)
            decider = None

    return Runtime(cfg, norm, matcher, registry, asr, decider)


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"voice-ctl {__version__}")
    print(f"Python   : {sys.version.split()[0]} ({sys.executable})")
    print(f"平台     : {sys.platform}")

    print("\n--- 配置文件 ---")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"✗ 配置有问题：{e}")
        return 2
    print(f"✓ 已加载 {cfg.source}")
    print(cfg.describe())

    print("\n--- 模型 ---")
    mp = cfg.model_path()
    model = mp / "model.int8.onnx"
    tokens = mp / "tokens.txt"
    if model.is_file() and tokens.is_file():
        print(f"✓ {model.name} ({model.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"✓ {tokens.name}")
    else:
        print(f"✗ 模型缺失于 {mp}；跑 `voice-ctl download` 自动下载")
        return 2

    print("\n--- 依赖 ---")
    for mod, why in (
        ("numpy", "音频处理"),
        ("sherpa_onnx", "语音识别"),
        ("sounddevice", "录音"),
        ("pynput", "热键与按键"),
        ("pypinyin", "中文同音纠错（可选）"),
    ):
        try:
            __import__(mod)
            print(f"✓ {mod:14} {why}")
        except ImportError:
            tag = "可选，建议装" if mod == "pypinyin" else "必需！"
            print(f"✗ {mod:14} {why}  ← {tag}")

    print("\n--- 语义层 ---")
    from .decision import available

    wdir = cfg.decision_path()
    ok, reason = available(wdir)
    print(f"{'✓' if ok else '·'} {reason}")
    print(f"  权重目录：{wdir}")
    if not ok:
        print("  下载：voice-ctl fetch-decision   （约 900MB，中文务必用 multilingual）")
        if cfg.decision.enabled:
            print("  ⚠ 配置里开了语义层但权重不可用，运行时会回落到仅别名匹配")
    if not cfg.decision.enabled:
        print("  （配置里 [decision].enabled = false，语义层不参与判断）")

    print("\n--- 动作预检 ---")
    try:
        rt = build_runtime(cfg, with_decision=False)
    except Exception as e:  # noqa: BLE001
        print(f"✗ 构建运行时失败：{e}")
        return 2

    # 区分两类失败：
    #   hard —— 系统自带的东西（notepad/calc/音量…）找不到，说明环境或配置有问题
    #   soft —— 第三方应用（微信等）没装，这是**正常状态**，不该判 doctor 失败
    HARD = {"open.notepad", "open.calc", "open.explorer", "open.taskmgr", "open.settings"}
    hard_bad, soft_missing = 0, 0
    for a in cfg.enabled_actions:
        act = rt.registry.get(a.id)  # type: ignore[attr-defined]
        r = act.preflight()
        if r.ok:
            print(f"✓ {a.id:24} {r.message}" + (f"  [{r.detail}]" if r.detail else ""))
            continue
        if a.id in HARD or a.handler != "open_app":
            hard_bad += 1
            print(f"✗ {a.id:24} {r.message}" + (f"  [{r.detail}]" if r.detail else ""))
        else:
            soft_missing += 1
            print(f"· {a.id:24} {r.message}  ← 没装这个应用，属正常；装了就能用")

    print("\n--- 热键 ---")
    from .hotkey import HotkeyError, parse_hotkey

    try:
        hk = parse_hotkey(cfg.hotkey.keys)
        print(f"✓ {cfg.hotkey.keys}  →  {'+'.join(sorted(hk.keys))}")
        low = cfg.hotkey.keys.lower().replace(" ", "")
        if low in ("<ctrl>+space", "<ctrl>+<space>"):
            print("⚠ 纯 Ctrl+Space 在中文 Windows 上是输入法切换键，会被系统抢走。建议加 alt。")
    except HotkeyError as e:
        print(f"✗ {e}")
        return 2

    print("\n--- 音频设备 ---")
    try:
        for idx, name, rate, default in list_input_devices():
            star = "★默认" if default else "     "
            print(f"{star} [{idx:2}] {name}  ({rate}Hz)")
    except RecorderError as e:
        print(f"✗ {e}")
        return 2

    if hard_bad:
        print(f"\n结论：{hard_bad} 个动作有问题（系统动作跑不通，需要修）")
        return 1
    concl = "结论：全部就绪，可以 voice-ctl run 了"
    if soft_missing:
        concl += f"（{soft_missing} 个第三方应用未安装，不影响使用）"
    print("\n" + concl)
    return 0


def cmd_devices(args: argparse.Namespace) -> int:  # noqa: ARG001
    try:
        devs = list_input_devices()
    except RecorderError as e:
        print(f"✗ {e}")
        return 2
    if not devs:
        print("没找到任何输入设备。检查麦克风是否插好、系统是否授权。")
        return 1
    print("可用输入设备（把序号填到 config.toml 的 [audio].device）：")
    for idx, name, rate, default in devs:
        print(f"  {'★默认' if default else '     '} [{idx:2}] {name}  ({rate}Hz)")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    import urllib.request

    try:
        cfg = load_config(args.config)
        target = cfg.model_path()
    except ConfigError:
        target = Path(args.dir or "models/sense-voice-int8").resolve()

    target.mkdir(parents=True, exist_ok=True)
    print(f"下载 SenseVoice int8 到 {target}")
    print("（来自 HuggingFace csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09）")

    failed: list[str] = []
    for rel, approx_mb in MODEL_FILES:
        dest = target / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and dest.stat().st_size > 1024:
            print(f"  跳过 {rel}（已存在 {dest.stat().st_size / 1024 / 1024:.1f} MB）")
            continue
        url = f"{HF_BASE}/{rel}"
        print(f"  下载 {rel} (~{approx_mb} MB) ...", end="", flush=True)
        try:
            tmp = dest.with_suffix(dest.suffix + ".part")
            with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:  # noqa: S310
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
            print(f" ok ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
        except Exception as e:  # noqa: BLE001
            print(f" 失败：{e}")
            failed.append(rel)

    if failed:
        print("\n以下文件下载失败：" + ", ".join(failed))
        print("网络不通时可用 HF 镜像：把 HF_ENDPOINT=https://hf-mirror.com 设进环境变量后重试。")
        return 1
    print("\n✓ 模型就绪。跑 `voice-ctl doctor` 确认。")
    return 0


def cmd_fetch_decision(args: argparse.Namespace) -> int:
    """下载 Laya 语义层的 ONNX 权重。

    官方仓库不发布 ONNX 图，所以图取自社区导出；config 和 tokenizer 走官方仓库
    （必须同源，否则 tokenizer 与权重不匹配）。中文务必选 multilingual。
    """
    from .decision import WEIGHTS, DecisionUnavailable, fetch_weights

    try:
        cfg = load_config(args.config)
        root = cfg.decision.onnx_dir
    except ConfigError:
        root = args.dir or "models/laya-onnx"

    which = args.model
    spec = WEIGHTS.get(which)
    if spec is None:
        print(f"✗ 不认识 {which!r}；可选：{', '.join(WEIGHTS)}")
        return 2

    print(f"下载 Laya ONNX 权重 → {root}")
    print(f"  checkpoint : {which}")
    print(f"  来源       : {spec['repo']}")
    print(f"  说明       : {spec['note']}")
    print("  ⚠ 这是社区导出，非官方发布；约 900MB，请确认磁盘空间\n")

    try:
        done = fetch_weights(root, which)
    except DecisionUnavailable as e:
        print(f"✗ {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"✗ 下载失败：{type(e).__name__}: {e}")
        print("  网络不通时可设 HF_ENDPOINT=https://hf-mirror.com 后重试")
        return 1

    if not done:
        print("所有文件已存在，无需下载。")
    else:
        for d in done:
            print(f"  ✓ {d}")
    print(f"\n✓ 权重就绪：{root}")
    print("  在 config.toml 里设 [decision].enabled = true 即可启用。")
    print("  建议先跑 `voice-ctl doctor` 确认，再用 simulate 验证。")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """用自带样例音频跑一次真识别，验证模型能工作。"""
    try:
        cfg = load_config(args.config)
        rt = build_runtime(cfg, with_decision=False)
    except ConfigError as e:
        print(f"✗ {e}")
        return 2

    mp = cfg.model_path()
    wavs = sorted((mp / "test_wavs").glob("*.wav"))
    if not wavs:
        print(f"✗ {mp / 'test_wavs'} 里没有样例音频；跑 `voice-ctl download`")
        return 2

    print(f"加载模型 {mp} ...", end="", flush=True)
    try:
        rt.asr.load()
    except AsrError as e:
        print(f"\n✗ {e}")
        return 2
    print(f" ok（{rt.asr.load_ms:.0f}ms）")

    ok = True
    for w in wavs:
        try:
            r = rt.asr.transcribe_file(w)
        except AsrError as e:
            print(f"✗ {w.name}: {e}")
            ok = False
            continue
        print(f"  {w.name:10} {r.summary()}")
        print(f"             {r.text}")
    return 0 if ok else 1


def _build_rt_or_die(args: argparse.Namespace, **kw) -> Runtime | None:
    try:
        cfg = load_config(args.config)
        return build_runtime(cfg, **kw)
    except ConfigError as e:
        print(f"✗ 配置错误：{e}")
    except Exception as e:  # noqa: BLE001
        print(f"✗ 初始化失败：{type(e).__name__}: {e}")
    return None


def cmd_simulate(args: argparse.Namespace) -> int:
    """不打字、不说话，直接把文本喂进「归一化→匹配→执行」链路。"""
    rt = _build_rt_or_die(args, with_asr=False, with_decision=None if not args.no_decision else False)
    if rt is None:
        return 2
    pipe = rt.pipeline()

    texts = list(args.text)
    if not texts:
        if not sys.stdin.isatty():
            texts = [ln.strip() for ln in sys.stdin if ln.strip()]
        if not texts:
            texts = ["打开微信", "帮我打开记事本", "截个屏", "音量小一点", "随便说点什么"]
            print("（没给文本，用内置样例）")

    rc = 0
    for t in texts:
        out = pipe.process_text(t, dry_run=args.dry_run)
        print(f"\n--- 输入: {t!r} ---")
        print(out.report(verbose=True))
        if not out.ok and not args.dry_run:
            rc = 1
    return rc


def cmd_record(args: argparse.Namespace) -> int:
    """录一段固定时长的音频存成 wav（不识别），用于对比不同麦克风/环境。"""
    rt = _build_rt_or_die(args, with_decision=False)
    if rt is None:
        return 2
    cfg = rt.cfg
    secs = args.seconds
    out = Path(args.out).resolve()

    rec = Recorder(
        device=cfg.audio.device or None,
        max_duration_ms=int(secs * 1000) + 1000,
    )
    print(f"按回车开始录 {secs} 秒 ...")
    try:
        input()
    except EOFError:
        pass

    if cfg.feedback.beep:
        play_beep(cfg.feedback.beep_start_hz, cfg.feedback.beep_ms)
    try:
        rec.start()
    except RecorderError as e:
        print(f"✗ {e}")
        return 2
    print(f"● 录音中 ... {secs}s", end="", flush=True)
    time.sleep(secs)
    r = rec.stop()
    if cfg.feedback.beep:
        play_beep(cfg.feedback.beep_end_hz, cfg.feedback.beep_ms)
    print(f"\n停止。{r.summary()}")
    print(f"          峰值/RMS = {r.peak / r.rms:.1f}" if r.rms > 0 else "          RMS 为 0（没采到任何信号）")
    if r.clipped:
        print("⚠ 峰值触顶（爆音）：把系统麦克风音量调低一点")
    reason = r.gate_reason(cfg.audio.min_peak)
    if reason:
        print(f"⚠ {reason} —— 这段录音会被丢弃")
        print("  若你确实说话了，说明麦克风声音太小或设备选错：")
        print("  · 跑 `voice-ctl devices` 确认用的是哪个麦克风")
        print("  · 在 设置 → 系统 → 声音 里提高输入音量")
        print("  · 或把 config.toml 的 [audio].min_peak 调低（例如 0.003）")
    else:
        good = r.peak / r.rms > 4 if r.rms > 0 else False
        print(f"✓ 会通过静音过滤" + ("（波形像语音：峰值明显高于底噪）" if good
                                      else "（但峰值/RMS 偏低，可能只是环境噪声）"))

    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = (r.samples * 32767.0).clip(-32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(pcm.tobytes())
    print(f"已写入 {out}")

    if args.transcribe:
        try:
            res = rt.asr.transcribe(r.samples)
            print(f"\n识别: {res.text}")
            print(f"      {res.summary()}")
        except AsrError as e:
            print(f"✗ 识别失败：{e}")
            return 2
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    """单次：按回车录一段 → 识别 → 匹配 → 执行。不开全局热键。"""
    rt = _build_rt_or_die(args)
    if rt is None:
        return 2
    cfg = rt.cfg
    pipe = rt.pipeline()

    print("加载识别模型 ...", end="", flush=True)
    try:
        rt.asr.load()
    except AsrError as e:
        print(f"\n✗ {e}")
        return 2
    print(f" ok（{rt.asr.load_ms:.0f}ms）")

    rec = Recorder(device=cfg.audio.device or None, max_duration_ms=cfg.hotkey.max_duration_ms)
    rounds = args.rounds
    n = 0
    while rounds <= 0 or n < rounds:
        n += 1
        try:
            input(f"\n[{n}] 按回车开始录音，说完再按一次回车停止（Ctrl+C 退出）...")
        except (EOFError, KeyboardInterrupt):
            break

        if cfg.feedback.beep:
            play_beep(cfg.feedback.beep_start_hz, cfg.feedback.beep_ms)
        try:
            rec.start()
        except RecorderError as e:
            print(f"✗ {e}")
            return 2
        try:
            input("● 录音中 ... 按回车停止")
        except (EOFError, KeyboardInterrupt):
            rec.abort()
            break
        r = rec.stop()
        if cfg.feedback.beep:
            play_beep(cfg.feedback.beep_end_hz, cfg.feedback.beep_ms)

        if r.duration_s * 1000 < cfg.hotkey.min_duration_ms:
            print(f"太短（{r.duration_s * 1000:.0f}ms），忽略")
            continue
        reason = r.gate_reason(cfg.audio.min_peak)
        if reason:
            print(f"{reason}，忽略（若确实说话了，把 [audio].min_peak 调低）")
            continue

        out = pipe.process_audio(r.samples, dry_run=args.dry_run)
        print(out.report(verbose=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """常驻：全局热键按住说话。这是最终形态。"""
    rt = _build_rt_or_die(args)
    if rt is None:
        return 2
    cfg = rt.cfg
    pipe = rt.pipeline()

    from .hotkey import HotkeyError, HotkeyListener, HotkeyTimer

    print(f"voice-ctl {__version__}")
    print(cfg.describe())
    print("\n加载识别模型 ...", end="", flush=True)
    try:
        rt.asr.load()
    except AsrError as e:
        print(f"\n✗ {e}")
        return 2
    _say(f" ok（{rt.asr.load_ms:.0f}ms，常驻内存）")

    rec = Recorder(device=cfg.audio.device or None, max_duration_ms=cfg.hotkey.max_duration_ms)

    def handle_recording(r) -> None:  # noqa: ANN001
        out = pipe.process_audio(r.samples, dry_run=args.dry_run)
        if cfg.feedback.print_result:
            print(out.report(verbose=args.verbose))

    # 会话状态机抽到 session.py：并发路径（监听线程 + 超时主循环）必须可单测。
    controller = SessionController(
        recorder=rec,
        timer=HotkeyTimer(cfg.hotkey.max_duration_ms, lambda: None),
        on_recording_start=lambda: (
            play_beep(cfg.feedback.beep_start_hz, cfg.feedback.beep_ms)
            if cfg.feedback.beep
            else None
        ),
        on_recording_end=lambda: (
            play_beep(cfg.feedback.beep_end_hz, cfg.feedback.beep_ms)
            if cfg.feedback.beep
            else None
        ),
        on_recording_ready=handle_recording,
        on_too_short=lambda ms: _say(f"· 太短（{ms:.0f}ms），忽略"),
        on_gated=lambda reason: _say(
            f"· {reason}，忽略（若确实说话了，把 [audio].min_peak 调低）"
        ),
        on_error=lambda msg: _say(f"✗ {msg}"),
        min_duration_ms=cfg.hotkey.min_duration_ms,
        min_peak=cfg.audio.min_peak,
        recorder_error=RecorderError,
    )
    # 超时守护接到会话上：录音超时由 controller.timeout() 统一收尾
    controller.timer = HotkeyTimer(cfg.hotkey.max_duration_ms, controller.timeout)

    def on_press() -> None:
        controller.press()
        if controller.recording:
            print("● 录音中 ...", end="", flush=True)

    def on_release() -> None:
        controller.release()

    try:
        listener = HotkeyListener(cfg.hotkey.keys, on_press=on_press, on_release=on_release)
        listener.start()
    except HotkeyError as e:
        print(f"✗ {e}")
        return 2

    print(f"\n✓ 就绪。按住 {cfg.hotkey.keys} 说话，松开执行。Ctrl+C 退出。", flush=True)
    print("  （若热键无反应，可能是被别的程序占用；换一个键试试）", flush=True)

    try:
        while True:
            controller.timer.tick()  # type: ignore[attr-defined]
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n退出中 ...")
        listener.stop()
        controller.abort()
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="voice-ctl",
        description="按住快捷键说话 → 本地离线识别 → 执行动作。纯离线，无需联网。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型用法：\n"
        "  voice-ctl doctor              检查环境、配置、动作、麦克风\n"
        "  voice-ctl download            下载识别模型（首次必做）\n"
        "  voice-ctl test                用样例音频验证识别\n"
        "  voice-ctl simulate 打开微信   不开麦克风，直接测匹配\n"
        "  voice-ctl run                 常驻，按住热键说话\n",
    )
    p.add_argument("-c", "--config", default=None, help="配置文件路径（默认 ./config.toml）")
    p.add_argument("-V", "--version", action="version", version=f"voice-ctl {__version__}")

    sub = p.add_subparsers(dest="cmd", metavar="命令")

    sp = sub.add_parser("doctor", help="检查环境与配置")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("devices", help="列出麦克风设备")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("download", help="下载 SenseVoice 模型")
    sp.add_argument("--dir", default=None, help="目标目录（默认取配置里的 [model].dir）")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser(
        "fetch-decision",
        help="下载 Laya 语义层 ONNX 权重（可选功能，约 900MB）",
    )
    sp.add_argument("--dir", default=None, help="目标目录（默认取 [decision].onnx_dir）")
    sp.add_argument(
        "--model",
        default="multilingual",
        help="要哪个 checkpoint（默认 multilingual；中文别用 english）",
    )
    sp.set_defaults(func=cmd_fetch_decision)

    sp = sub.add_parser("test", help="用自带样例音频验证识别")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("simulate", help="用文本测「匹配→执行」，不需要麦克风")
    sp.add_argument("text", nargs="*", help="要模拟的语音文本；留空则从 stdin 读或用内置样例")
    sp.add_argument("--dry-run", action="store_true", help="只显示会做什么，不真的执行")
    sp.add_argument("--no-decision", action="store_true", help="强制关闭语义层，只测别名匹配")
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("record", help="录一段音频存成 wav")
    sp.add_argument("--seconds", type=float, default=3.0, help="录音秒数（默认 3）")
    sp.add_argument("--out", default="assets/record.wav", help="输出路径")
    sp.add_argument("--transcribe", action="store_true", help="录完顺便识别")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("listen", help="单次录音并执行（不开全局热键，便于调试）")
    sp.add_argument("--rounds", type=int, default=1, help="循环次数，0 = 无限（默认 1）")
    sp.add_argument("--dry-run", action="store_true", help="只显示会做什么，不真的执行")
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("run", help="常驻运行：按住热键说话（最终形态）")
    sp.add_argument("--dry-run", action="store_true", help="只显示会做什么，不真的执行")
    sp.add_argument("-v", "--verbose", action="store_true", help="打印每步耗时")
    sp.set_defaults(func=cmd_run)

    return p


def _say(*args: object, **kw: object) -> None:
    """带 flush 的 print。

    重定向到文件/管道时 stdout 是块缓冲的：常驻进程（`run`）的关键提示
    （"就绪"、"录音中"）会卡在缓冲区里，Log 里看不到、也误导排查。
    """
    print(*args, **kw)  # type: ignore[arg-type]
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认不是 UTF-8，中文识别结果会变乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
