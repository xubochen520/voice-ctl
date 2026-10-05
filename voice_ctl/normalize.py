"""语音文本归一化：口语词剥离 + 常见同音误识别纠正。

为什么需要这一层（实测依据）：
    SenseVoice **不支持热词偏置**——sherpa-onnx 在 C++ 层直接 abort 进程
    （"Only transducer models support contextual biasing."）。它也没有别的
    命令词注入接口。所以「把识别结果往我的命令词上拉」这件事只能自己做。

两层手段：
    1. 口语词剥离 + 空白/大小写归一（确定性，必定生效）
    2. 同音字替换（表驱动，可持续补充）

拼音层（pypinyin）是可选的：装了就用它做同音判定，没装就退化为
「子串包含 + 替换表」，功能仍可用但不那么宽容。见 fuzzy_key()。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 常见中文 ASR 同音/近音误识别。
# 说明：这些不是猜的——是根据 SenseVoice 在中文命令场景的实际易错模式整理的
# 种子表。发现新的误识别，直接往这里加；也可以在 config.toml 的
# [normalize].substitutions 里覆盖/追加，不用改代码。
DEFAULT_SUBSTITUTIONS: dict[str, str] = {
    # 微信
    "威信": "微信",
    "薇信": "微信",
    "为信": "微信",
    "微小": "微信",
    # 常用应用
    "记事簿": "记事本",
    "计算汽": "计算器",
    "计算起": "计算器",
    "浏览器起": "浏览器",
    "哔哩哔哩": "bilibili",
    "比利比利": "bilibili",
    "必利必利": "bilibili",
    "b站": "bilibili",
    # 系统操作
    "截频": "截屏",
    "截平": "截屏",
    "锁频": "锁屏",
    "锁平": "锁屏",
    "音量家": "音量加",
    "音量建": "音量减",
    "静音键": "静音",
    "任务管理汽": "任务管理器",
    "资源管理汽": "资源管理器",
}

_PUNCT_RE = re.compile(r"[\s,，。.!！?？、;；:：'\"“”‘’()（）\[\]【】~～\-—_]+")
# 只保留 ASCII 字母数字——给别名做最后兜底比较用。
# 注意不能用 [^0-9a-z...] 的排除式写法：排除式留着汉字，而本函数的用途正是
# 把「打开 WeChat」和「WeChat」归一成同一个东西。
_SLUG_KEEP = re.compile(r"[^0-9a-z]+")

# 下划线和点只当分隔符用：动作 id "sys.show_desktop" 的末段
# "show_desktop" 应当和用户说的 "显示桌面"/"showdesktop" 都能对上，
# 所以比较时把 _ 换成 . ，让 canonical() 的 _PUNCT_RE 一起清掉。
_ID_SEP_RE = re.compile(r"[_.]+")


@dataclass
class NormalizeConfig:
    strip_prefixes: list[str] = field(default_factory=list)
    strip_suffixes: list[str] = field(default_factory=list)
    substitutions: dict[str, str] = field(default_factory=dict)
    """覆盖/追加的同音替换表；与 DEFAULT_SUBSTITUTIONS 合并，此处优先。"""

    inline_fillers: list[str] = field(
        default_factory=lambda: ["一下", "一个", "那个", "这个"]
    )
    """句中口语填充词，任意位置都删掉。

    为什么要单独一组：「打开一下微信」里的「一下」在句中，首尾剥离管不到。
    这组词必须挑**不会出现在正式命令词里**的，否则会把命令本身吃掉
    （比如「打开」就不能放进来，否则「打开微信」直接没了）。
    """

    use_pinyin: bool = True
    """是否尝试用 pypinyin 做同音判定（未安装则自动跳过）。"""


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def canonical(text: str) -> str:
    """匹配用的规范形式：NFKC + 小写 + 去掉所有空白与标点。

    中文识别结果里标点位置很不稳定（"打开微信。" / "打开微信" / "打开 微信"），
    所以匹配前一律清掉。这个函数是纯函数，方便测试。
    """
    s = _nfc(text).lower()
    s = _PUNCT_RE.sub("", s)
    return s.strip()


def fuzzy_key(text: str) -> str:
    """模糊键：取每个汉字的拼音首字母，非汉字原样保留（小写）。

    装了 pypinyin 才有效；没装就返回 canonical(text)，即退化为精确匹配。
    这样调用方不需要分支。
    """
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return canonical(text)

    s = canonical(text)
    if not s:
        return ""
    out: list[str] = []
    for ch in s:
        if "\u4e00" <= ch <= "\u9fff":
            py = lazy_pinyin(ch, style=Style.FIRST_LETTER, errors="ignore")
            out.append(py[0].lower() if py else ch)
        else:
            out.append(ch)
    return "".join(out)


class Normalizer:
    """把 ASR 原始文本变成更贴近命令词的形态。"""

    def __init__(self, cfg: NormalizeConfig | None = None) -> None:
        cfg = cfg or NormalizeConfig()
        self.cfg = cfg
        subs = dict(DEFAULT_SUBSTITUTIONS)
        subs.update({k.lower(): v for k, v in cfg.substitutions.items()})
        # 长键优先替换，避免 "微信" 先被 "微" 之类的短键吃掉
        self._subs = sorted(subs.items(), key=lambda kv: -len(kv[0]))
        self._pre = sorted((p.lower() for p in cfg.strip_prefixes), key=len, reverse=True)
        self._suf = sorted((s.lower() for s in cfg.strip_suffixes), key=len, reverse=True)
        self._inline = sorted((s.lower() for s in cfg.inline_fillers if s), key=len, reverse=True)

    # -- 各步 ------------------------------------------------------------- #

    def apply_substitutions(self, text: str) -> str:
        """按表替换同音误识别。"""
        out = text
        for wrong, right in self._subs:
            if wrong and wrong in out:
                out = out.replace(wrong, right)
        return out

    def strip_fillers(self, text: str) -> str:
        """剥掉开头/结尾的口语词。可反复剥离（"请帮我打开" → "打开"）。"""
        s = text.strip()
        changed = True
        while changed and s:
            changed = False
            for p in self._pre:
                if s.startswith(p) and len(s) > len(p):
                    s = s[len(p):].strip()
                    changed = True
                    break
            for q in self._suf:
                if s.endswith(q) and len(s) > len(q):
                    s = s[: -len(q)].strip()
                    changed = True
                    break
        return s

    def strip_inline_fillers(self, text: str) -> str:
        """删掉句中填充词。删完若空则保留原文，避免把整句吃光。"""
        s = text
        for f in self._inline:
            if f and f in s:
                candidate = s.replace(f, "")
                if candidate.strip():
                    s = candidate
        return s.strip()

    # -- 主入口 ----------------------------------------------------------- #

    def normalize(self, text: str) -> str:
        """返回归一化后的**可读**文本（保留汉字与大小写，只修正误识别和口语词）。"""
        s = _nfc(text).strip()
        s = self.apply_substitutions(s)
        s = self.strip_fillers(s)
        s = self.strip_inline_fillers(s)
        return re.sub(r"\s+", " ", s).strip()

    def keys(self, text: str) -> list[str]:
        """返回用于匹配的候选键：规范形式 + 模糊键（去重，保序）。

        两个键都拿去做别名匹配，命中任意一个即算命中——这样
        「微信」和「威信」（若表里没收录）都能靠拼音首字母 wx 命中 wx 别名。
        """
        n = self.normalize(text)
        ks = [canonical(n)]
        if self.cfg.use_pinyin:
            fk = fuzzy_key(n)
            if fk and fk not in ks:
                ks.append(fk)
        return [k for k in ks if k]


def ascii_slug(text: str) -> str:
    """把任意文本压成纯 [0-9a-z] 串，用于别名兜底比较。

    「打开 WeChat 2」和「WeChat2」都应得到 'wechat2'——汉字被丢弃，
    因为本函数的用途就是让中英混说的口令能和英文别名对上。
    """
    return _SLUG_KEEP.sub("", canonical(text))
