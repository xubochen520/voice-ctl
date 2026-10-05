"""PyInstaller 打包入口。

为什么不直接用 `voice_ctl/cli.py`：打包出来的 exe 有两个额外诉求——
  1. 双击时没有子命令，得给个**看得懂**的提示（控制台可能一闪而过，
     所以双击场景要弹对话框）
  2. 需要一条不依赖麦克风和热键的**自检**路径，用来验证打包是否完整
     （原生 DLL、模型、配置有没有漏）

这些都不该塞进 cli.py——那是给开发者用的命令行入口，保持干净。
"""

from __future__ import annotations

import sys


def _has_console() -> bool:
    """当前进程是否连着一个真实控制台。

    双击 exe 时 Windows 会给一个新的 console；但从 GUI 启动、或 stdout
    被重定向时（比如某些启动器）就没有。用 GetConsoleWindow 判定最直接。
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return True


def _msgbox(title: str, text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        print(f"{title}\n{text}")


HELP_TEXT = """voice-ctl —— 按住快捷键说话，本地离线识别后执行动作

用法：
  voice-ctl.exe                  打开图形界面（日志 / 快捷键 / 功能 / 设置）
  voice-ctl.exe ui               同上，显式写法
  voice-ctl.exe run              不开界面，常驻监听热键（适合开机自启）
  voice-ctl.exe doctor           体检：模型/配置/动作/麦克风逐项检查
  voice-ctl.exe download         下载识别模型（首次必做，约 226MB）
  voice-ctl.exe test             用自带样例音频验证识别
  voice-ctl.exe simulate 打开微信   不开麦克风，直接测匹配
  voice-ctl.exe devices          列出麦克风
  voice-ctl.exe --selftest       打包自检（不碰麦克风与热键）
  voice-ctl.exe --help           完整帮助

首次使用建议顺序：双击打开界面 → 在「设置」页下载模型 → 在「运行」页点启动
"""


def _selftest() -> int:
    """打包自检：不碰麦克风、不碰热键，只验证文件和质量。

    这是判断「exe 打全了没有」的权威手段——导入失败、DLL 缺失、
    模型没打进去，都会在这里暴露。
    """
    from voice_ctl import __version__, bootstrap

    # 必须最先调用：不切 UTF-8 的话，下面第一个 ✓ 就会 UnicodeEncodeError
    bootstrap.setup_console()
    say = bootstrap.safe_print

    ok = True
    say(f"voice-ctl {__version__} 自检")
    say(bootstrap.describe())
    say()

    say("--- 依赖导入 ---")
    for mod, why in (
        ("numpy", "音频处理"),
        ("sherpa_onnx", "语音识别（含原生 DLL）"),
        ("sounddevice", "录音（含 PortAudio DLL）"),
        ("pynput", "全局热键"),
        ("tkinter", "图形界面"),
        ("pypinyin", "中文同音纠错（可选）"),
    ):
        try:
            m = __import__(mod)
            ver = f"Tk {m.TkVersion}" if mod == "tkinter" else getattr(m, "__version__", "")
            say(f"  ✓ {mod:14} {ver:12} {why}")
        except Exception as e:  # noqa: BLE001
            tag = "可选" if mod == "pypinyin" else "必需"
            say(f"  ✗ {mod:14} [{tag}] {why} -> {type(e).__name__}: {e}")
            if mod != "pypinyin":
                ok = False

    say("\n--- 图形界面（真正建一次窗口）---")
    try:
        import tkinter as tk
        from tkinter import ttk

        from voice_ctl.ui import theme

        theme.enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        theme.init(root)  # 字体与全部 ttk 样式都在这里配，配置错了这里就会炸
        ttk.Button(root, text="x", style="Primary.TButton").pack()
        ttk.Entry(root).pack()
        ttk.Combobox(root, values=["a"]).pack()
        ttk.Treeview(root, columns=["a"]).pack()
        root.update_idletasks()
        say(f"  ✓ 窗口与 ttk 样式就绪（缩放 {theme.SCALE:.2f}×，字体 {theme.FONTS['body'][0]}）")
        if not theme.apply_dark_titlebar(root):
            say("  · 深色标题栏没设上（只影响观感）")
        root.destroy()
    except Exception as e:  # noqa: BLE001
        say(f"  ✗ 图形界面建不起来: {type(e).__name__}: {e}")
        say("    命令行功能不受影响（run / doctor / simulate 都还能用）")
        ok = False

    say("\n--- 识别器构造（真正加载 ONNX 图）---")
    try:
        from voice_ctl.asr import Asr
        from voice_ctl.cli import model_dir_for
        from voice_ctl.config import load_config

        cfg = load_config(None)
        md = model_dir_for(cfg)
        say(f"  模型目录: {md}")
        if not (md / "model.int8.onnx").is_file():
            say("  ✗ 缺少 model.int8.onnx")
            say("    精简版需要先下载：voice-ctl.exe download")
            say("    或用完整版（内置模型，免下载）")
            ok = False
        else:
            a = Asr(md, num_threads=2)
            a.load()
            say(f"  ✓ 识别器加载成功（{a.load_ms:.0f}ms）")
            wav = md / "test_wavs" / "zh.wav"
            if wav.is_file():
                r = a.transcribe_file(wav)
                say(f"  ✓ 实际识别成功: {r.text!r}  ({r.summary()})")
            else:
                say("  · 无样例音频，跳过实际推理")
    except Exception as e:  # noqa: BLE001
        import traceback

        say(f"  ✗ 识别器构造失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        ok = False

    say("\n--- 音频设备（不打开，只枚举）---")
    try:
        from voice_ctl.recorder import list_input_devices

        devs = list_input_devices()
        say(f"  ✓ 枚举到 {len(devs)} 个输入设备")
        for idx, name, rate, default in devs[:4]:
            say(f"      {'★' if default else ' '} [{idx}] {name} ({rate}Hz)")
        if not devs:
            say("  ⚠ 没有输入设备 —— 录音会失败")
            ok = False
    except Exception as e:  # noqa: BLE001
        say(f"  ✗ 设备枚举失败: {type(e).__name__}: {e}")
        ok = False

    say("\n--- 动作与配置 ---")
    try:
        from voice_ctl.actions import build_registry
        from voice_ctl.config import load_config

        cfg = load_config(None)
        reg = build_registry(cfg.enabled_actions)
        src = cfg.source if cfg.source else "(内置默认动作表)"
        say(f"  ✓ {len(reg)} 个动作已注册   配置来源: {src}")
        bad = [a.id for a in cfg.enabled_actions if not reg.get(a.id).preflight().ok]
        if bad:
            say(f"  · {len(bad)} 个动作目标找不到（多半是没装对应软件）：{', '.join(bad)}")
    except Exception as e:  # noqa: BLE001
        say(f"  ✗ 动作构建失败: {type(e).__name__}: {e}")
        ok = False

    say()
    say("自检" + ("通过 ✓" if ok else "失败 ✗"))
    if not ok:
        say("把上面完整输出贴到 issue 里，能直接定位是缺文件还是环境问题。")
    return 0 if ok else 1


def main() -> int:
    # 编码必须最先处理：打包后控制台是 GBK，任何中文/符号输出都会炸
    from voice_ctl.bootstrap import setup_console

    setup_console()

    # --selftest 要在 cli 之前拦下来：它连 argparse 都不该依赖
    if "--selftest" in sys.argv[1:]:
        try:
            return _selftest()
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            print(f"\n自检本身崩了: {type(e).__name__}: {e}")
            return 1

    from voice_ctl.cli import main as cli_main

    argv = sys.argv[1:]

    # 无参数 = 双击场景。打开图形界面，而不是进去之后只有一个黑框在等热键：
    # 用户双击一个 exe，期待的是"看到东西"；要常驻监听请显式用 `run`。
    if not argv:
        argv = ["ui"]

    if argv[0] in ("--help", "-h"):
        print(HELP_TEXT)
        return 0
    if argv[0] in ("--version", "-V"):
        from voice_ctl import __version__

        print(f"voice-ctl {__version__}")
        return 0

    rc = cli_main(argv)

    # 双击运行时，出错信息需要留住——否则窗口关了用户什么都没看到
    if not _has_console() and rc != 0:
        _msgbox("voice-ctl 出错了", f"退出码 {rc}\n\n请用命令行运行 `voice-ctl.exe doctor` 看详细原因。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
