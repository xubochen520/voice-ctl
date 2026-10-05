"""单实例保护。

常驻的热键工具没有这个会出事：双击两次 exe（或者 `run` 和 `ui` 各开了一个），
就有**两个**低级键盘钩子，同一句话被两个引擎各执行一次——音量调两档、日程建两条。

用 Windows 命名互斥量判定"是不是已经有一个在跑"，再用一个命名事件把
"请把你的窗口显示出来"传给那个已经在跑的实例（它在界面轮询里查这个事件）。

名字放在 `Local\\` 命名空间：每个登录会话各一份，多用户同机互不干扰。
"""

from __future__ import annotations

import sys
from typing import Any

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0


class SingleInstance:
    def __init__(self, name: str = "voice-ctl") -> None:
        self.name = name
        self.already_running = False
        self._mutex: Any = None
        self._event: Any = None
        self._k32: Any = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        k32.SetEvent.argtypes = (wintypes.HANDLE,)
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32

        ctypes.set_last_error(0)
        self._mutex = k32.CreateMutexW(None, False, f"Local\\{name}.lock")
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        # 自动复位事件：poll_signal() 读到一次就清掉，不会反复触发
        self._event = k32.CreateEventW(None, False, False, f"Local\\{name}.show")

    def signal_existing(self) -> bool:
        """通知已经在跑的那个实例「把窗口显示出来」。"""
        if self._k32 is None or not self._event:
            return False
        return bool(self._k32.SetEvent(self._event))

    def poll_signal(self) -> bool:
        """有人要我显示窗口吗？非阻塞，读一次清一次。"""
        if self._k32 is None or not self._event:
            return False
        return self._k32.WaitForSingleObject(self._event, 0) == WAIT_OBJECT_0

    def close(self) -> None:
        k32, self._k32 = self._k32, None
        if k32 is None:
            return
        for h in (self._event, self._mutex):
            if h:
                try:
                    k32.CloseHandle(h)
                except Exception:  # noqa: BLE001
                    pass
        self._event = self._mutex = None


def acquire_or_signal(name: str = "voice-ctl") -> SingleInstance | None:
    """我是第一个就返回实例（调用方持有到退出）；已有实例在跑就通知它并返回 None。"""
    inst = SingleInstance(name)
    if inst.already_running:
        inst.signal_existing()
        inst.close()
        return None
    return inst


__all__ = ["SingleInstance", "acquire_or_signal"]
