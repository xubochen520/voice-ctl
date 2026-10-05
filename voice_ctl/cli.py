"""命令行入口。所有能力都从这里暴露，方便脚本化诊断。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import __version__
from .asr import AsrError
from .config import AppConfig, ConfigError, load_config
from .recorder import Recorder, RecorderError, list_input_devices, play_beep
from .runner import Engine, Runtime, build_runtime, model_dir_for

__all__ = [
    "Runtime",
    "build_parser",
    "build_runtime",
    "main",
    "model_dir_for",
]

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

# Runtime / build_runtime / model_dir_for 现在住在 runner.py —— UI 也要用它们，
# 放在命令行模块里会让 UI 反向依赖 CLI。这里 re-export 只为兼容老写法。


# --------------------------------------------------------------------------- #
# 组装运行时
# --------------------------------------------------------------------------- #

# 具体实现在 runner.py（UI 与 CLI 共用）。这里只留一个兼容层说明。


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import bootstrap

    print(f"voice-ctl {__version__}")
    print(f"Python   : {sys.version.split()[0]}")
    print(bootstrap.describe())
    print(f"运行日志  : {bootstrap.log_path()}")

    print("\n--- 配置文件 ---")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"✗ 配置有问题：{e}")
        return 2
    if cfg.source is None:
        print("· 没找到配置文件，使用的是**内置默认动作表**")
        print("  想自定义就复制一份 config.toml 到程序同级目录，或用 --config 指定")
    else:
        print(f"✓ 已加载 {cfg.source}")
    print(cfg.describe())

    print("\n--- 模型 ---")
    mp = model_dir_for(cfg)
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

    print("\n--- 意图层与日程 ---")
    from .schedule import ScheduleStore, default_store_path

    if cfg.intent.enabled:
        from .apps import AppIndex

        idx = AppIndex(use_pinyin=cfg.normalize.use_pinyin)
        n_apps = len(idx.entries())
        if n_apps:
            print(f"✓ 意图层开启：动词/否定/时间识别 + {n_apps} 个已安装应用可动态打开")
        else:
            print("· 意图层开启，但读不到开始菜单应用列表（只能开配置里写过的应用）")
            print("  试：powershell -Command Get-StartApps   （有输出就说明系统能给）")
    else:
        print("· 意图层关闭（[intent].enabled = false），只用别名匹配")

    if cfg.schedule.enabled:
        sp = cfg.schedule_path() or default_store_path()
        st = ScheduleStore(sp)
        if st.load_error:
            print(f"✗ 日程文件有问题：{st.load_error}")
        else:
            pend = st.pending()
            nxt = st.next_due()
            tail = f"，最近一条 {nxt:%m-%d %H:%M}" if nxt else ""
            print(f"✓ 日程 {st.path}：待提醒 {len(pend)} 条{tail}")
        print("  看全部：voice-ctl schedule    导出：voice-ctl schedule --export 日程.ics")
    else:
        print("· 日程关闭（[schedule].enabled = false）")

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
    from . import bootstrap
    from .fetch import download_asr_model

    try:
        cfg = load_config(args.config)
        target = model_dir_for(cfg)
    except ConfigError:
        target = Path(args.dir).resolve() if args.dir else bootstrap.default_model_dir()

    failed = download_asr_model(target, force=getattr(args, "force", False))
    return 1 if failed else 0


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

    mp = model_dir_for(cfg)
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


def _parse_now(text: str) -> datetime | None:
    """把 `--now` 的字符串解析成 datetime。接受几种常见写法，不接受模糊猜测。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        return dt.replace(year=datetime.now().year) if fmt == "%m-%d %H:%M" else dt
    return None


def _fmt_progress(p) -> str:  # noqa: ANN001 - llamacpp.Progress
    if p.total:
        pct = p.ratio * 100
        return f"\r  {p.note} {p.done / 1024 / 1024:6.1f}/{p.total / 1024 / 1024:.1f} MB ({pct:5.1f}%)"
    return f"\r  {p.note} {p.done / 1024 / 1024:6.1f} MB"


def cmd_llm(args: argparse.Namespace) -> int:
    """装运行时 / 下模型 / 探测 / 试句子。

    这条命令存在的意义是**先量再开**：`[llm]` 是唯一需要额外下载几百 MB 的功能，
    开之前用户应该亲眼看到"它准备好了吗、它挑得对不对、一次花多久"。
    """
    from . import llamacpp
    from .llm import DEFAULT_ENDPOINT, make_client, candidate_actions

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"✗ 配置有问题：{e}")
        return 2

    lc = cfg.llm
    backend = "server" if args.backend == "server" else lc.backend
    if args.endpoint or args.model or args.backend:
        lc = replace(
            lc, backend=backend,
            endpoint=args.endpoint or lc.endpoint,
            model=args.model or lc.model,
        )
    # 这条命令本身就是"试用"，不该因为 enabled = false 就什么都不给看——
    # 那样用户得先改配置、再跑一条命令才发现装错了模型。配置只在**运行期**
    # 生效（engine 里判断），这里只看命令行。
    lc = replace(lc, enabled=True)

    # -- 装运行时 ---------------------------------------------------------- #
    if args.install:
        print(f"下载 llama.cpp 运行时（{llamacpp.LLAMA_BUILD}，Windows x64 CPU 版）…")
        print(f"  来源 {llamacpp.WIN_CPU_URL}")
        try:
            d = llamacpp.install_runtime(force=args.force, on_progress=lambda p: print(_fmt_progress(p), end=""))
        except Exception as e:  # noqa: BLE001
            print(f"\n✗ 安装失败：{e}")
            return 1
        files = sorted(x.name for x in d.glob("*"))
        size = sum(x.stat().st_size for x in d.iterdir() if x.is_file()) / 1024 / 1024
        print(f"\n✓ 已装到 {d}")
        print(f"  {len(files)} 个文件，{size:.1f} MB（只留了能跑 llama-server 的最小集）")
        print(f"  日志：{llamacpp.data_dir() / 'llama-server.log'}")

    # -- 下模型 ------------------------------------------------------------ #
    if args.download:
        spec = llamacpp.MODELS.get(args.download if isinstance(args.download, str) else "")
        if args.download is True or args.download == "":
            spec = llamacpp.MODELS.get(lc.model, llamacpp.QWEN_05B)
        if spec is None:
            print(f"✗ 不认识这个模型名：{args.download}")
            print("  可选：" + "、".join(f"{m.name}（{m.size_mb:.0f}MB）" for m in llamacpp.MODELS.values()))
            return 2
        print(f"下载 {spec.name}（约 {spec.size_mb:.0f} MB）…")
        print(f"  {spec.note}")
        try:
            p = llamacpp.download_model(
                spec, force=args.force, on_progress=lambda x: print(_fmt_progress(x), end="")
            )
        except Exception as e:  # noqa: BLE001
            print(f"\n✗ 下载失败：{e}")
            return 1
        print(f"\n✓ {p}（{p.stat().st_size / 1024 / 1024:.0f} MB）")

    # -- 状态 -------------------------------------------------------------- #
    if args.status or args.install or args.download:
        print("\n--- 小模型层状态 ---")
        deps = build_llm_status(cfg)
        for line in deps:
            print(line)

    if args.install or args.download:
        if not (args.text or args.probe):
            return 0

    # -- 探测 + 试句子 ----------------------------------------------------- #
    client = make_client(lc)
    if client is None:
        print("· 造不出小模型客户端（配置里的 backend 有问题？）")
        return 2

    print(f"探测：{client.describe()}")
    if not client.probe(force=True):
        err = getattr(client, "last_error", "")
        print(f"✗ 不可用：{err}")
        if backend == "server":
            print(f"  这个地址上没有 OpenAI 兼容服务：{lc.endpoint}")
            print("  LM Studio：打开本地服务（默认 1234）；Ollama：ollama serve（11434）")
            print("  或者改用内置的：voice-ctl llm --backend local --install --download --probe")
        else:
            print("  内置后端需要两步：voice-ctl llm --install  然后  voice-ctl llm --download")
        client.close()
        return 1
    print(f"✓ {client.describe()}")
    if args.probe and not args.text:
        client.close()
        return 0

    texts = list(args.text)
    if not texts:
        if not sys.stdin.isatty():
            texts = [ln.strip() for ln in sys.stdin if ln.strip()]
        if not texts:
            texts = ["有个文件要改一下", "把声音关小一点", "算个数", "随便说点什么"]
            print("（没给文本，用内置样例）")

    cands = candidate_actions(cfg.enabled_actions, args.candidates or lc.max_candidates)
    print(f"候选 {len(cands)} 个（[llm].max_candidates 控制）：{', '.join(c[0] for c in cands)}")
    rc = 0
    for t in texts:
        s = client.suggest(t, cands)
        if s is None:
            print(f"\n「{t}」→ 没拿到结果（{getattr(client, 'last_error', '') or '输出解析不了'}）")
            rc = 1
            continue
        verdict = s.action_id or "(null：它认为候选里没有合适的)"
        known = "" if s.action_id is None or s.action_id in {c[0] for c in cands} else "  ← 编的，会被忽略"
        print(f"\n「{t}」→ {verdict}{known}   {s.ms:.0f}ms")
        if s.raw and s.raw != (s.action_id or ""):
            print(f"    原始输出：{s.raw!r}")
    client.close()
    return rc


def build_llm_status(cfg) -> list[str]:  # noqa: ANN001 - AppConfig
    """小模型层的就绪状态。doctor 和 `llm --status` 共用，避免两处说法不一致。"""
    from . import llamacpp

    out: list[str] = []
    lc = cfg.llm
    out.append(f"后端       : {lc.backend}"
               + ("（内置 llama.cpp，不需要外部软件）" if lc.backend == "local" else f"  {lc.endpoint}"))
    if lc.backend == "server":
        out.append("外部服务   : 由你自己启动，本程序不管理它的进程")
        return out

    rt = llamacpp.default_runtime_dir()
    if llamacpp.server_ready(rt):
        n = len(list(rt.iterdir()))
        size = sum(f.stat().st_size for f in rt.iterdir() if f.is_file()) / 1024 / 1024
        out.append(f"运行时     : ✓ {rt}（{n} 个文件 {size:.1f} MB）")
    else:
        out.append(f"运行时     : ✗ 没装（{rt}）")
        out.append("             装：voice-ctl llm --install   （约 17.7MB 下载 / 39.8MB 磁盘）")

    m = llamacpp.find_model(lc.model)
    if m is not None:
        out.append(f"模型       : ✓ {m.name}（{m.stat().st_size / 1024 / 1024:.0f} MB）")
    else:
        spec = llamacpp.MODELS.get(lc.model, llamacpp.QWEN_05B)
        out.append(f"模型       : ✗ 没下（配置里要的是 {lc.model}）")
        out.append(f"             下：voice-ctl llm --download {spec.name}   （约 {spec.size_mb:.0f}MB）")
    out.append(f"配置       : {'开启' if lc.enabled else '关闭'}（[llm].enabled）")
    if llamacpp.server_ready(rt) and m is not None and not lc.enabled:
        out.append("             都装好了，把 [llm].enabled 改成 true 就能用")
    return out


def cmd_schedule(args: argparse.Namespace) -> int:
    """看、加、删日程，以及导出 .ics。

    语音是主要入口，但没有这条命令的话有几件事做不了：**导出**（用户想把提醒
    弄进手机日历）、**离线核对**（为什么不响？）、以及在没有麦克风的机器上
    先试试日程功能。
    """
    from .schedule import ScheduleStore, default_store_path, write_ics

    try:
        cfg = load_config(args.config)
        path = cfg.schedule_path()
    except ConfigError:
        path = None
    store = ScheduleStore(path or default_store_path())
    if store.load_error:
        print(f"⚠ {store.load_error}")

    if args.export:
        events = store.all() if args.all else store.pending()
        out = write_ics(args.export, events)
        print(f"✓ 已导出 {len(events)} 条到 {out}")
        print("  导入方式：双击它（Windows 日历/Outlook），或发到手机上点开")
        return 0

    if args.clear:
        n = store.clear_finished()
        print(f"✓ 清掉 {n} 条已提醒/已错过的")
        return 0

    items = store.all() if args.all else store.pending()
    if not items:
        print("（没有日程。说「明天早上八点提醒我开会」就能加一条）")
        return 0
    now = datetime.now()
    print(f"日程文件：{store.path}")
    for e in items:
        late = "（已错过）" if e.status == "missed" else ""
        head = "→" if e.status == "pending" and e.start >= now else " "
        print(f"{head} [{e.status:8}] {e.start:%Y-%m-%d %H:%M}  {e.title} {late}".rstrip())
        if e.note:
            print(f"      原话：{e.note}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """不打字、不说话，直接把文本喂进「意图 → 匹配 → 执行」链路。"""
    rt = _build_rt_or_die(args, with_asr=False, with_decision=None if not args.no_decision else False)
    if rt is None:
        return 2
    pipe = rt.pipeline()
    if args.no_intent:
        pipe.intent_enabled = False
    if args.now:
        fixed = _parse_now(args.now)
        if fixed is None:
            print(f'✗ --now 看不懂：{args.now!r}；写成 "2026-10-05 10:00"')
            return 2
        pipe._clock = lambda: fixed  # noqa: SLF001 - 只给 simulate 用，故意不留公开入口

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
    """常驻：全局热键按住说话。这是命令行形态的最终形态。"""
    rt = _build_rt_or_die(args)
    if rt is None:
        return 2
    cfg = rt.cfg

    # 事件总线已经在 main() 里接了控制台 sink，这里只管开引擎、转主循环。
    engine = Engine(cfg, dry_run=args.dry_run)
    try:
        if not engine.load_model():
            return 2
        if not engine.start():
            return 2
        print("\n  （若热键无反应，可能是被别的程序占用；换一个键试试）", flush=True)
        while True:
            engine.tick()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n退出中 ...")
    finally:
        engine.close()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """打开图形界面。日志、快捷键、功能、设置都在里面。"""
    try:
        from .ui import run_ui
    except ImportError as e:  # pragma: no cover - 精简 Python 可能没带 tkinter
        print(f"✗ 无法加载图形界面：{e}")
        print("  这个 Python 没带 tkinter。官方安装包默认带；conda/精简版可能需要单独装。")
        print("  命令行功能不受影响，可以继续用 run / doctor / simulate。")
        return 2
    return run_ui(args.config, dry_run=args.dry_run)


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="voice-ctl",
        description="按住快捷键说话 → 本地离线识别 → 执行动作。纯离线，无需联网。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型用法：\n"
        "  voice-ctl ui                  打开图形界面（日志 / 快捷键 / 功能 / 设置）\n"
        "  voice-ctl doctor              检查环境、配置、动作、麦克风\n"
        "  voice-ctl download            下载识别模型（首次必做）\n"
        "  voice-ctl test                用样例音频验证识别\n"
        "  voice-ctl simulate 打开微信   不开麦克风，直接测匹配\n"
        "  voice-ctl run                 常驻，按住热键说话（无界面）\n",
    )
    p.add_argument("-c", "--config", default=None, help="配置文件路径（默认 ./config.toml）")
    p.add_argument("-q", "--quiet", action="store_true", help="只输出警告与错误")
    p.add_argument("-V", "--version", action="version", version=f"voice-ctl {__version__}")

    sub = p.add_subparsers(dest="cmd", metavar="命令")

    sp = sub.add_parser("ui", help="打开图形界面（日志 / 快捷键 / 功能 / 设置）")
    sp.add_argument("--dry-run", action="store_true", help="界面上默认只报告不执行")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("doctor", help="检查环境与配置")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("devices", help="列出麦克风设备")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("download", help="下载 SenseVoice 模型")
    sp.add_argument("--dir", default=None, help="目标目录（默认取配置里的 [model].dir）")
    sp.add_argument("--force", action="store_true", help="即使文件已存在也重新下载")
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
    sp.add_argument("--no-intent", action="store_true", help="强制关闭意图层，退回纯别名匹配")
    sp.add_argument(
        "--now", default=None,
        help='把"现在"固定成某个时刻（如 "2026-10-05 10:00"），让日程解析可复现',
    )
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("schedule", help="查看 / 导出 / 清理日程提醒")
    sp.add_argument("--all", action="store_true", help="连已提醒、已错过的也列出来")
    sp.add_argument("--export", default=None, metavar="FILE.ics", help="导出成 .ics 日历文件")
    sp.add_argument("--clear", action="store_true", help="清掉已提醒/已错过/已取消的")
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("llm", help="本地小模型层：安装 / 下载模型 / 探测 / 试句子（可选功能）")
    sp.add_argument("text", nargs="*", help="要试的句子；留空则从 stdin 读或用内置样例")
    sp.add_argument("--install", action="store_true", help="下载并安装内置 llama.cpp 运行时（约 17.7MB）")
    sp.add_argument(
        "--download", nargs="?", const=True, default=None, metavar="MODEL",
        help="下载 GGUF 模型；不给名字就用配置里的（默认 qwen2.5-0.5b-instruct，469MB）",
    )
    sp.add_argument("--status", action="store_true", help="只看就绪状态，不下载也不探测")
    sp.add_argument("--force", action="store_true", help="已存在也重新下载")
    sp.add_argument("--backend", default=None, choices=["local", "server"], help="覆盖 [llm].backend")
    sp.add_argument("--endpoint", default=None, help="覆盖 [llm].endpoint（backend=server 时）")
    sp.add_argument("--model", default=None, help="覆盖 [llm].model")
    sp.add_argument("--candidates", type=int, default=0, help="给模型看几个候选动作")
    sp.add_argument("--probe", action="store_true", help="只探测，不试句子")
    sp.set_defaults(func=cmd_llm)

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
    # 编码与缓冲：必须在任何中文输出之前。见 bootstrap.setup_console 的说明。
    # 事件总线也要在解析参数之前接上：连 argparse 的报错都该进日志文件。
    from . import events
    from .bootstrap import setup_console

    setup_console()
    raw_out = events.install_stream_tee()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0

    # `ui` 自己管显示：界面里已经有日志面板，再往控制台打一份纯属重复；
    # 而且控制台马上会被藏起来，写了也没人看。
    if args.cmd != "ui":
        events.attach_console(min_level="warn" if args.quiet else "info")
    events.attach_file()

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    finally:
        events.get_bus().flush(0.5)
        raw_out()


if __name__ == "__main__":
    sys.exit(main())
