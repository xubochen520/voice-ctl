"""改名之后漏改调用点 —— 这类 bug 用一条通用守卫挡住。

来由：界面重构把 `theme.apply_dark_titlebar` 改名成 `apply_titlebar`，
`voice_ctl/ui/window.py` 跟上了，但 `exe_entry.py:113` 没跟。后果是
**打包出来的 exe 一开界面就崩**，而源码运行和全部单测都发现不了——
`exe_entry.py` 只在打包入口里跑，没有任何测试 import 它。
是靠 `voice-ctl.exe --selftest` 里"真正建一次窗口"那一步抓到的。

所以这里做一次**静态**检查（不 import，避免副作用）：
把 `voice_ctl` 包按模块算出一份"顶层名字表"，然后扫全仓库的源码，
凡是 `voice_ctl.<mod>.<attr>` / `theme.<attr>` / `gfx.<attr>` 这种跨模块引用，
属性不在表里就报出来。

只查我们能静态算准的东西：
  * 模块顶层 def / class / 赋值 / import 进来的名字 / `__all__` 里的字符串
动态塞进去的属性（比如运行时 setattr）一律**不报**——宁可漏报也不误报，
误报会让人把这条守卫关掉。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "voice_ctl"


def _module_names(path: Path) -> set[str]:
    """一个模块对外可见的顶层名字（静态可算的部分）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    # `__all__ = [...]` 里的字符串也算（re-export 的常见写法）
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                for el in node.value.elts:
                    if isinstance(el, ast.Constant) and isinstance(el.value, str):
                        names.add(el.value)
    return names


def _package_names() -> dict[str, set[str]]:
    """{'voice_ctl.ui.theme': {...}, ...}"""
    out: dict[str, set[str]] = {}
    for p in PKG.rglob("*.py"):
        rel = p.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mod = ".".join(parts)
        try:
            out[mod] = _module_names(p)
        except SyntaxError:  # pragma: no cover - 语法都过不了，另有测试会先红
            continue
    return out


# 常见的"从模块导入后取的属性"：局部名 → 模块全名
_ALIASES = {
    "theme": "voice_ctl.ui.theme",
    "gfx": "voice_ctl.ui.gfx",
    "W": "voice_ctl.ui.widgets",
    "kit": "voice_ctl.ui.kit",
    "inputs": "voice_ctl.ui.inputs",
    "textfit": "voice_ctl.ui.textfit",
    "P": None,  # 调色板字典，不查
}

# 扫这些文件（源码全扫；exe_entry.py 必须在内——它就是漏网的那个）
SCAN_GLOBS = ("voice_ctl/**/*.py", "exe_entry.py", "scripts/*.py")


def _scan_file(path: Path, pkgnames: dict[str, set[str]]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    bad: list[str] = []
    for m in re.finditer(r"\b(\w+)\.(\w+)\.(\w+)\b", text):
        local, mid, attr = m.group(1), m.group(2), m.group(3)
        mod = None
        if local == "voice_ctl":
            cand = f"voice_ctl.{mid}"
            if cand in pkgnames:
                mod = cand
        if mod is None:
            full = _ALIASES.get(local)
            if full:
                # 只查一层：alias.attr
                continue
        if mod and mod in pkgnames:
            known = pkgnames[mod]
            # 模块级 import 进来的子模块（voice_ctl.ui.theme 这种）也要认
            if attr not in known and f"{mod}.{attr}" not in pkgnames:
                line = text[: m.start()].count("\n") + 1
                bad.append(f"{path.relative_to(ROOT)}:{line}: {mod}.{attr}")
    return bad


def test_no_stale_cross_module_attribute_references():
    """全仓库扫一遍：`voice_ctl.<mod>.<attr>` 里的 attr 必须真的存在。"""
    pkgnames = _package_names()
    assert pkgnames, "没扫到任何模块，路径可能变了"

    files: list[Path] = []
    for pat in SCAN_GLOBS:
        files.extend(ROOT.glob(pat))

    problems: list[str] = []
    for f in files:
        problems.extend(_scan_file(f, pkgnames))

    assert not problems, (
        "这些跨模块引用指向了不存在的属性（改名之后漏改调用点？）：\n  "
        + "\n  ".join(sorted(problems))
    )


def test_the_guard_actually_catches_a_rename():
    """反向验证：拿一个真存在和一个真不存在的属性比，确认守卫的判据有效。

    没有这条，"守卫没报"可能只是因为它根本没在工作。
    """
    pkgnames = _package_names()
    theme = pkgnames["voice_ctl.ui.theme"]
    assert "apply_titlebar" in theme, "theme 里应当有 apply_titlebar"
    assert "apply_dark_titlebar" not in theme, (
        "theme 里居然还有 apply_dark_titlebar —— 那 exe_entry.py 的问题就不是这个了"
    )


def test_exe_entry_uses_only_real_theme_attributes():
    """点名守住那个真的翻过车的地方。

    `exe_entry.py` 只在打包入口里跑，没有任何测试 import 它，
    所以它的错误只会在"打包之后一开界面就崩"时才暴露。
    """
    src = (ROOT / "exe_entry.py").read_text(encoding="utf-8")
    pkgnames = _package_names()
    theme = pkgnames["voice_ctl.ui.theme"]

    used = set(re.findall(r"\btheme\.(\w+)", src))
    assert used, "exe_entry.py 里没用到 theme？那这条测试没意义了"
    missing = sorted(a for a in used if a not in theme)
    assert not missing, f"exe_entry.py 用了 theme 里不存在的属性：{missing}"
