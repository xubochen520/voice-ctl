"""动作注册表 + 全部 handler。

加一个新能力的完整流程：
    1. 如果只是"启动某个程序/打开某个网址/跑某个系统操作"，在 config.toml 里
       加一段 [[action]] 就够了，**不用改代码**。
    2. 如果需要全新行为，写一个 Action 子类，在 HANDLERS 里注册。

动作执行一律返回 ActionResult，不抛异常给主循环——语音助手不该因为一个
动作失败就整体崩掉。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..appfind import resolve_app

# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #


@dataclass
class ActionResult:
    ok: bool
    message: str
    detail: str = ""

    def describe(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"{mark} {self.message}" + (f"  [{self.detail}]" if self.detail else "")


@dataclass
class ActionContext:
    """执行上下文。动作可以读它，但不应该改它。"""

    text: str = ""
    """识别出的原始文本。"""

    normalized: str = ""
    """归一化后的文本。"""

    matched_alias: str = ""
    """命中动作的那条别名。"""

    dry_run: bool = False
    """True 时只报告将要做什么，不真的执行。"""

    extra: dict[str, Any] = field(default_factory=dict)


class Action:
    """一个可被语音触发的动作。"""

    handler_name = ""

    def __init__(self, cfg) -> None:  # noqa: ANN001 - ActionConfig，避免循环导入
        self.cfg = cfg

    @property
    def id(self) -> str:
        return self.cfg.id

    def execute(self, ctx: ActionContext) -> ActionResult:  # pragma: no cover - 抽象
        raise NotImplementedError

    # 供 `voice-ctl doctor` 做静态检查：这个动作现在能不能跑通
    def preflight(self) -> ActionResult:
        return ActionResult(True, "无需预检")


def _popen(args: list[str], *, cwd: str | None = None) -> subprocess.Popen:
    """启动一个脱离本进程的进程。

    Windows 上不加 CREATE_NO_WINDOW 会闪一个黑框；
    close_fds + 不捕获输出，避免子进程跟着我们退出。
    """
    kwargs: dict[str, Any] = {"cwd": cwd, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    return subprocess.Popen(args, **kwargs)  # noqa: S603


# --------------------------------------------------------------------------- #
# handler: open_app
# --------------------------------------------------------------------------- #


class OpenAppAction(Action):
    handler_name = "open_app"

    def _resolve(self):
        return resolve_app(self.cfg.target, self.cfg.aliases, self.cfg.describe)

    def preflight(self) -> ActionResult:
        r = self._resolve()
        what = self.cfg.target or (self.cfg.aliases[:1] or ["(无名)"])[0]
        if r.ok:
            return ActionResult(True, f"找到 {what}", f"{r.how}: {r.value}")
        return ActionResult(False, f"找不到 {what}", r.how)

    def execute(self, ctx: ActionContext) -> ActionResult:
        r = self._resolve()
        if not r.ok:
            return ActionResult(
                False,
                f"找不到应用：{self.cfg.target or '、'.join(self.cfg.aliases)}",
                r.how,
            )
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将启动 {r.label or r.value}", r.how)

        args = list(self.cfg.args)
        try:
            if r.kind == "exe":
                _popen([r.value, *args])
            elif r.kind == "shortcut":
                os.startfile(r.value)  # noqa: S606
            elif r.kind == "aumid":
                # UWP / 商店应用没有可直接启动的 exe，必须走 shell:AppsFolder
                _popen(["explorer.exe", f"shell:AppsFolder\\{r.value}"])
            else:  # shell
                _popen(["cmd", "/c", "start", "", r.value, *args])
        except OSError as e:
            return ActionResult(False, f"启动失败：{e}", r.how)
        return ActionResult(True, f"已启动 {r.label or r.value}", r.how)


# --------------------------------------------------------------------------- #
# handler: open_path
# --------------------------------------------------------------------------- #


class OpenPathAction(Action):
    handler_name = "open_path"

    def _target(self) -> Path:
        p = Path(os.path.expandvars(self.cfg.target)).expanduser()
        return p

    def preflight(self) -> ActionResult:
        p = self._target()
        if p.exists():
            return ActionResult(True, f"路径存在：{p}")
        return ActionResult(False, f"路径不存在：{p}")

    def execute(self, ctx: ActionContext) -> ActionResult:
        p = self._target()
        if not p.exists():
            return ActionResult(False, f"路径不存在：{p}")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将打开 {p}")
        try:
            os.startfile(str(p))  # noqa: S606
        except OSError as e:
            return ActionResult(False, f"打开失败：{e}")
        return ActionResult(True, f"已打开 {p}")


# --------------------------------------------------------------------------- #
# handler: open_url
# --------------------------------------------------------------------------- #


class OpenUrlAction(Action):
    handler_name = "open_url"

    def preflight(self) -> ActionResult:
        t = self.cfg.target.strip()
        if not t:
            return ActionResult(False, "target 为空")
        if "://" not in t and not t.endswith(":"):
            return ActionResult(
                True, f"{t} 看起来像域名，浏览器会补协议", "建议写成 https://..."
            )
        return ActionResult(True, f"打开 {t}")

    def execute(self, ctx: ActionContext) -> ActionResult:
        t = self.cfg.target.strip()
        if not t:
            return ActionResult(False, "target 为空")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将打开 {t}")
        try:
            # webbrowser 会走系统默认浏览器；协议式 URI（ms-settings: 等）也能开
            ok = webbrowser.open(t, new=2)
        except Exception as e:  # noqa: BLE001
            return ActionResult(False, f"打开失败：{e}")
        if not ok:
            # 退一步用 start，能处理更多协议式 URI
            try:
                _popen(["cmd", "/c", "start", "", t])
                return ActionResult(True, f"已打开 {t}", "webbrowser 返回失败，改用 start")
            except OSError as e:
                return ActionResult(False, f"打开失败：{e}")
        return ActionResult(True, f"已打开 {t}")


# --------------------------------------------------------------------------- #
# handler: sysctl
# --------------------------------------------------------------------------- #


class SysctlAction(Action):
    handler_name = "sysctl"

    _OPS = {
        "volume_up",
        "volume_down",
        "mute",
        "lock",
        "screenshot",
        "show_desktop",
        "sleep",
        "explorer",
    }

    def preflight(self) -> ActionResult:
        op = self.cfg.target.strip().lower()
        if op not in self._OPS:
            return ActionResult(False, f"不认识的系统操作 {op!r}", f"支持：{', '.join(sorted(self._OPS))}")
        if op in ("sleep",) and sys.platform != "win32":
            return ActionResult(False, "sleep 只在 Windows 上支持")
        return ActionResult(True, f"系统操作 {op}")

    # -- 各操作 ----------------------------------------------------------- #

    @staticmethod
    def _volume(direction: str) -> None:
        """用 Windows 多媒体键调音量——比碰 CoreAudio 稳，且对蓝牙设备也有效。"""
        from pynput.keyboard import Controller, Key

        kb = Controller()
        key = Key.media_volume_up if direction == "up" else Key.media_volume_down
        for _ in range(2):  # Windows 每档 2%
            kb.press(key)
            kb.release(key)
            time.sleep(0.02)

    @staticmethod
    def _mute() -> None:
        from pynput.keyboard import Controller, Key

        kb = Controller()
        kb.press(Key.media_volume_mute)
        kb.release(Key.media_volume_mute)

    @staticmethod
    def _lock() -> None:
        import ctypes

        ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]

    @staticmethod
    def _screenshot() -> subprocess.Popen:
        """用系统截图工具。Win11 上是 Win+Shift+S 的等价物。"""
        return _popen(["explorer.exe", "ms-screenclip:"])

    @staticmethod
    def _show_desktop() -> None:
        from pynput.keyboard import Controller, Key

        kb = Controller()
        kb.press(Key.cmd)
        kb.press("d")
        kb.release("d")
        kb.release(Key.cmd)

    @staticmethod
    def _sleep() -> None:
        subprocess.run(  # noqa: S603
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def execute(self, ctx: ActionContext) -> ActionResult:
        op = self.cfg.target.strip().lower()
        if op not in self._OPS:
            return ActionResult(False, f"不认识的系统操作 {op!r}")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将执行 {op}")

        labels = {
            "volume_up": "已调高音量",
            "volume_down": "已调低音量",
            "mute": "已切换静音",
            "lock": "已锁定屏幕",
            "screenshot": "已唤起截图",
            "show_desktop": "已显示桌面",
            "sleep": "正在睡眠",
            "explorer": "已打开资源管理器",
        }
        try:
            if op == "volume_up":
                self._volume("up")
            elif op == "volume_down":
                self._volume("down")
            elif op == "mute":
                self._mute()
            elif op == "lock":
                self._lock()
            elif op == "screenshot":
                self._screenshot()
            elif op == "show_desktop":
                self._show_desktop()
            elif op == "sleep":
                self._sleep()
            elif op == "explorer":
                _popen(["explorer.exe"])
        except Exception as e:  # noqa: BLE001
            return ActionResult(False, f"{op} 执行失败：{e}")
        return ActionResult(True, labels.get(op, op))


# --------------------------------------------------------------------------- #
# handler: keys
# --------------------------------------------------------------------------- #


class KeysAction(Action):
    handler_name = "keys"

    _ALIASES = {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "win": "cmd",
        "cmd": "cmd",
        "super": "cmd",
        "enter": "enter",
        "return": "enter",
        "esc": "esc",
        "escape": "esc",
        "tab": "tab",
        "space": "space",
        "del": "delete",
        "delete": "delete",
        "backspace": "backspace",
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "home": "home",
        "end": "end",
        "pageup": "page_up",
        "pagedown": "page_down",
    }

    def _keys(self) -> list[str]:
        """键序列来源：args 优先，其次按 '+' 拆 target。

        两个来源都要支持——用户可能写 target = "ctrl+alt+w"（直观），
        也可能写 args = ["ctrl", "alt", "w"]（含 '+' 的键名时更明确）。
        """
        if self.cfg.args:
            return [str(k).strip().lower() for k in self.cfg.args if str(k).strip()]
        return [k.strip().lower() for k in self.cfg.target.split("+") if k.strip()]

    def preflight(self) -> ActionResult:
        keys = self._keys()
        if not keys:
            return ActionResult(False, "没有按键序列", "target 写成 ctrl+alt+w，或用 args = [...]")
        for k in keys:
            if len(k) == 1:
                continue  # 单字符键一律接受
            if k in self._ALIASES:
                continue
            if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
                continue  # 功能键 F1-F24
            return ActionResult(
                False,
                f"不认识的键名 {k!r}",
                f"可用：单字符、F1-F24，或 {', '.join(sorted(self._ALIASES))}",
            )
        return ActionResult(True, f"按键 {'+'.join(keys)}")

    def execute(self, ctx: ActionContext) -> ActionResult:
        keys = self._keys()
        if not keys:
            return ActionResult(False, "没有按键序列")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将按 {'+'.join(keys)}")
        try:
            from pynput.keyboard import Controller, Key

            kb = Controller()
            resolved = []
            for k in keys:
                name = self._ALIASES.get(k, k)
                resolved.append(getattr(Key, name) if hasattr(Key, name) else name)

            for k in resolved:
                kb.press(k)
            for k in reversed(resolved):  # 逆序松开，符合真人按键习惯
                kb.release(k)
        except Exception as e:  # noqa: BLE001
            return ActionResult(False, f"发送按键失败：{e}")
        return ActionResult(True, f"已发送 {'+'.join(keys)}")


# --------------------------------------------------------------------------- #
# handler: shell
# --------------------------------------------------------------------------- #


class ShellAction(Action):
    handler_name = "shell"

    def preflight(self) -> ActionResult:
        if not self.cfg.target.strip():
            return ActionResult(False, "target 为空")
        return ActionResult(True, f"将执行：{self.cfg.target}", "shell 有风险，确认这是你要的命令")

    def execute(self, ctx: ActionContext) -> ActionResult:
        cmd = self.cfg.target.strip()
        if not cmd:
            return ActionResult(False, "target 为空")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将执行 {cmd}")
        try:
            args = [cmd, *self.cfg.args] if self.cfg.args else cmd
            if isinstance(args, str):
                p = _popen(["cmd", "/c", args])
            else:
                p = _popen(args)
        except OSError as e:
            return ActionResult(False, f"执行失败：{e}")
        return ActionResult(True, f"已启动命令（pid {p.pid}）")


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

HANDLERS: dict[str, type[Action]] = {
    "open_app": OpenAppAction,
    "open_path": OpenPathAction,
    "open_url": OpenUrlAction,
    "sysctl": SysctlAction,
    "keys": KeysAction,
    "shell": ShellAction,
}


class Registry:
    """按 id 索引的动作集合。"""

    def __init__(self) -> None:
        self._by_id: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.id in self._by_id:
            raise ValueError(f"动作 id 重复：{action.id}")
        self._by_id[action.id] = action

    def get(self, action_id: str) -> Action | None:
        return self._by_id.get(action_id)

    @property
    def ids(self) -> list[str]:
        return list(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, action_id: object) -> bool:
        return action_id in self._by_id


def build_registry(action_configs) -> Registry:  # noqa: ANN001
    """从配置构建注册表。handler 不认识时明确报错，而不是静默跳过。"""
    reg = Registry()
    for cfg in action_configs:
        cls = HANDLERS.get(cfg.handler)
        if cls is None:
            raise ValueError(
                f"动作 {cfg.id!r} 的 handler={cfg.handler!r} 没有实现；"
                f"已实现：{', '.join(sorted(HANDLERS))}"
            )
        reg.register(cls(cfg))
    return reg
