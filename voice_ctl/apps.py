"""动态应用词典：「打开XX」对**任何已安装的应用**生效，不用在 config.toml 里一个个配。

原来的做法是封闭集合：想说「打开QQ」就得手写一段 [[action]]。但系统其实早就有一份
权威的应用清单——开始菜单（实测这台机器 195 项，`appfind.start_apps()` 已经把它读进
内存了），只是匹配器从来没用它来**理解口令**，只在动作已经选定之后才拿它找 exe。

这一层做三件事：

  1. **名字匹配**：用 `appfind.strict_score`（方向敏感：说「QQ音乐」不会落到「QQ」上）
  2. **拼音匹配**：ASR 把「微信」听成「威信」「薇信」是常态，全拼（无声调）相同就认——
     这比手工维护同音替换表可持续得多
  3. **昵称表**：「扣扣」「企鹅」→ QQ 这类没有任何字面关系的叫法，只能查表。
     内置一份常见的，用户可以在 `[apps].nicknames` 里加自己的

拿不准时**问**而不是猜：得分够高且和第二名拉开距离才直接执行，否则返回候选让界面问一句。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath

from . import appfind
from .appfind import Resolved, strict_score

# 说法 → 系统里的显示名（一个或几个候选写法：开始菜单里可能叫 "微信" 也可能叫 "WeChat"）。
# 只有目标应用确实装了才会生效，没装就什么都不发生，所以放心写得宽一点。
BUILTIN_NICKNAMES: dict[str, tuple[str, ...]] = {
    "扣扣": ("QQ",),
    "企鹅": ("QQ",),
    "腾讯qq": ("QQ",),
    "腾讯扣扣": ("QQ",),
    "威信": ("微信", "WeChat", "Weixin"),
    "薇信": ("微信", "WeChat", "Weixin"),
    "wechat": ("微信", "WeChat", "Weixin"),
    "weixin": ("微信", "WeChat", "Weixin"),
    "网易云": ("网易云音乐", "NetEase Cloud Music", "CloudMusic"),
    "网抑云": ("网易云音乐", "NetEase Cloud Music", "CloudMusic"),
    "云音乐": ("网易云音乐", "NetEase Cloud Music", "CloudMusic"),
    "谷歌浏览器": ("Google Chrome", "Chrome"),
    "谷歌": ("Google Chrome", "Chrome"),
    "chrome": ("Google Chrome", "Chrome"),
    "微软浏览器": ("Microsoft Edge", "Edge"),
    "edge": ("Microsoft Edge", "Edge"),
    "vscode": ("Visual Studio Code", "Code"),
    "vs code": ("Visual Studio Code", "Code"),
    "油管": ("YouTube",),
    "哔哩哔哩": ("哔哩哔哩", "bilibili"),
    "b站": ("哔哩哔哩", "bilibili"),
    "钉钉": ("钉钉", "DingTalk"),
    "飞书": ("飞书", "Feishu", "Lark"),
    "腾讯会议": ("腾讯会议", "Tencent Meeting", "VooV Meeting"),
    "任务管理器": ("任务管理器", "Task Manager"),
    "资源管理器": ("文件资源管理器", "File Explorer"),
    "文件管理器": ("文件资源管理器", "File Explorer"),
    "控制面板": ("控制面板", "Control Panel"),
    "设置": ("设置", "Settings"),
}

AUTO_SCORE = 0.9
"""得分达到它、并且和第二名拉开 MARGIN，才不用问。"""
ASK_SCORE = 0.6
"""低于它就当没找到。介于两者之间：问一句「是想打开 X 吗？」"""
MARGIN = 0.08

NAME_FLOOR = 0.6
"""按名字匹配时的及格线，低于它连候选都不算。

旧的写法是「分数 > 0 就收」，于是说「为信」会以 0.30 落到「微信」上
（两个字里恰有一个相同），说「完全不存在的软件」也会凑出几个 0.1x 的候选。
这些低分命中本身没有用，却会挤掉后面真正该出现的拼音/昵称候选。"""

CJK = r"\u4e00-\u9fff"
"""汉字区间。别写成 `[一-鿿]`——那个写法在源码里肉眼分辨不出边界，改一次错一次。"""

# 只留字母、数字、汉字，其余（空格、标点、emoji、零宽字符）全部丢掉。
# 实测教训：并入时用了白名单小得多的字符类，标点没被剥掉，
# 于是配置里写「my  app!」的用户永远拿不到自己的昵称——静默失效，最难查。
_COMPACT = re.compile(rf"[^{CJK}a-z0-9]+")

_CJK_RE = re.compile(rf"[{CJK}]")


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


def _compact(text: str) -> str:
    return _COMPACT.sub("", _clean(text))


def _pinyin_of(text: str) -> str:
    """整个名字的无声调全拼；英文/数字原样小写。没装 pypinyin 或没有汉字返回 ""。"""
    if not _CJK_RE.search(text):
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ""
    parts = lazy_pinyin(_clean(text), style=Style.NORMAL, errors="default")
    return "".join(p for p in parts if p.strip()).lower()


def _syllables(text: str) -> tuple[str, ...]:
    """**逐字**的音节：'微信' → ('wei', 'xin')。

    不能用 `lazy_pinyin` 的结果：它会把英文按字母拆（'Steam' → s,t,e,a,m），
    于是「微信」和「Steam」的音节数一样，个数比较就形同虚设。逐字取音节时
    英文和数字原样返回，恰好也是我们想要的。"""
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        return ()
    try:
        return tuple(
            (x[0] if x else "")
            for x in pinyin(_clean(text), style=Style.NORMAL, errors="default")
        )
    except Exception:  # noqa: BLE001 - 拼音库对古怪输入偶尔会抛，退回"没有拼音"
        return ()


FUZZY_SYLLABLE = 0.9
"""模糊拼音里**单个音节**的相似度下限。

不能只看整串相似度，它会掩盖短音节上的差别：'weixian' 与 'weixin' 整串是 0.923
（过了 0.92 的线），可 'xian'/'xin' 逐音节比出来只有 0.857——多出来的那个韵尾
恰恰说明这是另一个词（危险 ≠ 微信）。

0.9 这个数是从实测的两个样本上定的：
    'xin'/'xing' = 0.923  → 放行（ASR 把后鼻音听丢，这是最常见的听错）
    'xian'/'xin' = 0.857  → 挡住（多一个音）
两者中间就是分界线。"""


def _close_syllables(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """音节个数相同，且逐个都对得上。个数不同一律不算——多一个音就是另一个词。"""
    if not a or len(a) != len(b):
        return False
    return all(
        x == y or SequenceMatcher(None, x, y).ratio() >= FUZZY_SYLLABLE for x, y in zip(a, b)
    )


@dataclass(frozen=True)
class AppEntry:
    name: str
    """给用户看的名字。"""
    appid: str = ""
    """开始菜单的 AppID：exe 路径、`{GUID}\\...\\x.exe` 或 UWP 的 `包名!应用`。"""
    system: str = ""
    """系统命令名（notepad / calc …）。「记事本」这类自带应用靠它兜底。"""
    pinyin: str = ""

    @property
    def exe_path(self) -> str:
        """开始菜单里记的完整 exe 路径（不是 exe 路径就是空串）。

        **关闭应用要靠它，不能靠 exe 文件名。** 实测这台机器上三个完全不同的
        启动器都叫 `launcher.exe`：
            米哈游启动器  E:\\mihoyou\\miHoYo Launcher\\launcher.exe
            鸣潮          E:\\Wuthering Waves\\launcher.exe
            鹰角启动器    E:\\Hypergryph Launcher\\Launcher.exe
        拿 `taskkill /IM launcher.exe` 去关米哈游，会把鸣潮和鹰角一起关掉。
        """
        a = self.appid
        if a and not a.startswith("{") and a.lower().endswith(".exe"):
            return a
        return ""

    def resolved(self) -> Resolved:
        """变成 appfind 能启动的东西。"""
        if self.appid:
            if self.appid.lower().endswith(".exe") or Path(self.appid).is_file():
                return Resolved("exe", self.appid, f"开始菜单「{self.name}」", self.name)
            return Resolved("aumid", self.appid, f"开始菜单「{self.name}」", self.name)
        sa = appfind._system_alias(self.name)  # noqa: SLF001 - 同包内复用中文名 → 系统命令表
        if sa is not None:
            return Resolved(sa.kind, sa.value, sa.how, self.name)
        return Resolved("fail", "", f"找不到「{self.name}」")


@dataclass
class AppHit:
    entry: AppEntry
    score: float
    how: str
    """exact / name / nickname / pinyin / pinyin~ —— 事后解释"为什么命中它"。"""

    def describe(self) -> str:
        return f"{self.entry.name}（{self.score:.2f} {self.how}）"


class AppIndex:
    """已安装应用的可搜索索引。构建很便宜（几百项的名字和拼音），但依赖开始菜单列表，
    所以懒加载——并且 `invalidate()` 能让它在"用户刚装了新软件"时重建。"""

    def __init__(
        self,
        nicknames: dict[str, str | tuple[str, ...] | list[str]] | None = None,
        *,
        loader: Callable[[], list[tuple[str, str]]] | None = None,
        use_pinyin: bool = True,
        system_aliases: bool = True,
    ) -> None:
        self._loader = loader or appfind.start_apps
        self._entries: list[AppEntry] | None = None
        self.use_pinyin = use_pinyin
        self.system_aliases = system_aliases
        self._nick: dict[str, tuple[str, ...]] = {}
        for src in (BUILTIN_NICKNAMES, nicknames or {}):  # 用户的后写，覆盖内置
            for spoken, canon in src.items():
                vals = (canon,) if isinstance(canon, str) else tuple(canon)
                if _compact(spoken) and vals:
                    self._nick[_compact(spoken)] = vals

    # -- 构建 ------------------------------------------------------------- #

    def invalidate(self) -> None:
        self._entries = None

    def refresh(self) -> bool:
        """重新扫一遍开始菜单并重建索引。返回是否真的扫了。

        用户刚装好一个软件就来说「打开XX」时用它。**限频**（默认 30 秒）——
        每次扫描要起一个 PowerShell，一句口误不该换来一次全盘扫描。
        """
        if not appfind.refresh_start_apps_if_stale():
            return False
        self.invalidate()
        return True

    def entries(self) -> list[AppEntry]:
        if self._entries is None:
            self._entries = self._build()
        return self._entries

    def _build(self) -> list[AppEntry]:
        out: list[AppEntry] = []
        seen: dict[str, int] = {}
        system_cmds = dict(appfind._SYSTEM_ALIASES) if self.system_aliases else {}  # noqa: SLF001

        for name, appid in self._loader():
            key = _compact(name)
            if not key or key in seen:
                continue  # 开始菜单里同名的快捷方式常有好几份，留第一份
            seen[key] = len(out)
            out.append(AppEntry(name, appid, system_cmds.get(name, ""),
                                _pinyin_of(name) if self.use_pinyin else ""))

        for zh, cmd in system_cmds.items():
            key = _compact(zh)
            if key in seen:
                continue  # 开始菜单里已经有了（比如「计算器」「记事本」），不重复
            seen[key] = len(out)
            out.append(AppEntry(zh, "", cmd, _pinyin_of(zh) if self.use_pinyin else ""))
        return out

    # -- 搜索 ------------------------------------------------------------- #

    def search(self, query: str, limit: int = 3) -> list[AppHit]:
        q = _clean(query)
        if not _compact(q):
            return []
        entries = self.entries()
        best: dict[str, AppHit] = {}

        def offer(entry: AppEntry, score: float, how: str) -> None:
            cur = best.get(entry.name)
            if cur is None or score > cur.score:
                best[entry.name] = AppHit(entry, score, how)

        # 1) 昵称：说法和某个昵称一致 → 找那个昵称指向的、确实装了的应用
        canon = self._nick.get(_compact(q))
        if canon:
            for entry in entries:
                if any(strict_score(c, entry.name) >= 0.98 for c in canon):
                    offer(entry, 1.0, "nickname")

        # 2) 名字
        for entry in entries:
            s = strict_score(q, entry.name)
            if s >= NAME_FLOOR:
                offer(entry, s, "exact" if s >= 0.98 else "name")

        # 3) 拼音：说的是汉字，且全拼与某个应用名相同（威信 ↔ 微信）
        if self.use_pinyin and _CJK_RE.search(q):
            qp = _pinyin_of(q)
            if qp:
                qsyl: tuple[str, ...] | None = None  # 只在需要时才算（要起一次 pypinyin）
                for entry in entries:
                    if not entry.pinyin:
                        continue
                    if entry.pinyin == qp:
                        offer(entry, 0.93, "pinyin")
                        continue
                    # 模糊一档：ASR 常把韵母听错（xin ↔ xing）。两道闸门都要过：
                    # 整串够像，且**逐个音节**都对得上、个数也相同。只卡整串相似度
                    # 会让「危险 wei|xian」落到「微信 wei|xin」上。
                    if len(qp) < 6:
                        continue
                    if SequenceMatcher(None, qp, entry.pinyin).ratio() < 0.92:
                        continue
                    if qsyl is None:
                        qsyl = _syllables(q)
                    if _close_syllables(qsyl, _syllables(entry.name)):
                        offer(entry, 0.82, "pinyin~")

        hits = sorted(best.values(), key=lambda h: (-h.score, len(h.entry.name), h.entry.name))
        return hits[:limit]


def decide(
    hits: list[AppHit],
    *,
    auto: float = AUTO_SCORE,
    ask: float = ASK_SCORE,
    margin: float = MARGIN,
) -> str:
    """直接执行（auto）/ 问一句（ask）/ 当没找到（none）。"""
    if not hits or hits[0].score < ask:
        return "none"
    top = hits[0]
    if top.score >= auto and (len(hits) == 1 or top.score - hits[1].score >= margin):
        return "auto"
    return "ask"


def process_hints(entry: AppEntry) -> tuple[frozenset[str], str]:
    """从应用条目推出"它运行起来是哪个进程"：(可能的 exe 名集合, UWP 包族名)。

    关闭应用要靠它找窗口/进程。三种来源：
      * AppID 是 exe 路径（含 `{GUID}\\Tencent\\QQNT\\QQ.exe` 这种已知文件夹写法）→ 取文件名
      * AppID 是 `包名!应用`（UWP/MSIX）→ 包族名，进程要用 GetPackageFamilyName 去认
      * 系统自带应用的命令名（notepad → notepad.exe）
    """
    exes: set[str] = set()
    pfn = ""
    aid = entry.appid
    if aid:
        if aid.lower().endswith(".exe"):
            exes.add(PureWindowsPath(aid.replace("/", "\\")).name.lower())
        elif "!" in aid:
            pfn = aid.split("!", 1)[0]
    if entry.system:
        exes.add(entry.system.lower() if entry.system.lower().endswith(".exe") else entry.system.lower() + ".exe")
    return frozenset(exes), pfn


def names_for_display(hits: Iterable[AppHit]) -> list[str]:
    return [h.entry.name for h in hits]


__all__ = [
    "ASK_SCORE",
    "AUTO_SCORE",
    "BUILTIN_NICKNAMES",
    "AppEntry",
    "AppHit",
    "AppIndex",
    "decide",
    "names_for_display",
    "process_hints",
]
