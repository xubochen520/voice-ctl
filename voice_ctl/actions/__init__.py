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
    """打开一个网址。

    地址有两个来源，和 open_app / open_target 那一对是一个道理：

      * `target` 写在配置里 —— 用户固定几个常用站（`[[action]] web.xxx`）
      * `ctx.slots["url"]` —— 运行时解析出来的（「打开百度」→ baidu.com）

    第二个来源在 0.3.3 加的。**运行时给的优先**：那是用户这句话真正的目标，
    而配置里的 target 只是模板/兜底。
    """

    handler_name = "open_url"

    def _url(self, ctx: ActionContext | None) -> tuple[str, str]:
        """返回 (网址, 显示名)。运行时给的优先——那是这句话真正的目标。"""
        if ctx is not None:
            slot = str(ctx.slots.get("url") or "").strip()
            if slot:
                return slot, str(ctx.slots.get("url_name") or slot)
        t = self.cfg.target.strip()
        return t, t

    def preflight(self) -> ActionResult:
        t = self.cfg.target.strip()
        if not t:
            # 运行时才知道开哪个站，和 open.target 一样，预检只能放行
            return ActionResult(True, "运行时才知道要开哪个站", "地址由站点表/搜索解析")
        if "://" not in t and not t.endswith(":"):
            return ActionResult(
                True, f"{t} 看起来像域名，浏览器会补协议", "建议写成 https://..."
            )
        return ActionResult(True, f"打开 {t}")

    def execute(self, ctx: ActionContext) -> ActionResult:
        t, label = self._url(ctx)
        if not t:
            return ActionResult(False, "没有要打开的网址", "target 为空，槽位里也没有 url")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将打开 {label}" + (f"（{t}）" if t != label else ""))
        try:
            # webbrowser 会走系统默认浏览器；协议式 URI（ms-settings: 等）也能开
            ok = webbrowser.open(t, new=2)
        except Exception as e:  # noqa: BLE001
            return ActionResult(False, f"打开失败：{e}")
        if not ok:
            # 退一步用 start，能处理更多协议式 URI
            try:
                _popen(["cmd", "/c", "start", "", t])
                return ActionResult(True, f"已打开 {label}", "webbrowser 返回失败，改用 start")
            except OSError as e:
                return ActionResult(False, f"打开失败：{e}")
        note = "搜索页（站点表里没有这个站）" if ctx.slots.get("via_search") else ""
        return ActionResult(True, f"已打开 {label}", note)


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


@dataclass(frozen=True)
class Proc:
    """一个正在运行的进程。"""

    pid: int
    name: str
    """映像名（`launcher.exe`）。**不能拿它单独定位进程**，见 `close_plan_for`。"""
    path: str = ""
    """完整路径。拿不到时是空串（权限不足、进程刚退出）。"""


def _psutil_procs() -> list[Proc]:
    import psutil

    out: list[Proc] = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        info = p.info
        out.append(Proc(int(info.get("pid") or 0), str(info.get("name") or ""),
                        str(info.get("exe") or "")))
    return out


def _wmi_procs() -> list[Proc]:
    r = subprocess.run(  # noqa: S603
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process | "
         "ForEach-Object { $_.ProcessId.ToString() + \"`t\" + $_.Name + \"`t\" + $_.ExecutablePath }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out: list[Proc] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        out.append(Proc(int(parts[0]), parts[1].strip(), parts[2].strip() if len(parts) > 2 else ""))
    return out


def running_processes() -> list[Proc]:
    """列出正在运行的进程（带路径）。失败返回空列表。

    先试 psutil（实测 16ms / 423 个进程），没有就退回 PowerShell + CIM
    （同样的机器 677ms）。**40 倍差距**落在用户松开热键等结果的那一刻，
    所以 psutil 进了正式依赖，但仍然保留 WMI 兜底——它在任何 Windows 上都能用，
    不依赖任何二进制扩展。
    """
    try:
        return _psutil_procs()
    except Exception:  # noqa: BLE001 - 没装 psutil / 权限不足 / 别的意外，退回 WMI
        pass
    try:
        return _wmi_procs()
    except Exception:  # noqa: BLE001
        return []


def close_plan_for(
    names: list[str],
    *,
    paths: list[str] | None = None,
    procs: list[Proc] | None = None,
    has_window: Callable[[str], bool] | None = None,
    force: bool = False,
    describe: str = "",
) -> list[tuple[str, str, str]]:
    """要关掉这些东西，各用哪条命令。返回 [(显示名, 方法, 参数)]。

    方法：
        pid        按 PID 终止某个**确切**的进程（最安全，优先用）
        taskkill   按映像名让所有同名进程优雅退出
        taskkill-f 按映像名强制终止所有同名进程
        explorer   外壳进程专用：只关窗口，不杀进程

    ## 为什么优先按 PID

    用户的实测报告：「关闭米哈游启动器」命中了，但报的是
    `没找到正在运行的 launcher.exe`。顺着这条线查下去，发现**这台机器上三个
    完全不同的启动器都叫 launcher.exe**：

        米哈游启动器  E:\\mihoyou\\miHoYo Launcher\\launcher.exe
        鸣潮          E:\\Wuthering Waves\\launcher.exe
        鹰角启动器    E:\\Hypergryph Launcher\\Launcher.exe

    也就是说 `taskkill /IM launcher.exe` 会把三个一起关掉。用户说关米哈游，
    鸣潮和鹰角跟着消失——而且他大概率不会立刻把这两件事联系起来。

    所以：**有完整路径就按路径精确定位进程，只杀那一个 PID**。
    拿不到路径（UWP、系统命令名兜底）才退回按映像名。

    ## 为什么不能一律 /F

    /F 是"立刻终止"，Office 这类程序来不及存盘。所以有窗口的先走不带 /F 的
    那条；没有窗口的（后台常驻、托盘程序）不带 /F 会直接失败，只能强制。

    纯函数：进程列表和窗口有无都由调用方注入，测试因此不用真的起进程。
    """
    procs = running_processes() if procs is None else procs
    probe = has_window or has_visible_window
    out: list[tuple[str, str, str]] = []
    seen_pids: set[int] = set()

    # 路径 → 进程。路径比较用小写规范化：Windows 不区分大小写，而且开始菜单给的
    # 路径和进程报的路径斜杠方向可能不同。
    by_path: dict[str, Proc] = {}
    for p in procs:
        if p.path:
            by_path[normalize_path(p.path)] = p

    # 映像名 → 进程。**只用来补出"该用哪个路径"，不用来决定关谁。**
    #
    # 为什么需要这一步：UWP 应用在开始菜单里是 AUMID（`Microsoft.WindowsNotepad_…!App`），
    # 没有 exe 路径，只能拿系统命令名推出 `notepad.exe`。可它的真身是
    # `...\WindowsApps\...\Notepad.exe`——按映像名 `taskkill /IM` 关它时灵时不灵
    # （实测 8 次里错 3 次），而按 PID 关 10 次全成。
    #
    # 所以：有映像名就去进程表里认领一个**同名**进程的完整路径，然后走精确路径。
    # 认领的前提是名字真的对得上，绝不能凭"看起来像"就认。
    by_name: dict[str, list[Proc]] = {}
    for p in procs:
        key = normalize_exe(p.name)
        if key:
            by_name.setdefault(key, []).append(p)

    # 要处理的目标，按"有没有确切路径"分成两拨。
    want_paths: list[str] = []
    seen_path_keys: set[str] = set()
    for raw_path in (paths or []):
        k = normalize_path(raw_path)
        if k and k not in seen_path_keys:
            seen_path_keys.add(k)
            want_paths.append(raw_path)

    covered: set[str] = set()
    """已经能按路径定位的映像名。

    这些名字**绝不能**再走"按映像名"那条路——它不可靠，而且会把同名的别的程序
    一起关掉。所以"路径给了但进程没在跑"的正确结果是**什么都不做**，
    而不是退回去 `taskkill /IM launcher.exe`（那正是要修的缺陷）。
    """
    for raw_path in want_paths:
        exe = normalize_exe(raw_path)
        if exe:
            covered.add(exe)
    want_names: list[str] = []
    for raw in names:
        exe = normalize_exe(raw)
        if not exe or exe in covered or exe in want_names:
            continue
        # 先试着**认领一个同名进程的路径**，让它走精确那条路。
        #
        # 两个前提，都是实测踩出来的：
        #   * 映像名要**真的相同**——`notepad++.exe` 不是 `notepad.exe`，
        #     认错了就会去关一个完全无关的程序；
        #   * 优先挑**拿得到路径**的那个同名进程——同名的进程里有的读不到路径
        #     （权限不足），随手挑第一个会白白放弃精确路径。
        same = [p for p in by_name.get(exe, []) if p.pid]
        hit = next((p for p in same if p.path), same[0] if same else None)
        if hit is not None:
            if hit.path:
                k = normalize_path(hit.path)
                if k not in seen_path_keys:
                    seen_path_keys.add(k)
                    want_paths.append(hit.path)
                covered.add(exe)
                continue
            # 同名进程一个路径都拿不到。不能就地退回按映像名去关：那会把同名的
            # **那一批**全关掉，包括我们本来不该碰的。如实按名字处理，让用户
            # 看到"可能需要管理员权限"，而不是误伤。
        want_names.append(exe)

    def kill(what: str, exe: str, hit: Proc) -> None:
        if hit.pid in seen_pids:
            return
        seen_pids.add(hit.pid)
        label = describe or what
        if exe in SHELL_EXES:
            out.append((label, "explorer", ""))
        else:
            # **不查窗口了。** 有窗口与否只用来决定"先优雅还是直接强杀"，而
            # `has_visible_window` 每次要起一个 tasklist（实测 400ms）——用户
            # 松开热键正等着结果，这 400ms 花得不值。改成统一先优雅关闭，
            # 真的没退再降级强杀（见 CloseAppAction.execute），结果一样而更快。
            out.append((label, "pid", f"{hit.pid}/f" if force else str(hit.pid)))

    for raw_path in want_paths:
        hit = by_path.get(normalize_path(raw_path))
        if hit is not None and hit.pid:
            kill(normalize_exe(raw_path), normalize_exe(raw_path), hit)

    for exe in want_names:
        if exe in SHELL_EXES:
            if any(p.pid for p in by_name.get(exe, [])):
                out.append((describe or exe, "explorer", ""))
            continue
        if not by_name.get(exe):
            # 进程表里压根没有这个名字 → 它没在跑。**不生成计划**。
            #
            # 不能只看"有没有可见窗口"就决定发不发 taskkill：那是在拿一个
            # 400ms 的旁证去回答一个我们手里已经有答案的问题，而且答错了会
            # 让用户看到「没能关掉」（听起来像权限问题），其实是"它本来就没开"。
            continue
        if force:
            out.append((describe or exe, "taskkill-f", exe))
        elif probe(exe):
            out.append((describe or exe, "taskkill", exe))
        else:
            out.append((describe or exe, "taskkill-f", exe))
    return out


def normalize_path(raw: str) -> str:
    """路径比较用的规范形式：小写 + 反斜杠。"""
    return raw.strip().strip('"').replace("/", "\\").lower()


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


GRACEFUL_WAIT = 1.5
"""发完关闭消息后最多等多久（秒）。程序要过一会儿才真的退出。"""
FORCE_WAIT = 1.0
"""强制终止后最多等多久（秒）。/F 基本是即时的，等这么久是防卡顿。"""


def pid_alive(pid: int) -> bool:
    """这个 PID 还在不在。

    **不能拿 taskkill 的退出码当结论。** 实测：`taskkill /PID <n>` 目标确实关了，
    退出码却可能是 1。照着退出码报错的话，用户看到「没能关掉」而窗口其实已经
    消失——他会以为功能坏了，然后反复重试。

    所以判据是"进程还在不在"这个事实，不是命令的退出码。

    实现上用 `OpenProcess`（内核级，微秒级返回），不用 `tasklist`：后者每次要
    起一个进程，实测 360ms，而这条路要轮询好几次——用户松开热键正等着结果。
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        return _pid_alive_slow(pid)
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        STILL_ACTIVE = 259
        h = k32.OpenProcess(SYNCHRONIZE | 0x1000, False, pid)  # 0x1000 = PROCESS_QUERY_LIMITED
        if not h:
            err = ctypes.get_last_error()
            # 5 = 拒绝访问：进程还在，只是我们无权打开它（比如它以管理员身份在跑）。
            # 报"还活着"比报"已关闭"安全——宁可多报一次失败，也不要谎报成功。
            return err == 5
        try:
            if k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT:
                return True
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001 - 拿不准就退回慢的那条
        return _pid_alive_slow(pid)


def _pid_alive_slow(pid: int) -> bool:
    """`tasklist` 版本。慢（约 360ms）但哪都能用，作为兜底。"""
    try:
        r = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return True
    out = r.stdout or ""
    if "No tasks" in out or "没有运行" in out:
        return False
    return str(pid) in out


def _wait_gone(pid: int, timeout: float) -> bool:
    """等这个进程消失，最多 timeout 秒。返回它是否真的没了。

    轮询而不是 sleep 固定时长：大多数程序几十毫秒就退了，干等 1.5 秒会让
    「关闭微信」这句话平白慢一倍。实测有窗口的程序平均在 100-400ms 内退出。
    """
    deadline = time.monotonic() + timeout
    while True:
        if not pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


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
        names = [str(n) for n in (ctx.extra.get("exe_names") or self._names())]
        paths = [str(p) for p in (ctx.extra.get("exe_paths") or [])]
        force = bool(ctx.extra.get("force"))
        label = str(ctx.extra.get("app_name") or "")
        if not names and not paths:
            return ActionResult(False, "不知道该关哪个进程", "没解析出应用名")

        if ctx.dry_run:
            plan = close_plan_for(names, paths=paths, force=force, describe=label)
            if not plan:
                return ActionResult(
                    False, f"{label or '它'}现在没在运行", "dry-run：预览的结果和真跑一致"
                )
            how = "；".join(f"{d} → {m}" for d, m, _ in plan)
            return ActionResult(True, f"[dry-run] 将关闭 {how}")

        plan = close_plan_for(names, paths=paths, force=force, describe=label)
        if not plan:
            return ActionResult(False, f"{label or '、'.join(names)} 现在没在运行",
                                "它可能本来就没开")

        killed: list[str] = []
        failed: list[str] = []
        for what, method, arg in plan:
            if method == "explorer":
                _close_explorer_windows()
                killed.append(f"{what} 的窗口")
                continue
            if method == "pid":
                pid_s, _, hard = arg.partition("/")
                pid = int(pid_s)
                _run(["taskkill", "/PID", pid_s] + (["/F"] if hard else []))
                if not hard:
                    # 优雅关闭是**异步**的：taskkill 只是把关闭消息投出去，
                    # 程序要过一会儿才真的退出。实测记事本/微信只要几十毫秒，
                    # 但没等就查会看到"还在"——于是同一句话时灵时不灵。
                    if not _wait_gone(pid, GRACEFUL_WAIT):
                        # 它不理会关闭消息（托盘常驻、后台服务）。用户说"关闭"
                        # 就是要它关掉，补一次强制——留一个半死的进程不算完成。
                        _run(["taskkill", "/PID", pid_s, "/F"])
                        _wait_gone(pid, FORCE_WAIT)
                if pid_alive(pid):
                    failed.append(what)
                else:
                    killed.append(what)
                continue
            ok = _run(["taskkill", "/IM", arg] + (["/F"] if method == "taskkill-f" else []))
            if ok:
                killed.append(what if method != "taskkill-f" else f"{what}（强制）")
            else:
                failed.append(what)

        if killed and not failed:
            return ActionResult(True, f"已关闭 {'、'.join(killed)}")
        if killed:
            return ActionResult(True, f"已关闭 {'、'.join(killed)}", f"没关掉：{'、'.join(failed)}")
        # 一条都没关掉。分清"它本来就没开"和"它开着但关不掉"——这两件事对用户
        # 的含义完全不同：前者不用管，后者要去看权限或者手动关。
        #
        # 判据是"计划里有没有按名字兜底的那一条"：按名字走意味着我们在进程表里
        # **没认出**它（认得出来就会走精确路径了）。所以有按名字的条目 = 它开着
        # 但我们只能对着映像名喊话，多半是权限问题。
        by_name_only = any(m in ("taskkill", "taskkill-f") for _w, m, _a in plan)
        if by_name_only:
            return ActionResult(False, f"{label or '、'.join(failed)} 没能关掉",
                                "进程还在——可能需要管理员权限，或者它是开机自启的")
        return ActionResult(False, f"{label or '、'.join(names)} 现在没在运行",
                            "它可能本来就没开")


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
