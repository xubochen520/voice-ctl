"""全局热键：按住说话（push-to-talk）。

用 pynput（Windows 上是 SetWindowsHookEx 低级键盘钩子）——它能同时拿到
"按下"和"松开"两个事件，这是按住说话的必要条件；Win32 的 RegisterHotKey
只有按下、没有松开，做不了。

配置语法沿用 pynput 的写法：
    "<ctrl>+<alt>+space"   "<ctrl>+<shift>+j"   "<f9>"   "<ctrl>+<alt>+q"

⚠️ 别用纯 "<ctrl>+space"：中文 Windows 上那是输入法切换键，会被系统抢走。
"""

from __future__ import annotations

import sys
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


def _letter_or_digit_from_vk(vk: object) -> str | None:
    """Windows 虚拟键码里，A-Z 是 0x41-0x5A，0-9 是 0x30-0x39（与键盘布局无关）。

    只在 Windows 上成立：mac 的 vk 是另一套编号。本项目是 Windows 工具，
    但这里仍然显式判平台——拿错了编号的后果是热键悄悄变成别的键。
    """
    if sys.platform != "win32" or not isinstance(vk, int):
        return None
    if 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    return None


def key_to_name(key) -> str | None:  # noqa: ANN001 - pynput 的 Key/KeyCode
    """把 pynput 的按键对象转成内部键名。认不出来返回 None。

    字母和数字**先按虚拟键码认，不看 char**。实测（pynput 1.8.2，Windows）：
    按住 Ctrl 时 `char` 是控制字符（Ctrl+J 给 '\\n'），按住 Ctrl+Alt 时甚至是 None。
    只看 char 的话，`<ctrl>+<alt>+j`、`<ctrl>+<shift>+j` 这类 README 里写着
    "支持"的热键永远凑不齐——默认键用的是 space（特殊键，不走这条路），
    所以一直没人踩到。
    """
    from pynput import keyboard

    if isinstance(key, keyboard.Key):
        return _canonical(key.name)
    by_vk = _letter_or_digit_from_vk(getattr(key, "vk", None))
    if by_vk is not None:
        return by_vk
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


MODIFIERS = frozenset({"ctrl", "alt", "shift", "cmd"})


class HotkeyListener:
    """监听一个组合键的按下/松开。

    回调 on_press 在**所有**组成键都按下时触发一次；on_release 在任意一个
    组成键松开时触发一次。重复触发被抑制（长按不会连续回调）。

    可选的 `on_arm` / `on_disarm` 用来做**预热**：一个多键热键（Ctrl+Alt+空格）
    总是先按修饰键、最后才按主键，手指从 Alt 挪到空格要 100–300ms。把"打开
    麦克风"挪到修饰键按下的那一刻，主键按下时流已经开好了——否则实测要
    160–280ms 才开得了流，开口早一点的人前半个字就没了，而且这 280ms
    还花在键盘钩子线程里，离 Windows 摘钩子的 300ms 红线只差一口气。

    只有**所有修饰键都按下、主键还没按**时才预热；单键热键（<f9>）没有可
    提前的信号，不预热。
    """

    def __init__(
        self,
        spec: str,
        *,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        suppress: bool = False,
        on_arm: Callable[[], None] | None = None,
        on_disarm: Callable[[], None] | None = None,
    ) -> None:
        self.hotkey = parse_hotkey(spec)
        self._on_press = on_press
        self._on_release = on_release
        self._on_arm = on_arm
        self._on_disarm = on_disarm
        self._suppress = suppress
        self._mods = frozenset(k for k in self.hotkey.keys if k in MODIFIERS)

        self._down: set[str] = set()
        self._active = False
        self._armed = False
        self._lock = threading.Lock()
        self._listener = None
        self.press_count = 0
        self.release_count = 0

    # -- 事件处理 --------------------------------------------------------- #

    def _can_arm(self) -> bool:
        return bool(self._on_arm and self._mods and self._mods <= self._down)

    def _handle_press(self, key) -> None:  # noqa: ANN001
        name = key_to_name(key)
        if name is None:
            return
        fire = ""
        with self._lock:
            self._down.add(name)
            if self._active:
                return
            if self.hotkey.keys.issubset(self._down):
                self._active = True
                self.press_count += 1
                fire = "press"
            elif not self._armed and self._can_arm():
                self._armed = True
                fire = "arm"
        if fire == "press":
            self._on_press()
        elif fire == "arm":
            self._safe(self._on_arm)

    def _handle_release(self, key) -> None:  # noqa: ANN001
        name = key_to_name(key)
        if name is None:
            return
        fire = ""
        with self._lock:
            self._down.discard(name)
            if self._active:
                # 任一组键松开即结束（同时清掉记录，防止丢事件后卡在按下态）
                if name not in self.hotkey.keys:
                    return
                self._active = False
                self._down.intersection_update(self.hotkey.keys - {name})
                fire = "release"
                # 录音一结束流就关了；修饰键还按着的话，下一次按主键还要用，重新预热
                self._armed = self._can_arm()
            elif self._armed and not self._can_arm():
                self._armed = False
                fire = "disarm"
        if fire == "release":
            self.release_count += 1
            self._on_release()
            if self._armed:
                self._safe(self._on_arm)
        elif fire == "disarm":
            self._safe(self._on_disarm)

    @staticmethod
    def _safe(fn: Callable[[], None] | None) -> None:
        """预热回调出错不该影响热键本身：预热只是优化，不是功能。"""
        if fn is None:
            return
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    def force_release(self) -> None:
        """兜底：按键卡死时手动结束当前按下态。"""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._armed = False
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
        with self._lock:
            self._armed = False
            self._down.clear()
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
