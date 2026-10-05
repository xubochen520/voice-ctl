"""冻结（PyInstaller）与源码两种运行方式下的路径解析。

打包成 exe 之后，「当前目录」不再可信：
  * 用户可能在任意目录双击 exe，`os.getcwd()` 可能是 C:\\Windows\\System32
  * PyInstaller 把 data 解包到临时目录（`sys._MEIPASS`），那是**只读且用完即删**的
  * 模型有 226MB，不能每次启动都解包到临时目录

所以路径分三类，各有明确归属：

    只读资源（config.toml 默认、内置模型）
        冻结时在 _MEIPASS 里；源码时在项目根。
    可写数据（下载的模型）
        冻结时放 exe 同级目录（用户看得见、下次还在）；源码时放项目根。
    用户显式指定的路径（--config / --model-dir）
        一律绝对化后直接用，不做任何猜测。

这套区分的必要性是实测出来的：把模型塞进 _MEIPASS 会导致每次启动解包 226MB，
启动要几十秒且白占磁盘。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def setup_console() -> None:
    """把 stdout/stderr 切到 UTF-8，并关掉块缓冲。

    必须在**任何中文输出之前**调用。这是个实测踩出来的坑：中文 Windows 的
    控制台默认代码页是 GBK，输出 `✓`/`✗` 这类符号会直接抛 UnicodeEncodeError
    ——打包成 exe 后自检第一行就崩，而且报错指向打印语句，很难看出是编码问题。

    同时设 line_buffering：常驻进程（run）的关键提示会卡在缓冲区里，
    重定向到文件时看不到，误导排查。
    """
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 有些环境不支持 reconfigure，忽略即可
            pass


def safe_print(*args: object, **kw: object) -> None:
    """带 flush 的 print，编码失败也不抛。

    最后一道保险：即便 reconfigure 没生效（比如被 embedding 到别的宿主里，
    stdout 是个古怪的流），也不该因为一个勾号让整个程序挂掉。
    """
    try:
        print(*args, **kw)  # type: ignore[arg-type]
        sys.stdout.flush()
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))
    except Exception:  # noqa: BLE001
        pass


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> Path | None:
    """PyInstaller 的解包目录（只读、临时）。非冻结时为 None。"""
    if not is_frozen():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def exe_dir() -> Path:
    """exe 所在目录（冻结时）或项目根（源码时）。

    冻结时这是**唯一可写且稳定**的位置——用户能看见模型下到哪了，
    下次启动也不用重新下载。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # bootstrap.py 在 voice_ctl/ 下，上一级就是项目根
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """可写数据目录。优先 exe 同级；那里不可写（比如装在 Program Files）时退回用户目录。

    环境变量 `VOICE_CTL_DATA` 可以整个改掉位置（便携使用、多份配置并存、测试隔离）。
    """
    override = os.environ.get("VOICE_CTL_DATA", "").strip()
    if override:
        p = Path(override).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    base = exe_dir()
    try:
        probe = base / ".voice-ctl-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = Path.home() / ".voice-ctl"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def resource(*parts: str) -> Path | None:
    """在只读资源位置里找一个存在的文件/目录，找不到返回 None。

    搜索顺序：显式覆盖（环境变量） → 打包内 → exe 同级 → 可写数据目录。
    """
    candidates: list[Path] = []

    override = os.environ.get("VOICE_CTL_HOME")
    if override:
        candidates.append(Path(override).expanduser())

    bundle = _bundle_dir()
    if bundle is not None:
        candidates.append(bundle)

    candidates.append(exe_dir())
    candidates.append(data_dir())

    for base in candidates:
        p = base.joinpath(*parts)
        if p.exists():
            return p
    return None


def resolve_model_dir(configured: Path) -> Path:
    """把配置里的模型目录解析成实际可用的位置。

    三种情况的优先级：
      1. 配置指向的目录真的存在 → 用它（源码运行、或用户自己下了模型）
      2. 打包内嵌了模型（full 版）→ 用解包目录里的那份

    为什么需要第 2 条：完整版把 226MB 模型打进 exe，启动时解包到 _MEIPASS；
    而用户配置里的相对路径是相对 exe 同级目录的，那里没有模型。
    没有这个兜底，内嵌模型就白打了。
    """
    if configured.is_dir():
        return configured
    bundle = _bundle_dir()
    if bundle is not None:
        for sub in ("models/sense-voice-int8", "sense-voice-int8"):
            cand = bundle / sub
            if cand.is_dir():
                return cand
    return configured


def bundled_decision_dir() -> Path | None:
    """打包内嵌的语义层权重目录，没有就返回 None。

    单独抽出来是因为有两处要用，而且**必须用同一套路径**：
      1. `resolve_decision_dir()` —— 运行时找权重
      2. `ensure_user_config()`  —— 判断要不要自动开语义层
    两处路径不一致的表现是"权重要么找不到、要么明明在却不自动开"。
    """
    bundle = _bundle_dir()
    if bundle is None:
        return None
    for sub in ("models/laya-onnx/multilingual", "laya-onnx/multilingual"):
        cand = bundle / sub
        if cand.is_dir():
            return cand
    return None


def resolve_decision_dir(configured: Path) -> Path:
    """把配置里的语义层权重目录解析成实际可用的位置。

    和 `resolve_model_dir` 同样是**必须**的兜底，理由一模一样：配置里的
    `onnx_dir` 是相对路径，按配置文件所在目录解析，而打包后那份 config.toml
    在 exe 同级——那里没有权重，权重在 `_MEIPASS` 里。没有这个兜底，
    把 873MB 权重打进 exe 会毫无效果：程序照样说"缺少 *.onnx"。

    三种情况：
      1. 配置指向的目录真的存在 → 用它（源码运行、或用户自己下了权重）
      2. 打包内嵌了权重 → 用解包目录里的那份
      3. 都没有 → 原样返回，让调用方报"缺少权重"并给出下载指引

    另外兜一个 _MEIPASS 之外的常见布局：权重直接放在 exe 同级（用户手动
    下到那里、或想换一份权重而不重新打包）。这条排在配置之后、内嵌之前，
    因为它更贴近用户的显式意图——用户放进来的文件应当赢过打包内那份。
    """
    if configured.is_dir():
        return configured

    next_to_exe = exe_dir() / "models" / "laya-onnx" / "multilingual"
    if next_to_exe.is_dir():
        return next_to_exe

    bundled = bundled_decision_dir()
    if bundled is not None:
        return bundled
    return configured


def bundled_config() -> Path | None:
    """打包内自带的 config.toml（只读模板）。"""
    bundle = _bundle_dir()
    if bundle is None:
        return None
    p = bundle / "config.toml"
    return p if p.is_file() else None


def enable_decision_in_template(text: str) -> str:
    """把 [decision] 段里的 enabled 改成 true，其余一字不动。

    **不自动调用**，只给用户显式要求时用（界面勾选、或命令行开关）。
    当初写它是想在内嵌权重的包里自动开语义层，量过之后撤销了这个想法——
    理由见下，值得记着免得再犯：

    在 21 个候选动作的真实配置下实测 11 条口语 + 14 条非命令：
      真该命中的置信度     0.15 ~ 0.93（"我想聊个天" 只有 0.153）
      不该命中的置信度     0.20 ~ 1.00（"这个多少钱" → web.bilibili **1.000**）
    置信度和正确性**不相关**，所以任何阈值都拦不住后者。默认打开等于默认
    乱执行——那是比"少一个功能"严重得多的缺陷。权重的内嵌解决的是
    "装完不用下载"，不是"默认该开"。

    用 TomlDoc 而不是字符串替换，是为了**保住那一段上面二十多行的注释**：
    那里写着这一层的实测精度，是用户判断要不要开它的唯一依据。
    正则只匹配 `enabled` 这个键名、且只在 [decision] 段内替换，避免碰到
    [model] / [web] / [intent] 各自的 enabled。

    模板结构变了也不会静默出错：TomlDoc 找不到那个键时原样返回。
    """
    from .toml_edit import TomlDoc, TomlEditError

    try:
        doc = TomlDoc(text)
        if not doc.has_section("decision"):
            return text
        if doc.value("decision", "enabled", default=False) is True:
            return text
        if not doc.set("decision", "enabled", True):
            return text
        return doc.text()
    except TomlEditError:
        # 模板坏到解析不了时不改它——宁可让用户手动开，也不要写出一份半坏的配置
        return text


def ensure_user_config() -> Path | None:
    """保证 exe 旁边有一份**可编辑**的 config.toml。

    打包后内置配置在只读的临时解包目录里，用户改不了也留不住。所以首次运行
    复制一份到可写数据目录；之后用户改的就是这一份，升级 exe 不会覆盖它。

    注意这里**刻意不自动打开 [decision]**，即使这个包把 906MB 权重都内嵌了。
    理由见 `enable_decision_in_template()` 的注释：语义层在候选集之外的输入上
    会给出高置信度的错误答案，默认打开等于默认乱执行。权重内嵌解决的是
    "装完不用下载"，不是"默认该开"。
    """
    target = data_dir() / "config.toml"
    if target.is_file():
        return target

    template = bundled_config()
    if template is None:
        return None
    try:
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        return None
    return target


def resolve_config(explicit: str | None) -> Path | None:
    """定位配置文件。

    优先级：显式 --config > 可写数据目录（用户改过的那份）> 打包内模板。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    user = data_dir() / "config.toml"
    if user.is_file():
        return user

    if is_frozen():
        return ensure_user_config()

    # 源码运行：优先 cwd（开发时最方便），其次项目根
    for cand in (Path.cwd() / "config.toml", exe_dir() / "config.toml"):
        if cand.is_file():
            return cand
    return None


def default_model_dir() -> Path:
    """识别模型的默认位置。

    刻意**不**指向 _MEIPASS：那里会被临时解包。指向 exe 同级，这样
    「下载一次、之后一直在」，也让用户能直接看到模型在哪。
    """
    return data_dir() / "models" / "sense-voice-int8"


def writable_temp_dir() -> Path:
    """临时目录（音频落盘、导出中间产物时用）。"""
    d = Path(tempfile.gettempdir()) / "voice-ctl"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    """运行日志。放可写数据目录，UI 的「打开日志」按钮指向这里。"""
    return data_dir() / "voice-ctl.log"


def hide_own_console() -> bool:
    """如果当前控制台是**为本进程单独创建**的，就把它藏起来。

    UI 模式下控制台窗口是纯噪音（用户要的是窗口，不是一个黑框跟着）。
    但绝不能无条件藏——从 cmd/PowerShell 里跑 `voice-ctl ui` 时，
    GetConsoleWindow() 返回的是**用户自己的终端**，藏掉等于把人家终端弄没了。

    判据用 GetConsoleProcessList：只有本进程挂在上面，说明这个控制台
    是双击 exe 时 Windows 专门开的，可以放心藏。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return False
        buf = (ctypes.c_uint32 * 8)()
        n = k32.GetConsoleProcessList(buf, 8)
        if n != 1:
            return False
        k32.ShowWindow(hwnd, 0)  # SW_HIDE
        return True
    except Exception:  # noqa: BLE001
        return False


def describe() -> str:
    """给 `doctor` 用的路径诊断信息。"""
    bundle = _bundle_dir()
    return "\n".join(
        [
            f"运行方式  : {'打包 exe' if is_frozen() else '源码/venv'}",
            f"可执行文件: {sys.executable}",
            f"解包目录  : {bundle if bundle else '(不适用)'}",
            f"可写数据  : {data_dir()}",
            f"默认模型  : {default_model_dir()}",
        ]
    )
