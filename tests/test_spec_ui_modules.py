"""打包配置的完整性：界面模块一个都不能漏。

为什么专门测这个：界面是**延迟 import** 的（`cli` 里才 `import voice_ctl.ui`），
所以 PyInstaller 的静态分析扫不到它下面的子模块，必须在 spec 的 `hiddenimports`
里显式声明。漏一个的表现是"源码跑得好好的，打包后切到某一页就 ImportError"
——而且报错不指向 spec，很容易误判成代码问题。

现状：漏一个 `workers.py` 不会立刻炸（它是被静态 import 的，自动分析抓得到），
但**这是巧合不是保证**——哪天有人把某个界面模块改成延迟 import，同样的漏法
就会变成真故障。所以判据定成"全部子模块都得列出来"，而不是"列了常用的那几个"。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "voice-ctl.spec"
UI_DIR = ROOT / "voice_ctl" / "ui"


def _declared_ui_modules() -> set[str]:
    """spec 里显式声明的 voice_ctl.ui.* 模块名。"""
    text = SPEC.read_text(encoding="utf-8")
    return set(re.findall(r'"(voice_ctl\.ui(?:\.[\w.]+)?)"', text))


def _actual_ui_modules() -> set[str]:
    """voice_ctl/ui/ 下真实存在的模块（排除 __init__）。"""
    out = set()
    for p in UI_DIR.glob("*.py"):
        if p.stem != "__init__":
            out.add(f"voice_ctl.ui.{p.stem}")
    return out


def test_ui_dir_exists():
    """防止路径写错导致这条守卫静默失效——空集合对空集合也能"通过"。"""
    assert UI_DIR.is_dir(), f"找不到界面目录：{UI_DIR}"
    assert len(_actual_ui_modules()) >= 10, "界面模块数量不对，路径可能变了"


def test_spec_declares_every_ui_module():
    actual = _actual_ui_modules()
    declared = _declared_ui_modules()
    missing = sorted(actual - declared)
    assert not missing, (
        "这些界面模块没写进 voice-ctl.spec 的 hiddenimports，"
        f"打包后会 ImportError：{missing}"
    )


def test_spec_has_no_stale_ui_module():
    """反过来也要查：列了但文件已不在的模块名，是重构留下的死条目。"""
    actual = _actual_ui_modules()
    declared = _declared_ui_modules()
    # voice_ctl.ui 本身是包，不算模块
    stale = sorted(m for m in declared - actual if m != "voice_ctl.ui")
    assert not stale, f"voice-ctl.spec 里这些界面模块已经不存在了：{stale}"
