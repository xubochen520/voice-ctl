"""诊断应用定位：为什么某些应用找不到。

appfind 有 6 级兜底（PATH → App Paths 注册表 → 已知表 → 开始菜单 → 系统命令），
任何一级写错都会让"打开某某"失效。这个脚本把每一级的实际结果打出来。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice_ctl.appfind import _KNOWN, _expand, _from_app_paths, resolve_app  # noqa: E402

TARGETS = ["微信", "wechat", "Weixin", "计算器", "calc", "记事本", "终端", "任务管理器"]


def main() -> int:
    print("=" * 78)
    print("已安装的疑似目标（进程名含关键词）")
    print("=" * 78)
    import subprocess

    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process | Where-Object { $_.MainWindowTitle } | "
         "Select-Object -First 40 -Property Name,MainWindowTitle | "
         "ForEach-Object { $_.Name + ' :: ' + $_.MainWindowTitle }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    for ln in (r.stdout or "").splitlines():
        if ln.strip():
            print("  " + ln.strip())

    print()
    print("=" * 78)
    print("resolve_app 逐项结果")
    print("=" * 78)
    for t in TARGETS:
        res = resolve_app(t)
        mark = "✓" if res.ok else "✗"
        print(f"  {mark} {t!r:16} -> kind={res.kind:9} value={res.value or '(空)'}")
        print(f"      how: {res.how}")

    print()
    print("=" * 78)
    print("已知表里各应用的搜索路径是否存在")
    print("=" * 78)
    for key, spec in _KNOWN.items():
        names = spec.get("names", [])
        print(f"\n  [{key}] names={names}")
        for exe in spec.get("exe", []):
            w = shutil.which(str(exe))
            reg = _from_app_paths(str(exe))
            flag = "PATH" if w else ("注册表" if reg else "—")
            print(f"    exe {exe:18} {flag}")
        for d in spec.get("dirs", []):
            p = _expand(str(d))
            print(f"    dir {str(d):52} {'存在' if p.is_dir() else '不存在'}")

    print()
    print("=" * 78)
    print("开始菜单里有没有相关快捷方式")
    print("=" * 78)
    for root in (
        _expand(r"{APPDATA}\Microsoft\Windows\Start Menu\Programs"),
        _expand(r"{PROGRAMDATA}\Microsoft\Windows\Start Menu\Programs"),
    ):
        if not root.is_dir():
            print(f"  (不存在) {root}")
            continue
        print(f"  搜索 {root}")
        for lnk in list(root.rglob("*.lnk"))[:400]:
            stem = lnk.stem
            if any(k in stem for k in ("微信", "WeChat", "Weixin", "计算器", "Calc", "记事", "终端")):
                print(f"    {stem}   <- {lnk}")

    print()
    print("=" * 78)
    print("常见微信安装位置探查")
    print("=" * 78)
    cands = [
        r"C:\Program Files\Tencent\WeChat",
        r"C:\Program Files (x86)\Tencent\WeChat",
        r"C:\Program Files\Tencent\Weixin",
        r"C:\Program Files (x86)\Tencent\Weixin",
        r"{LOCALAPPDATA}\Programs\WeChat",
        r"{LOCALAPPDATA}\Tencent",
        r"{PROGRAMFILES}\WindowsApps",
    ]
    for c in cands:
        p = _expand(c)
        if not p.is_dir():
            print(f"  ✗ {c}")
            continue
        print(f"  ✓ {c}")
        try:
            for child in sorted(p.iterdir())[:20]:
                tag = "/" if child.is_dir() else ""
                print(f"      {child.name}{tag}")
        except OSError as e:
            print(f"      (读不了: {e})")

    print()
    print("环境变量：")
    for k in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
        print(f"  {k} = {os.environ.get(k, '(未设置)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
