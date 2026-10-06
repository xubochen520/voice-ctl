"""pytest 配置：让 tests/ 能 import voice_ctl，且不依赖安装。

这里还放两套共享 fixture：
  * `cfg_path` 用于不需要界面的测试
  * `tcl_root` / `app` 给界面测试（真 Tk 窗口）

界面 fixture 放 conftest 而不是留在 test_ui_smoke.py 里，是因为
0.3.5 加的 test_ui_decision_download.py 也要用它们；而 tests/ 不是包，
`from .test_ui_smoke import ...` 那种相对导入在 pytest 下直接 ImportError。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MINIMAL_CONFIG = """
[hotkey]
keys = "<ctrl>+<alt>+space"
min_duration_ms = 200
max_duration_ms = 15000

[audio]
samplerate = 16000
channels = 1
min_peak = 0.01

[model]
dir = "models/sense-voice-int8"
language = "auto"
use_itn = true
num_threads = 2
provider = "cpu"

[match]
threshold = 80

[feedback]
beep = false

[[action]]
id = "open.notepad"
handler = "open_app"
aliases = ["记事本", "notepad"]
describe = "打开记事本"
target = "notepad.exe"

[[action]]
id = "sys.mute"
handler = "sysctl"
aliases = ["静音"]
describe = "系统静音"
target = "mute"

[[action]]
id = "off.example"
handler = "open_app"
aliases = ["停用的"]
target = "calc.exe"
enabled = false
"""


@pytest.fixture()
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def tcl_root():
    """整个模块共用一个 Tcl 解释器。

    每个测试都 Tk()/destroy() 的话，Tcl 会反复 Init/Finalize，跑几次之后
    连自己的 init.tcl 都找不到（"Can't find a usable init.tcl"）——在全量
    测试里表现为随机一个 UI 测试报错，单独跑那个文件却是绿的。
    """
    from voice_ctl.ui import theme

    try:
        # DPI 感知必须在 Tk() 之前声明，否则 winfo_fpixels 拿到的是被虚拟化的值
        theme.enable_dpi_awareness()
        root = tk.Tk()
    except tk.TclError as e:  # pragma: no cover - 无图形环境
        # 真实的 TclError 文本必须打出来：只说"没有图形环境"会把
        # "Tcl 解释器被前面的测试搞坏了"这种问题伪装成环境缺失，
        # 表现为"单跑通过、全量跑跳过"，很难查。
        print(f"\n[conftest] Tk 初始化失败，界面测试将跳过：{type(e).__name__}: {e}",
              file=sys.stderr)
        pytest.skip(f"没有可用的图形环境：{e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:  # pragma: no cover
        pass


@pytest.fixture()
def app(cfg_path: Path, tcl_root, monkeypatch: pytest.MonkeyPatch):
    from voice_ctl import decision, events
    from voice_ctl.config import load_config
    from voice_ctl.ui.window import build_window

    # 界面测试不该真的 import torch。语义层可用性检查在后台线程里跑（见 ui/workers.py），
    # 而测试里主线程只是 update() 轮询、不在 mainloop 里：_tkinter 对"非主线程调 Tcl"会先干等
    # 主线程进入主循环（最多 1 秒）再报错——后台线程 import torch 期间每回收一个 tkinter 对象
    # 就白等 1 秒，一次导入拖到半分钟以上。要测真实行为的用例自己再覆盖这个桩。
    monkeypatch.setattr(decision, "available", lambda root: (False, "测试环境：不检查语义层"))

    win = tk.Toplevel(tcl_root)
    win.withdraw()
    a = build_window(load_config(cfg_path), cfg_path, root=win)
    yield a
    try:
        a.on_close()
    except tk.TclError:  # pragma: no cover
        pass
    events.get_bus().close(0.2)
