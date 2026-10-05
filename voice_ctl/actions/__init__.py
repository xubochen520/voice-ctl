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
from typing import Any, Callable

from ..appfind import resolve_app
from ..apps import AppEntry

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
    """识别出的**原始**文本（ASR 原文，没经过归一化）。"""

    normalized: str = ""
    """归一化后的文本。"""

    matched_alias: str = ""
    """命中动作的那条别名。"""

    dry_run: bool = False
    """True 时只报告将要做什么，不真的执行。"""

    slots: dict[str, Any] = field(default_factory=dict)
    """从句式（`patterns`）里抓到的槽位，如 {"q": "天气"}。"""

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


class OpenTargetAction(Action):
    """打开**运行时才确定**的应用（「打开QQ」里的 QQ）。

    和 open_app 的区别只有一个：目标不是配置里写死的，而是意图层从已安装应用
    索引里解析出来的，通过 `ctx.slots["app"]` 传进来。启动逻辑完全复用
    `launch_resolved`——两份实现迟早会不一致（一个支持 UWP，另一个忘了）。

    `target` 仍然可以写：那样它就是一条普通的 open_app，留着是为了让用户
    能把「打开音乐」固定绑到某个程序上。
    """

    handler_name = "open_target"

    def _resolve(self, ctx: ActionContext | None):  # noqa: ANN202
        app = (ctx.slots.get("app") if ctx else None)
        if app is not None:
            entry = AppEntry(name=app.name, appid=app.appid, system=app.system)
            r = entry.resolved()
            if r.ok:
                return r
        if self.cfg.target.strip():
            return resolve_app(self.cfg.target, self.cfg.aliases, self.cfg.describe)
        return None

    def preflight(self) -> ActionResult:
        if not self.cfg.target.strip():
            return ActionResult(True, "运行时才知道要开谁", "目标由应用索引动态解析")
        r = resolve_app(self.cfg.target, self.cfg.aliases, self.cfg.describe)
        return ActionResult(r.ok, f"找到 {self.cfg.target}" if r.ok else f"找不到 {self.cfg.target}", r.how)

    def execute(self, ctx: ActionContext) -> ActionResult:
        if ctx.dry_run:
            app = ctx.slots.get("app")
            return ActionResult(True, f"[dry-run] 将启动 {app.name if app else self.cfg.target}")
        r = self._resolve(ctx)
        if r is None:
            return ActionResult(False, "没解析出要打开哪个应用", "意图层没给出目标，target 也没写")
        if not r.ok:
            return ActionResult(False, f"找不到应用：{self.cfg.target or '（动态目标）'}", r.how)
        try:
            launch_resolved(r, self.cfg.args)
        except OSError as e:
            return ActionResult(False, f"启动失败：{e}", r.how)
        return ActionResult(True, f"已启动 {r.label or r.value}", r.how)


def launch_resolved(r, args: list[str] | None = None) -> None:  # noqa: ANN001 - appfind.Resolved
    """启动一个已解析的应用。失败抛 OSError，调用方负责翻译成 ActionResult。

    从 OpenAppAction 里抽出来，是因为「打开XX」的动态路径（没在配置里写过的应用）
    也要用同一套启动逻辑——两份实现迟早会不一致。
    """
    args = list(args or [])
    if r.kind == "exe":
        _popen([r.value, *args])
    elif r.kind == "shortcut":
        os.startfile(r.value)  # noqa: S606
    elif r.kind == "aumid":
        # UWP / 商店应用没有可直接启动的 exe，必须走 shell:AppsFolder
        _popen(["explorer.exe", f"shell:AppsFolder\\{r.value}"])
    else:  # shell
        _popen(["cmd", "/c", "start", "", r.value, *args])


class OpenAppAction(Action):
    handler_name = "open_app"

    RESOLVE_TTL = 600.0
    """解析结果缓存多久。实测每次重新解析要 ~54ms（对开始菜单里 195 项做 6 个名字的
    模糊比较），而整条语音链路的其它部分加起来也就 ~200ms。"""

    def __init__(self, cfg) -> None:  # noqa: ANN001
        super().__init__(cfg)
        self._cached: tuple[Any, float] | None = None

    def _resolve(self, *, executing: bool = False):  # noqa: ANN202
        now = time.monotonic()
        c = self._cached
        if c is not None and now - c[1] < self.RESOLVE_TTL and self._still_valid(c[0]):
            return c[0]
        # 只在真正执行时才允许"没找到就重扫开始菜单"：体检/预检也会找不到
        # （没装是正常状态），不能每次都为它们起一个 PowerShell
        r = resolve_app(
            self.cfg.target, self.cfg.aliases, self.cfg.describe, refresh_on_miss=executing
        )
        # 只缓存成功的：没找到时下一次要重新找（用户可能刚装好）
        self._cached = (r, now) if r.ok else None
        return r

    @staticmethod
    def _still_valid(r) -> bool:  # noqa: ANN001
        """缓存的 exe 被卸载/移走了就作废。aumid/shell 没法便宜地验证，信它。"""
        if r.kind in ("exe", "shortcut"):
            try:
                return Path(r.value).is_file()
            except OSError:
                return False
        return True

    def preflight(self) -> ActionResult:
        r = self._resolve()
        what = self.cfg.target or (self.cfg.aliases[:1] or ["(无名)"])[0]
        if r.ok:
            return ActionResult(True, f"找到 {what}", f"{r.how}: {r.value}")
        return ActionResult(False, f"找不到 {what}", r.how)

    def execute(self, ctx: ActionContext) -> ActionResult:
        r = self._resolve(executing=not ctx.dry_run)
        if not r.ok:
            return ActionResult(
                False,
                f"找不到应用：{self.cfg.target or '、'.join(self.cfg.aliases)}",
                r.how,
            )
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将启动 {r.label or r.value}", r.how)

        try:
            launch_resolved(r, self.cfg.args)
        except OSError as e:
            self._cached = None  # 启动失败的结果不能留在缓存里
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
# handler: close_app
# --------------------------------------------------------------------------- #

SHELL_EXES = frozenset({"explorer.exe"})
"""Windows 外壳进程。taskkill 杀掉它会把整个桌面（任务栏、图标）一起带走，
Windows 通常会自动重开，于是表现为"屏幕闪一下、窗口全没了"。
它该收到的是**关窗口**的消息，不是终止进程。"""

_GENTLE_HINTS: frozenset[str] = frozenset()
"""保留位：将来若要给个别应用强制走"温柔关闭"，把 exe 名加到这里。
现在不用它——判据是**实测那个进程有没有可见窗口**（见 `has_visible_window`），
比维护一张"哪些程序怕被强杀"的名单可靠得多。"""


def close_plan_for(
    names: list[str],
    *,
    has_window: Callable[[str], bool] | None = None,
    force: bool = False,
) -> list[tuple[str, str]]:
    """要关掉这些进程名，各用哪条命令。返回 [(exe 名, 方法)]。

    方法只有三种：
        taskkill     正常收尾（给它的顶层窗口发关闭消息）
        taskkill-f   立刻终止
        explorer     外壳进程专用：只关窗口，不杀进程

    为什么不能一律 /F：/F 是"立刻终止"，Office 这类程序来不及存盘，
    用户的文档就没了。所以**有窗口**的先走不带 /F 的那条，没有窗口的
    （后台服务、托盘常驻）不带 /F 会直接失败，只能强制。

    纯函数：窗口有无由调用方注入，测试因此不用真的起进程。
    """
    probe = has_window or has_visible_window
    out: list[tuple[str, str]] = []
    for raw in names:
        exe = normalize_exe(raw)
        if not exe:
            continue
        if exe in SHELL_EXES:
            out.append((exe, "explorer"))
            continue
        if force:
            out.append((exe, "taskkill-f"))
        elif probe(exe):
            out.append((exe, "taskkill"))
        else:
            out.append((exe, "taskkill-f"))
    return out


def normalize_exe(raw: str) -> str:
    """统一成 `xxx.exe` 小写形式，顺带挡掉路径和空白。

    只接受**纯文件名**：`taskkill /IM` 要的是进程映像名，不是路径；
    而带路径的输入一律取文件名，这样 `C:\\...\\Weixin.exe` 也能用。
    """
    s = raw.strip().strip('"').replace("/", "\\")
    if not s:
        return ""
    s = s.rsplit("\\", 1)[-1].lower()
    if not s.endswith(".exe"):
        s += ".exe"
    return s


def has_visible_window(exe: str) -> bool:
    """这个进程现在有没有带标题的窗口。

    用 `tasklist /V` 的"窗口标题"列：没有窗口的进程这一列是 `N/A`（实测确认）。
    多花一次约 100ms 的调用是值得的——它决定了关闭时**会不会丢掉用户没保存的文档**。
    """
    try:
        r = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/V", "/NH"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = (r.stdout or b"").decode("utf-8", errors="replace")
    if "N/A" == text.strip():
        return False
    for line in text.splitlines():
        # CSV 的最后一列是窗口标题；按引号切比按逗号切安全（标题里会有逗号）
        parts = line.rsplit('","', 1)
        if len(parts) < 2:
            continue
        title = parts[-1].rstrip('"').strip()
        if title and title.upper() != "N/A":
            return True
    return False


class CloseAppAction(Action):
    handler_name = "close_app"

    def _names(self) -> list[str]:
        """要关的进程名：动作自己的 target/exe，或运行时算出来的（见 ctx.extra）。"""
        return [str(x) for x in (self.cfg.args or [self.cfg.target]) if str(x).strip()]

    def preflight(self) -> ActionResult:
        names = self._names()
        if not cmds_available("taskkill"):
            return ActionResult(False, "系统里没有 taskkill", "正常 Windows 都自带它")
        if not names:
            return ActionResult(True, "运行时才知道要关谁", "目标由应用索引动态解析")
        return ActionResult(True, f"将关闭 {'、'.join(names)}")

    def execute(self, ctx: ActionContext) -> ActionResult:
        if ctx.dry_run:
            names = ctx.extra.get("exe_names") or self._names()
            return ActionResult(True, f"[dry-run] 将关闭 {'、'.join(names) or '(未指定)'}")
        names = [str(n) for n in (ctx.extra.get("exe_names") or self._names())]
        force = bool(ctx.extra.get("force"))
        if not names:
            return ActionResult(False, "不知道该关哪个进程", "没解析出应用名")

        killed: list[str] = []
        failed: list[str] = []
        for exe, method in close_plan_for(names, force=force):
            if method == "explorer":
                _close_explorer_windows()
                killed.append(f"{exe} 的窗口")
                continue
            args = ["taskkill", "/IM", exe] + (["/F"] if method == "taskkill-f" else [])
            if _run(args):
                killed.append(exe if method == "taskkill" else f"{exe}（强制）")
            else:
                failed.append(exe)

        if killed and not failed:
            return ActionResult(True, f"已关闭 {'、'.join(killed)}")
        if killed:
            return ActionResult(True, f"已关闭 {'、'.join(killed)}", f"没找到：{'、'.join(failed)}")
        return ActionResult(False, f"没找到正在运行的 {'、'.join(failed)}", "它可能本来就没开")


def cmds_available(cmd: str) -> bool:
    """系统里有没有这个命令。preflight 用它给出"这台机器上能不能跑通"。"""
    from shutil import which

    return which(cmd) is not None


def _run(args: list[str]) -> bool:
    """跑一条系统命令，只看成败。不捕获输出：taskkill 的报错文本对用户没用。"""
    try:
        r = subprocess.run(  # noqa: S603
            args, capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _close_explorer_windows() -> None:
    """壳层的 `关闭所有窗口`：explorer 进程留着，只把窗口收掉。"""
    try:
        subprocess.run(  # noqa: S603
            ["taskkill", "/IM", "explorer.exe"], capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

HANDLERS: dict[str, type[Action]] = {
    "open_app": OpenAppAction,
    "open_target": OpenTargetAction,
    "open_path": OpenPathAction,
    "open_url": OpenUrlAction,
    "sysctl": SysctlAction,
    "keys": KeysAction,
    "shell": ShellAction,
    "close_app": CloseAppAction,
}


def _register_schedule() -> None:
    """日程 handler 单独注册。

    它住在 `schedule_action.py` 而不是这个文件里：那个模块要 import
    `..schedule`（存储/提醒），而 `schedule.py` 又要在启动时被 runner 用到。
    放在一起会变成 `actions → schedule → actions` 的循环导入。运行时再 import
    一次，代价是一次模块查找，换来依赖图是单向的。
    """
    from .schedule_action import ScheduleAction

    HANDLERS["schedule"] = ScheduleAction


_register_schedule()


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
