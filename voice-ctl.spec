# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

两个变体由环境变量控制：
    VOICE_CTL_BUNDLE_MODEL=1   把 226MB 识别模型打进 exe（完整版，免下载）
    默认                       不打包模型（精简版，首次需跑 `download`）

必须显式处理的三个原生依赖（PyInstaller 自动分析抓不全）：
  1. `sherpa_onnx/lib/` —— 里面有 onnxruntime.dll、C API DLL 和 _sherpa_onnx.pyd。
     少了它 exe 一启动就 ImportError，而且报错信息很难指向真因。
  2. `_sounddevice_data/` —— PortAudio 的 DLL 放在这个独立顶层包里，
     不在 sounddevice 包内，靠 import 分析发现不了。
  3. 识别模型与 config.toml —— 纯数据，必须 collect_data_files 或手动加。

必须排除的：torch / transformers（约 600MB），默认路径用不到；
只有开启 Laya 语义层才需要，那是可选项，不该让所有人买单。
"""

import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

BUNDLE_MODEL = os.environ.get("VOICE_CTL_BUNDLE_MODEL", "0") == "1"
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

excludes = [
    # 默认路径完全用不到，却占约 600MB
    "torch", "torchvision", "torchaudio", "transformers",
    "tensorflow", "jax", "jaxlib", "flax",
    # 语义层的可选依赖，用户自己装
    "laya.mcp", "laya.serve", "laya.integrations", "laya.train",
    "laya.evals", "laya.calibrate", "laya.shortlist",
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
