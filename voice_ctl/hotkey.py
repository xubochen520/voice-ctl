"""全局热键：按住说话（push-to-talk）。

用 pynput（Windows 上是 SetWindowsHookEx 低级键盘钩子）——它能同时拿到
"按下"和"松开"两个事件，这是按住说话的必要条件；Win32 的 RegisterHotKey
只有按下、没有松开，做不了。

配置语法沿用 pynput 的写法：
    "<ctrl>+<alt>+space"   "<ctrl>+<shift>+j"   "<f9>"   "<ctrl>+<alt>+q"

⚠️ 别用纯 "<ctrl>+space"：中文 Windows 上那是输入法切换键，会被系统抢走。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


class HotkeyError(Exception):
    """热键配置或运行错误。"""


@dataclass(frozen=True)
class ParsedHotkey:
    raw: str
    keys: frozenset[str]
    """规范化的键名集合，全部小写。"""

    display: str

    def __str__(self) -> str:
        return self.display


def _canonical(name: str) -> str:
    """把各种写法归一到内部键名。"""
    n = name.strip().lower()
    n = n.strip("<>")
    aliases = {
        "control": "ctrl",
        "ctl": "ctrl",
        "escape": "esc",
        "return": "enter",
        "win": "cmd",
        "windows": "cmd",
        "super": "cmd",
        "meta": "cmd",
        "option": "alt",
        "spacebar": "space",
        "del": "delete",
        "pageup": "page_up",
        "pagedown": "page_down",
        "capslock": "caps_lock",
    }
    if n in aliases:
        return aliases[n]
    # 左右成对的修饰键在 pynput 里是独立的 Key 成员（ctrl_l / ctrl_r / shift_l …），
    # 但用户说的「ctrl」不分左右，必须归一——否则按下左 ctrl 永远凑不齐组合键。
    for side in ("_l", "_r"):
        if n.endswith(side) and n[: -len(side)] in ("ctrl", "shift", "cmd", "alt"):
            return n[: -len(side)]
    return n


def parse_hotkey(spec: str) -> ParsedHotkey:
    """把 "<ctrl>+<alt>+space" 解析成键名集合。"""
    if not spec or not spec.strip():
        raise HotkeyError("热键不能为空")
    parts = [p for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"热键 {spec!r} 解析后没有任何键")
    keys = frozenset(_canonical(p) for p in parts)
    if len(keys) != len(parts):
        raise HotkeyError(f"热键 {spec!r} 里有重复的键：{parts}")
    if not keys:
        raise HotkeyError(f"热键 {spec!r} 无效")
    order = sorted(keys)
    return ParsedHotkey(raw=spec, keys=keys, display="+".join(order))


def key_to_name(key) -> str | None:  # noqa: ANN001 - pynput 的 Key/KeyCode
    """把 pynput 的按键对象转成内部键名。认不出来返回 None。"""
    from pynput import keyboard

    if isinstance(key, keyboard.Key):
        return _canonical(key.name)
    char = getattr(key, "char", None)
    if char:
        # 控制字符（Ctrl+A 之类会给出 '\x01'）不要当成可匹配的字符键
        if len(char) == 1 and ord(char) < 32:
            return None
        return _canonical(char)
    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        return f"vk{vk}"
    return None


class HotkeyListener:
    """监听一个组合键的按下/松开。

    回调 on_press 在**所有**组成键都按下时触发一次；on_release 在任意一个
    组成键松开时触发一次。重复触发被抑制（长按不会连续回调）。
    """

    def __init__(
        self,
        spec: str,
        *,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        suppress: bool = False,
    ) -> None:
        self.hotkey = parse_hotkey(spec)
        self._on_press = on_press
        self._on_release = on_release
        self._suppress = suppress

        self._down: set[str] = set()
        self._active = False
        self._lock = threading.Lock()
        self._listener = None
        self.press_count = 0
        self.release_count = 0

    # -- 事件处理 --------------------------------------------------------- #

    def _handle_press(self, key) -> None:  # noqa: ANN001
        name = key_to_name(key)
        if name is None:
            return
        with self._lock:
            self._down.add(name)
            if self._active or not self.hotkey.keys.issubset(self._down):
                return
            self._active = True
            self.press_count += 1
        self._on_press()

    def _handle_release(self, key) -> None:  # noqa: ANN001
        name = key_to_name(key)
        if name is None:
            return
        with self._lock:
            self._down.discard(name)
            if not self._active:
                return
            # 任一组键松开即结束（同时清掉记录，防止丢事件后卡在按下态）
            if name in self.hotkey.keys:
                self._active = False
                self._down.intersection_update(self.hotkey.keys - {name})
                fired = True
            else:
                fired = False
        if fired:
            self.release_count += 1
            self._on_release()

    def force_release(self) -> None:
        """兜底：按键卡死时手动结束当前按下态。"""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._down.clear()
        self._on_release()

    # -- 生命周期 --------------------------------------------------------- #

    def start(self) -> None:
        from pynput import keyboard

        if self._listener is not None:
            raise HotkeyError("监听器已经启动了")
        try:
            self._listener = keyboard.Listener(
                on_press=self._handle_press,
                on_release=self._handle_release,
                suppress=self._suppress,
            )
            self._listener.start()
        except Exception as e:  # noqa: BLE001
            self._listener = None
            raise HotkeyError(f"启动键盘监听失败：{e}") from e

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                pass

    def wait(self, timeout: float | None = None) -> None:
        listener = self._listener
        if listener is not None:
            listener.join(timeout)

    @property
    def running(self) -> bool:
        return self._listener is not None and getattr(self._listener, "running", False)


class HotkeyTimer:
    """热键 + 最大时长的守护。

    按键卡住（比如被别的程序抢了松开事件）时，max_duration_ms 到点会强行
    调用 on_timeout，避免无限录音。主循环每轮调用 tick()。
    """

    def __init__(self, max_duration_ms: int, on_timeout: Callable[[], None]) -> None:
        self.max_duration_ms = max_duration_ms
        self._on_timeout = on_timeout
        self._deadline = 0.0

    def arm(self) -> None:
        self._deadline = time.perf_counter() + self.max_duration_ms / 1000.0

    def disarm(self) -> None:
        self._deadline = 0.0

    def tick(self) -> bool:
        """返回 True 表示刚触发超时。"""
        if self._deadline and time.perf_counter() >= self._deadline:
            self._deadline = 0.0
            self._on_timeout()
            return True
        return False
