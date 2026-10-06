# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

两个变体由环境变量控制：

    VOICE_CTL_BUNDLE_MODEL=1    把 226MB 识别模型打进 exe（完整版，免下载）
    VOICE_CTL_BUNDLE_LLAMA=1    把 39.8MB llama.cpp 运行时打进 exe（内置小模型层的二进制）
    VOICE_CTL_BUNDLE_LLM_MODEL=1 连 GGUF 模型也打进去（469MB，慎用，见下）
    VOICE_CTL_BUNDLE_DECISION=1 把 Laya 语义层打进去（含 torch/transformers 约 600MB）
    VOICE_CTL_BUNDLE_DECISION_WEIGHTS=1  连 873MB ONNX 权重也打进去（0.3.4 新增）
    VOICE_CTL_DECISION_WEIGHTS=<路径>     权重的来源目录，默认 models/laya-onnx/multilingual
    默认                        都不打（精简版，首次按需下载）

关于 llama.cpp 的三个变体（0.3.1 新增）：

  1. **运行时（39.8MB）值得打进去**。它是"内置小模型层"能工作的前提，而且
     从 GitHub 下载要用户能访问 github.com。实测代价很小：exe 从 58.1MB 涨到
     84.0MB，但**启动时间几乎没变**——内嵌的是 22 个 DLL，只有真的开小模型层
     才会被加载，不开就只是磁盘上的几行目录项（见 docs/BUILDING-EXE.md 的实测表）。
  2. **GGUF 模型（469MB~1GB）不建议打**，但留了开关。原因是 PyInstaller 的
     单文件 exe **每次启动都要把内嵌数据解包到临时目录**——0.2.0 实测过：
     226MB 的识别模型让启动从 1.7s 变成 3.1s。469MB 会更糟，而且每次启动都
     白写 469MB 到磁盘。让用户下一次比每次启动都解包划算得多。
  3. 模型自动落到可写数据目录，和识别模型同一套机制（见 bootstrap）。

必须显式处理的四个原生依赖（PyInstaller 自动分析抓不全）：
  1. `sherpa_onnx/lib/` —— 里面有 onnxruntime.dll、C API DLL 和 _sherpa_onnx.pyd。
     少了它 exe 一启动就 ImportError，而且报错信息很难指向真因。
  2. `_sounddevice_data/` —— PortAudio 的 DLL 放在这个独立顶层包里，
     不在 sounddevice 包内，靠 import 分析发现不了。
  3. 识别模型与 config.toml —— 纯数据，必须 collect_data_files 或手动加。
  4. llama.cpp 的 22 个文件同理，而且**一个都不能少**：实测少一个 `mtmd.dll`
     就是 0xC0000135（DLL 找不到），报错完全不指向真因。

必须排除的：torch / transformers（约 600MB），默认路径用不到；
只有开启 Laya 语义层才需要，那是可选项，不该让所有人买单。

**但"只是可选"这个说法要更正**（0.3.2 实测）：`VOICE_CTL_BUNDLE_DECISION=1`
可以把它打进去，而且**语义层其实离不开 torch**——`laya.onnx_agent` 顶层
import `laya.common`，而 `laya.common` 第 13 行就是 `import torch`，单文件里
40 多处直接用 `torch.nn` / `torch.softmax`。所以"用 ONNX 权重就不需要 torch"
是错的：不带 torch 的 exe 一开语义层就是 `No module named 'torch'`。
两者差 600MB，所以默认仍是排除，但要打就得打全套。
"""

import os
import sys as _sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

BUNDLE_MODEL = os.environ.get("VOICE_CTL_BUNDLE_MODEL", "0") == "1"
BUNDLE_LLAMA = os.environ.get("VOICE_CTL_BUNDLE_LLAMA", "0") == "1"
BUNDLE_LLM_MODEL = os.environ.get("VOICE_CTL_BUNDLE_LLM_MODEL", "0") == "1"
BUNDLE_DECISION = os.environ.get("VOICE_CTL_BUNDLE_DECISION", "0") == "1"
# 语义层的 873MB ONNX 权重单独一个开关（0.3.4 新增）：torch 那 500MB 和权重
# 这 873MB 是两件事，有时只想要其中一件（比如想验证"权重打进去了没有"而
# 不想再等 torch 那一份）。BUNDLE_DECISION_WEIGHTS=1 但 BUNDLE_DECISION=0
# 是允许的——那会打一份有权重却没 torch 的包，语义层加载会失败，
# 但 doctor 能看出权重在哪，适合排查路径问题。
BUNDLE_DECISION_WEIGHTS = os.environ.get("VOICE_CTL_BUNDLE_DECISION_WEIGHTS", "0") == "1"
DECISION_WEIGHTS_SRC = os.environ.get(
    "VOICE_CTL_DECISION_WEIGHTS", "models/laya-onnx/multilingual"
)
ROOT = os.path.abspath(os.getcwd())

datas = [
    (os.path.join(ROOT, "config.toml"), "."),
    # 仓库里的 README 一起带上，用户在 exe 旁边能看到用法
    (os.path.join(ROOT, "README.md"), "."),
]

if BUNDLE_MODEL:
    model_dir = os.path.join(ROOT, "models", "sense-voice-int8")
    if os.path.isdir(model_dir):
        datas.append((model_dir, "models/sense-voice-int8"))

# --- 语义层权重（873MB） ----------------------------------------------------- #
#
# 和 ASR 模型同一套机制：打进包 -> 启动时落在 `_MEIPASS` -> 运行时由
# `bootstrap.resolve_decision_dir()` 兜底找到。**这个兜底是不可省的**——
# 配置里的 `[decision].onnx_dir` 是相对路径，按配置文件所在目录解析，
# 而打包后那份 config.toml 在 exe 同级，那里没有权重。早先版本没有这个
# 兜底函数，所以"把权重打进包"会毫无效果（程序照样报"缺少 *.onnx"）。
#
# 体积上这是 unfriendly 的一档：权重 873MB + torch 约 500MB。所以必须出
# **目录版**（onedir），单文件版每次启动都要把这 873MB 解到临时目录。
if BUNDLE_DECISION_WEIGHTS:
    _wsrc = os.path.join(ROOT, DECISION_WEIGHTS_SRC.replace("/", os.sep))
    if os.path.isdir(_wsrc):
        _graph = [f for f in os.listdir(_wsrc) if f.endswith(".onnx")]
        if _graph:
            _mb = sum(
                os.path.getsize(os.path.join(_dp, _fn))
                for _dp, _dn, _fns in os.walk(_wsrc)
                for _fn in _fns
            ) / 1024 / 1024
            # 目标路径必须和 resolve_decision_dir() 里找的那两条一致
            datas.append((_wsrc, "models/laya-onnx/multilingual"))
            print(
                f"[spec] 内嵌语义层权重：{_wsrc} -> models/laya-onnx/multilingual"
                f"（{_mb:.0f}MB，{_graph[0]}）"
            )
        else:
            print(f"[spec] 警告：{_wsrc} 里没有 .onnx 图，权重不完整，跳过")
    else:
        print(
            f"[spec] 警告：要内嵌语义层权重但目录不存在：{_wsrc}\n"
            "        先跑 `voice-ctl fetch-decision`，或用 VOICE_CTL_DECISION_WEIGHTS 指到别处。"
        )

# --- llama.cpp 运行时 ------------------------------------------------------- #
#
# 放在 `llama-runtime/`（不是 `llm/`）是为了不和可写数据目录里那份混起来：
# 数据目录里的是用户自己下的、可能版本不同；这里的是随 exe 冻结的。
# llamacpp.py 会先找数据目录，找不到再回落到这里。
LLAMA_DEST = "llama-runtime"
if BUNDLE_LLAMA or BUNDLE_LLM_MODEL:
    import sys as _sys

    _sys.path.insert(0, ROOT)
    try:
        from voice_ctl import llamacpp as _lc

        _rt = _lc.default_runtime_dir()
        if _lc.server_ready(_rt):
            datas.append((str(_rt), LLAMA_DEST))
            print(f"[spec] 内嵌 llama.cpp 运行时：{_rt}")
        else:
            print(
                "[spec] 警告：要内嵌 llama.cpp 但本机没装。"
                "先跑 `voice-ctl llm --install`，或去掉 VOICE_CTL_BUNDLE_LLAMA。"
            )
        if BUNDLE_LLM_MODEL:
            _m = _lc.find_model()
            if _m is not None:
                datas.append((str(_m), "llm-models"))
                print(f"[spec] 内嵌 GGUF 模型：{_m}（注意：每次启动都会解包它）")
            else:
                print("[spec] 警告：要内嵌模型但没有 .gguf。先跑 `voice-ctl llm --download`。")
    except Exception as _e:  # noqa: BLE001
        print(f"[spec] 警告：收集 llama.cpp 运行时失败：{_e}")

binaries = []

# 1. sherpa-onnx 的原生 DLL 与扩展模块
try:
    binaries += collect_dynamic_libs("sherpa_onnx")
except Exception as e:  # noqa: BLE001
    print(f"[spec] 警告：sherpa_onnx 动态库收集失败：{e}")

# 2. PortAudio（sounddevice 依赖，但不在它自己的包里）
try:
    import _sounddevice_data  # noqa: F401

    datas += collect_data_files("_sounddevice_data", include_py_files=False)
except Exception as e:  # noqa: BLE001
    print(f"[spec] 警告：_sounddevice_data 收集失败：{e}")

# 3. onnxruntime 的原生部分（Laya 语义层用；默认关闭也要带上，
#    否则用户一开语义层就报缺 DLL，且不好定位）
try:
    binaries += collect_dynamic_libs("onnxruntime")
except Exception as e:  # noqa: BLE001
    print(f"[spec] 提示：onnxruntime 未安装或不完整（语义层将不可用）：{e}")

# 4. 语义层（可选，VOICE_CTL_BUNDLE_DECISION=1）的原生部分
#
# torch 的 DLL 全在 torch/lib/ 下（实测 9 个共 314MB：c10.dll、torch_cpu.dll、
# libiomp5md.dll …），PyInstaller 自带的 hook 会把它们放到 torch/lib/，所以
# 这里不用手动搬；但**必须显式收集**，否则缺 c10.dll 时是启动即崩、报错还
# 不指向真因。transformers 则是纯 Python + 一堆 json，靠 hook 即可。
# （hiddenimports 那一长串在下面，因为列表还没定义，这里 append 不了。）
if BUNDLE_DECISION:
    for _pkg in ("torch", "onnxruntime"):
        try:
            binaries += collect_dynamic_libs(_pkg)
        except Exception as _e:  # noqa: BLE001
            print(f"[spec] 警告：收集 {_pkg} 动态库失败：{_e}")

# torch 的动态库会被上面两处各收一次（onnxruntime 的也是），重复项会让
# Analysis 报 "duplicate entries"。按 (dest 路径) 去重，保留先出现的那个。
_seen_bin: set = set()
_dedup_binaries = []
for _b in binaries:
    _key = _b[0] if isinstance(_b, (tuple, list)) else _b
    if _key in _seen_bin:
        continue
    _seen_bin.add(_key)
    _dedup_binaries.append(_b)
if len(_dedup_binaries) != len(binaries):
    print(f"[spec] 动态库去重：{len(binaries)} -> {len(_dedup_binaries)}")
binaries = _dedup_binaries

hiddenimports = [
    "voice_ctl",
    "voice_ctl.actions",
    "voice_ctl.bootstrap",
    "voice_ctl.events",
    "voice_ctl.fetch",
    "voice_ctl.confedit",
    "voice_ctl.toml_edit",
    "voice_ctl.runner",
    "voice_ctl.ui",
    "voice_ctl.ui.window",
    "voice_ctl.ui.theme",
    "voice_ctl.ui.gfx",
    "voice_ctl.ui.textfit",
    "voice_ctl.ui.kit",
    "voice_ctl.ui.inputs",
    "voice_ctl.ui.widgets",
    "voice_ctl.ui.tab_run",
    "voice_ctl.ui.tab_logs",
    "voice_ctl.ui.tab_hotkey",
    "voice_ctl.ui.tab_actions",
    "voice_ctl.ui.tab_settings",
    "voice_ctl.ui.tab_about",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "sounddevice",
    "_sounddevice_data",
    # 关闭应用要用它列进程（16ms vs WMI 的 677ms）。它是 C 扩展，
    # 静态分析抓得到模块本身，但漏了会变成"运行时 ImportError → 静默退回 WMI"，
    # 表现只是变慢，很难发现，所以显式写上。
    "psutil",
    # 图形界面：tkinter 的 tcl/tk 数据目录由 PyInstaller 自带的 hook 收集，
    # 但它只认得到"被 import 过"的模块。界面是延迟 import 的（cli 里才 import
    # voice_ctl.ui），静态分析扫不到，必须显式声明。
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.font",
    "tkinter.ttk",
    "tkinter.constants",
]

# 可选依赖：装了才带上，没装不影响
for mod in ("pypinyin", "onnxruntime", "laya.onnx_agent"):
    try:
        __import__(mod)
        hiddenimports.append(mod)
    except Exception:  # noqa: BLE001
        pass

# 语义层打开时，把 laya / transformers / torch 的模块与数据全带上。
# 列这么细是因为它们大量使用 try/except 和延迟 import，静态分析抓不全；
# 漏一个的表现是"运行时才炸"，而且用户看不出是打包的问题。
if BUNDLE_DECISION:
    hiddenimports += [
        "laya",
        "laya.onnx_agent",
        "laya.common",
        "laya.confidence",
        "laya.hooks",
        "laya.revisions",
        "laya.structured",
        "laya.router",
        "laya.lang",
        "laya.presets",
        "laya.email",
        "transformers",
        "transformers.models.auto",
        "transformers.models.auto.tokenization_auto",
        "transformers.models.modernbert",
        "tokenizers",
        "safetensors",
        "huggingface_hub",
        "torch",
        "torch.nn",
        "torch.nn.functional",
        "torch.utils.checkpoint",
    ]
    try:
        datas += collect_data_files("transformers", include_py_files=False)
    except Exception as _e:  # noqa: BLE001
        print(f"[spec] 警告：收集 transformers 数据文件失败：{_e}")
    print("[spec] 语义层（Laya + torch）打进 exe：预计 +600MB，启动也会变慢")

excludes = [
    # 默认路径完全用不到，却占约 600MB。要语义层就别排除（见 BUNDLE_DECISION）。
    *([] if BUNDLE_DECISION else ["torch", "torchvision", "torchaudio", "transformers"]),
    "tensorflow", "jax", "jaxlib", "flax",
    # 语义层的可选依赖，用户自己装。
    # ⚠ `laya.calibrate` 不在这张名单里（0.3.2 修正）：`laya/onnx_agent.py`
    #   第 220 行和 652 行都在函数体里 `from .calibrate import ...`，静态分析
    #   看不到，排除掉的表现是 exe 启动 23 秒之后报
    #   "加载 ONNX 失败：ModuleNotFoundError: No module named 'laya.calibrate'"
    #   ——报错指向权重，真因在打包配置里。要开语义层就别排除 laya 的任何子模块。
    *([] if BUNDLE_DECISION else [
        "laya.mcp", "laya.serve", "laya.integrations", "laya.train",
        "laya.evals", "laya.calibrate", "laya.shortlist",
    ]),
    # 科学计算与绘图的大件
    "matplotlib", "scipy", "pandas", "IPython", "notebook",
    # 图形界面用的是标准库自带的 tkinter（见下），这些第三方 GUI 一律不要。
    # 注意：tkinter 本身**不能**排除——0.2.0 起界面是主要入口。
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "tkinter.test", "tkinter.tix", "tkinter.dnd",
    # 测试与开发工具
    "pytest", "setuptools", "pip", "wheel",
]

a = Analysis(
    [os.path.join(ROOT, "exe_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# 语义层那 600MB 每次启动都要解到临时目录，单文件根本不适合装它：实测
# `--version` 从 4.3s 变成 20.8s。所以这个变体默认出**目录版**（onedir）——
# 文件就在 exe 旁边，启动时不用解包。要单文件还是可以显式 ONE_FILE=1。
#
# 同理，873MB 权重也必须走目录版：单文件版每次启动都要把它解到临时目录，
# 那是 873MB 的写入，启动时间和磁盘磨损都不可接受。
ONE_FILE = os.environ.get("VOICE_CTL_ONE_FILE", "1") == "1"
if (BUNDLE_DECISION or BUNDLE_DECISION_WEIGHTS) and os.environ.get("VOICE_CTL_ONE_FILE") is None:
    ONE_FILE = False

if ONE_FILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="voice-ctl",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX 对 onnxruntime 这类大 DLL 常出问题，且拖慢启动
        runtime_tmpdir=None,
        console=True,       # 必须保留控制台：这是个 CLI 工具，用户要看识别结果
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        # 不改图标：没有 .ico 资源，用默认的比塞一个难看的强
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="voice-ctl",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="voice-ctl",
    )
