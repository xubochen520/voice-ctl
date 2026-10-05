"""找到应用的可执行文件。

Windows 上「打开微信」没有官方 URI 协议，`start wechat:` 之类并不存在。
但系统有一个**权威数据源**：开始菜单应用列表（`Get-StartApps` / `shell:AppsFolder`）。
它同时覆盖三类东西：

    * 传统程序（返回 exe 路径）        微信 → E:\\weixin\\Weixin.exe
    * UWP / 商店应用（返回 AUMID）      计算器 → Microsoft.WindowsCalculator_8wekyb3d8bbwe!App
    * 系统设置等（返回协议式 AppID）    设置 → windows.immersivecontrolpanel_cw5n1h2txyewy!...

所以解析顺序是：
    1. target 本身就是存在的文件
    2. PATH
    3. **开始菜单**（覆盖最广，含 UWP）—— 用 name / describe 一起匹配
    4. App Paths 注册表
    5. 硬编码的常见安装路径（开始菜单偶尔不全，比如新装的程序还没索引）
    6. 系统命令，兜底交给 `start`

每一步都返回「为什么」，因为用户最需要知道的是"为什么没打开"。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


@dataclass
class Resolved:
    kind: str
    """exe / shortcut / aumid / shell / fail"""

    value: str
    how: str
    """解析方式，写日志和报错时用。"""

    display: str = ""
    """给用户看的名字。"""

    @property
    def ok(self) -> bool:
        return self.kind != "fail"

    @property
    def label(self) -> str:
        return self.display or (Path(self.value).name if self.value else "")


# 已知应用：别名 -> 可执行文件名 + 常见安装目录
# 这一层是**兜底**：开始菜单通常已经能找到，但新装/未索引的程序需要它。
_KNOWN: dict[str, dict[str, object]] = {
    "wechat": {
        "exe": ["weixin.exe", "wechat.exe", "xwechat.exe"],
        "dirs": [
            r"C:\Program Files\Tencent\Weixin",
            r"C:\Program Files (x86)\Tencent\Weixin",
            r"C:\Program Files\Tencent\WeChat",
            r"C:\Program Files (x86)\Tencent\WeChat",
            r"{LOCALAPPDATA}\Programs\WeChat",
            r"D:\Program Files\Tencent\Weixin",
            r"E:\weixin",
            r"D:\weixin",
        ],
        "names": ["微信", "weixin", "wechat"],
    },
    "qq": {
        "exe": ["QQ.exe", "QQScLauncher.exe"],
        "dirs": [r"C:\Program Files\Tencent\QQNT", r"C:\Program Files (x86)\Tencent\QQ"],
        "names": ["qq", "腾讯qq"],
    },
    "dingtalk": {
        "exe": ["DingTalk.exe"],
        "dirs": [r"{LOCALAPPDATA}\DingTalk", r"C:\Program Files (x86)\DingDing"],
        "names": ["钉钉", "dingtalk"],
    },
    "feishu": {
        "exe": ["Feishu.exe", "Lark.exe"],
        "dirs": [r"{LOCALAPPDATA}\Feishu", r"{LOCALAPPDATA}\Lark"],
        "names": ["飞书", "lark", "feishu"],
    },
    "vscode": {
        "exe": ["Code.exe"],
        "dirs": [r"{LOCALAPPDATA}\Programs\Microsoft VS Code"],
        "names": ["vscode", "vs code", "code", "代码"],
    },
    "chrome": {
        "exe": ["chrome.exe"],
        "dirs": [
            r"C:\Program Files\Google\Chrome\Application",
            r"C:\Program Files (x86)\Google\Chrome\Application",
        ],
        "names": ["chrome", "谷歌浏览器"],
    },
    "edge": {
        "exe": ["msedge.exe"],
        "dirs": [r"C:\Program Files (x86)\Microsoft\Edge\Application"],
        "names": ["edge", "微软浏览器"],
    },
    "netease_music": {
        "exe": ["cloudmusic.exe"],
        "dirs": [r"C:\Program Files (x86)\Netease\CloudMusic"],
        "names": ["网易云", "网易云音乐"],
    },
}

# 中文名 → 系统命令。这些是 Windows 自带的，中文用户会说中文名。
_SYSTEM_ALIASES: dict[str, str] = {
    "记事本": "notepad",
    "笔记本": "notepad",
    "计算器": "calc",
    "画图": "mspaint",
    "任务管理器": "taskmgr",
    "进程管理": "taskmgr",
    "资源管理器": "explorer",
    "文件管理器": "explorer",
    "我的电脑": "explorer",
    "命令行": "cmd",
    "控制台": "cmd",
    "注册表": "regedit",
    "控制面板": "control",
}

_SHELL_COMMANDS = {
    "explorer", "notepad", "calc", "mspaint", "taskmgr", "cmd", "powershell",
    "pwsh", "wt", "control", "regedit", "snippingtool", "tasklist",
}


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p))


# --------------------------------------------------------------------------- #
# 开始菜单应用列表
# --------------------------------------------------------------------------- #

_START_APPS: list[tuple[str, str]] | None = None
_START_AT = 0.0
"""最近一次加载（含失败）的 time.monotonic()。限频刷新用它。"""
_START_LOCK = threading.Lock()

REFRESH_MIN_INTERVAL = 30.0
"""「没找到」触发的重新扫描，最短间隔（秒）。每次扫描要起一次 PowerShell。"""


def start_apps(refresh: bool = False) -> list[tuple[str, str]]:
    """返回 [(显示名, AppID或路径)]，来自系统开始菜单。

    AppID 可能是三种东西：
      * AUMID（`包名!AppId`）—— UWP/商店应用，要用 shell:AppsFolder 启动
      * 完整 exe 路径 —— 传统程序
      * 协议式 ID —— 系统设置之类

    结果缓存：这个调用要起一次 PowerShell，实测约 0.8 秒（冷启动），不能每次按键
    都跑，更不能留到用户第一次说话时才跑——见 `warm_start_apps()`。
    加了锁：预热线程和第一次说话几乎同时到达时，不该起两个 PowerShell。
    """
    global _START_APPS, _START_AT
    if _START_APPS is not None and not refresh:
        return _START_APPS
    if sys.platform != "win32":
        _START_APPS = []
        return _START_APPS

    with _START_LOCK:
        if _START_APPS is not None and not refresh:
            return _START_APPS  # 等锁的时候别的线程已经加载好了

        ps = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            # 失败也记下时间：不缓存"永远没有应用"，过了限频间隔还能重试
            _START_AT = time.monotonic()
            if _START_APPS is None:
                _START_APPS = []
            return _START_APPS

        out: list[tuple[str, str]] = []
        for line in (r.stdout or "").splitlines():
            if "\t" not in line:
                continue
            name, appid = line.split("\t", 1)
            name, appid = name.strip(), appid.strip()
            if name and appid:
                out.append((name, appid))
        _START_APPS = out
        _START_AT = time.monotonic()
        return out


def warm_start_apps() -> threading.Thread | None:
    """在后台把开始菜单列表加载好。已经有缓存就什么都不做。

    不预热的代价（实测）：第一次说"打开XX"时多等约 0.8 秒——而且是用户刚松开
    热键、正等着看结果的那一刻。
    """
    if _START_APPS is not None or sys.platform != "win32":
        return None
    t = threading.Thread(target=start_apps, name="voice-ctl-startapps", daemon=True)
    t.start()
    return t


def refresh_start_apps_if_stale(min_interval: float = REFRESH_MIN_INTERVAL) -> bool:
    """距上次扫描超过 min_interval 才重新扫描，返回是否真的扫了。

    用在"没找到这个应用"之后：用户可能刚装好它。以前缓存只加载一次，
    新装的软件要重启 voice-ctl 才认。限频是为了防止一句口误反复起 PowerShell。
    """
    if sys.platform != "win32":
        return False
    if _START_AT and time.monotonic() - _START_AT < min_interval:
        return False
    start_apps(refresh=True)
    return True


class _Loose(str):
    """标记：这个名字是**一整句描述**（describe），不是应用名。

    describe 写的是「打开微信，用来聊天、发消息」这种句子，应用名只是句子里的一部分，
    所以对它必须保留旧的宽松包含匹配。别名和 target 则是"应用名"，用严格匹配。
    用 str 子类而不是额外参数，是为了不改 `from_start_menu(names)` 的签名。
    """


_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
STRICT_FLOOR = 0.80
"""严格匹配的及格线。旧的 0.72 太松：difflib 比出来 0.73 的
`visual studio code` ↔ `Visual Studio Installer` 就会被当成同一个应用。"""


def _norm_name(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


def name_tokens(text: str) -> list[str]:
    """英文/数字连续段、汉字连续段各算一个词元：'QQ音乐' → ['qq', '音乐']。"""
    return _TOKEN_RE.findall(_norm_name(text))


def strict_score(query: str, shown: str) -> float:
    """用户说的应用名 vs 系统里的显示名，0-1。比 `_similarity` 严格。

    核心区别是**方向**：
      * 显示名被包含在用户说的名字里（「QQ音乐」vs「QQ」）——用户多说的那部分没有
        着落，几乎肯定想要的是另一个应用。按覆盖率打折，压到及格线以下。
        旧版在这里给了 0.90，于是说「QQ音乐」会启动 QQ。
      * 用户说的是显示名的一部分（「code」vs「Visual Studio Code」）——这是人们
        真正会说的简称。说的每个词元都是名字里完整的词元才给高分。
    """
    q, s = _norm_name(query), _norm_name(shown)
    if not q or not s:
        return 0.0
    if q == s:
        return 1.0
    qt, st = _TOKEN_RE.findall(q), _TOKEN_RE.findall(s)
    qc, sc = "".join(qt), "".join(st)
    if not qc or not sc:
        return 0.0
    if qc == sc:
        return 0.98  # 只差空格/标点
    if len(qc) >= 2 and len(sc) >= 2:
        if sc in qc:
            return 0.9 * len(sc) / len(qc)
        if qc in sc:
            if all(t in st for t in qt):
                return 0.86
            return 0.5 + 0.45 * len(qc) / len(sc)
    r = SequenceMatcher(None, qc, sc).ratio()
    return r * 0.95 if r >= 0.85 else r * 0.6


def _similarity(a: str, b: str) -> float:
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        r = max(r, 0.80 + 0.20 * (min(len(a), len(b)) / max(len(a), len(b))))
    return r


def from_start_menu(names: list[str]) -> Resolved | None:
    """按名字在开始菜单里找。names 里越靠前优先级越高。

    普通名字走 `strict_score`（及格线 0.80）；`_Loose` 标记的整句描述走旧的
    宽松匹配（及格线 0.72）。取得分最高的那个。
    """
    apps = start_apps()
    if not apps:
        return None

    best: tuple[float, str, str] | None = None
    for want in names:
        if not str(want).strip():
            continue
        loose = isinstance(want, _Loose)
        floor = 0.72 if loose else STRICT_FLOOR
        for shown, appid in apps:
            score = _similarity(want, shown) if loose else strict_score(want, shown)
            if score >= floor and (best is None or score > best[0]):
                best = (score, shown, appid)

    if best is None:
        return None
    score, shown, appid = best

    if appid.lower().endswith(".exe") or Path(appid).is_file():
        return Resolved("exe", appid, f"开始菜单「{shown}」(相似度 {score:.2f})", shown)
    # AUMID / 协议式 ID —— 交给 shell:AppsFolder
    return Resolved("aumid", appid, f"开始菜单「{shown}」(相似度 {score:.2f})", shown)


# --------------------------------------------------------------------------- #
# 其他来源
# --------------------------------------------------------------------------- #


def _from_app_paths(exe_name: str) -> Resolved | None:
    """查 HKCU/HKLM 的 App Paths 注册表——安装程序登记过就能找到。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return None

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root, label in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as k:
                    val, _ = winreg.QueryValueEx(k, None)
                if val and Path(val).is_file():
                    return Resolved("exe", val, f"注册表 {label} App Paths\\{exe_name}")
            except OSError:
                continue
    return None


def _known_app(target: str) -> Resolved | None:
    """在已知应用表里找。target 可能是中文别名、英文名或 exe 名。"""
    key = target.strip().lower().replace(" ", "")
    entry = None
    for k, v in _KNOWN.items():
        names = [str(n).lower().replace(" ", "") for n in v.get("names", [])]  # type: ignore[union-attr]
        exes = [str(e).lower().removesuffix(".exe") for e in v.get("exe", [])]  # type: ignore[union-attr]
        if key == k or key in names or key in exes:
            entry = v
            break
    if entry is None:
        return None

    for exe in entry.get("exe", []):  # type: ignore[union-attr]
        w = shutil.which(str(exe))
        if w:
            return Resolved("exe", w, "PATH")
        reg = _from_app_paths(str(exe))
        if reg:
            return reg

    for d in entry.get("dirs", []):  # type: ignore[union-attr]
        base = _expand(str(d))
        if not base.is_dir():
            continue
        for exe in entry.get("exe", []):  # type: ignore[union-attr]
            cand = base / str(exe)
            if cand.is_file():
                return Resolved("exe", str(cand), f"常见安装路径 {base}")
    return None


def _system_alias(target: str) -> Resolved | None:
    """中文名 → 系统命令，然后再走 PATH。"""
    cmd = _SYSTEM_ALIASES.get(target.strip().lower())
    if cmd is None:
        return None
    w = shutil.which(cmd)
    if w:
        return Resolved("exe", w, f"系统命令 {cmd}（由「{target}」映射）")
    return Resolved("shell", cmd, f"系统命令 {cmd}（由「{target}」映射）")


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def resolve_app(
    target: str,
    aliases: list[str] | None = None,
    describe: str = "",
    *,
    refresh_on_miss: bool = False,
) -> Resolved:
    """把用户说的/配置里写的应用名解析成可启动的东西。

    aliases 与 describe 都会参与开始菜单匹配——用户往往在 describe 里写了
    更接近系统显示名的说法（"任务管理器" vs 别名 "进程管理"）。

    `refresh_on_miss`：全都没找到时，限频重新扫一次开始菜单再试一遍（用户可能
    刚装好它）。**只该在真正执行时开**：预检/体检也会找不到（没装是正常状态），
    不能让它们每次都去起 PowerShell。
    """
    t = (target or "").strip()

    # 1. 直接是存在的文件路径
    if t:
        p = _expand(t)
        if p.is_file():
            return Resolved("exe", str(p), "配置里的绝对路径", p.stem)

    candidates: list[str] = []
    for c in [t, *(aliases or [])]:
        c = (c or "").strip()
        if c and c not in candidates:
            candidates.append(c)

    # 2. PATH（含中文名映射）
    for cand in candidates:
        w = shutil.which(cand)
        if w:
            return Resolved("exe", w, "PATH", cand)

    # 3. 中文名 → 系统命令
    for cand in candidates:
        sa = _system_alias(cand)
        if sa:
            return sa

    # 4. 开始菜单（覆盖 UWP / 商店应用，这层最管用）
    names: list[str] = [t, *(aliases or [])]
    if describe:
        names.append(_Loose(describe))
    sm = from_start_menu(names)
    if sm:
        return sm

    # 5. App Paths 注册表
    for cand in candidates:
        exe_name = cand if cand.lower().endswith(".exe") else f"{cand}.exe"
        reg = _from_app_paths(exe_name)
        if reg:
            return reg

    # 6. 已知应用表
    for cand in candidates:
        known = _known_app(cand)
        if known:
            return known

    # 7. 系统命令兜底
    for cand in candidates:
        low = cand.lower()
        if low in _SHELL_COMMANDS or low.removesuffix(".exe") in _SHELL_COMMANDS:
            return Resolved("shell", cand, "系统命令，交给 cmd start 解析", cand)

    if refresh_on_miss and refresh_start_apps_if_stale():
        sm = from_start_menu(names)
        if sm:
            return sm

    tried = ", ".join(candidates) or "(空)"
    return Resolved("fail", "", f"找不到可执行文件，尝试过：{tried}")
