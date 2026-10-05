"""开机自启。

写 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`——当前用户级，不需要管理员权限，
用户在「设置 → 应用 → 启动」里也能看到、能关掉。比放进「启动」文件夹或建计划任务
都更透明，卸载时也不会留下用户找不到的东西。

启动命令带 `--minimized`：开机自启不该蹦出一个窗口，只在托盘里待着。
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "voice-ctl"


def command() -> str:
    """开机时要执行的命令行。

    打包版直接启动 exe；源码版用 pythonw（没有控制台窗口）跑模块——
    用 python.exe 的话每次开机都会闪一个黑框。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" ui --minimized'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.is_file() else exe
    return f'"{runner}" -m voice_ctl.cli ui --minimized'


def _winreg():  # noqa: ANN202
    if sys.platform != "win32":
        return None
    import winreg

    return winreg


def current(*, key: str = RUN_KEY, name: str = VALUE_NAME) -> str | None:
    """注册表里现在登记的命令；没登记返回 None。"""
    wr = _winreg()
    if wr is None:
        return None
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_READ) as k:
            value, _ = wr.QueryValueEx(k, name)
            return str(value)
    except OSError:
        return None


def is_enabled(*, key: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    return current(key=key, name=name) is not None


def is_stale(*, key: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    """已登记，但登记的命令和现在的不一样（exe 挪了位置 / 换了 Python 环境）。

    这种状态下开机自启实际上是坏的——指向一个不存在的路径，Windows 静默忽略。
    界面据此提示「点一下修复」。
    """
    cur = current(key=key, name=name)
    return cur is not None and cur != command()


def enable(*, key: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    wr = _winreg()
    if wr is None:
        return False
    try:
        with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_SET_VALUE) as k:
            wr.SetValueEx(k, name, 0, wr.REG_SZ, command())
        return True
    except OSError:
        return False


def disable(*, key: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    """取消自启。本来就没登记也算成功。"""
    wr = _winreg()
    if wr is None:
        return False
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_SET_VALUE) as k:
            wr.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


__all__ = ["command", "current", "disable", "enable", "is_enabled", "is_stale"]
