"""口令词表：动词、否定词、客套话、确认/否认。

别名匹配只看"话里有没有出现某个名字"，**对动词和否定完全无感**——实测
「关闭微信」「把微信关掉」「不要打开记事本」都会命中「打开」动作。这里的词表就是
给意图层用的：先认出动词，再决定这句话是不是真的在要求"打开"。

只放**多字词**能判定的东西，单字动词（关、开、去、上）只在句首、并且后面确实跟着
一个能解析成应用的名字时才算数——否则「关于」「关机」「上班」都会被当成动词。
"""

from __future__ import annotations

import re

OPEN_VERBS = (
    "打开", "开启", "启动", "运行", "开一下", "开个", "进入", "切换到", "切换至", "切到",
    "跳到", "去", "上", "开",
)
CLOSE_VERBS = ("关闭", "关掉", "关了", "关上", "退出", "结束", "停止", "终止", "关")
FORCE_VERBS = (
    "强制关闭", "强制关掉", "强制退出", "强制结束", "强行关闭", "强行关掉", "强行结束", "强行退出",
    "结束进程", "杀掉", "杀死", "干掉",
)
"""强杀类动词。

「强制关掉」「强行关掉」必须各自列出来，不能让「强制」当通用前缀去拼——实测
只写「强制关闭」时，说「强制关掉微信」会被判成普通 close（因为「关掉」在
_CLOSE_ANYWHERE 里），而 `strip_verb` 又剥不掉句首的「强制」，整串拿去当应用名
查不到，于是整句返回 None、掉回对动词无感的别名匹配——很可能反而把微信**打开**。"""
NEGATIONS = ("不要", "别", "不用", "不想", "不必", "不需要", "无需", "不许", "不能")

# 多字的关闭词：出现在句中任意位置都足以说明"这句话是在要求关"。单字「关」不算。
_CLOSE_ANYWHERE = ("关闭", "关掉", "关了", "关上", "退出", "结束", "停止", "终止")

LEADING_GLUE = (
    "我要你", "我想要", "我不想", "我不要", "麻烦你", "能不能", "可不可以", "帮我把", "帮我", "帮忙",
    "替我", "给我", "请你", "请", "麻烦", "我要", "我想", "我得", "可以", "你", "呃", "嗯", "那个", "就",
    "再", "然后", "先",
)
"""句首的客套话/口头禅。`strip_leading_glue` 会反复剥，直到剥不动。

「我不想」「我不要」在这里不是为了剥掉好看，而是因为**否定判定必须在剥掉代词之后**：
`is_negated` 只认句首，而「我」会挡住「不想」——实测「我不想打开计算器」
就这样绕过否定检查、命中「打开计算器」，然后真的把计算器打开了。
把「我不想」当成一层客套话剥掉，剩下的句首就是「打开计算器」……所以要
**在剥之前**判否定，见 `is_negated` 的实现。"""

TRAILING_GLUE = ("一下子", "一下", "好不好", "好吗", "好么", "谢谢", "吧", "啊", "呀", "呢", "哦", "嘛", "了")

_PUNCT = " \t\r\n,，。.!！?？、;；:：~～…\"'“”‘’「」『』()（）"


def strip_punct(text: str) -> str:
    return text.strip(_PUNCT)


def strip_leading_glue(text: str, glue: tuple[str, ...] = LEADING_GLUE) -> str:
    """反复剥掉开头的客套话（「请帮我」→ 两个都剥掉）。剥到只剩空就不剥了。"""
    s = strip_punct(text)
    changed = True
    while changed and s:
        changed = False
        for g in sorted(glue, key=len, reverse=True):
            if s.startswith(g) and len(s) > len(g):
                s = strip_punct(s[len(g):])
                changed = True
                break
    return s


def strip_trailing_glue(text: str, glue: tuple[str, ...] = TRAILING_GLUE) -> str:
    s = strip_punct(text)
    changed = True
    while changed and s:
        changed = False
        for g in sorted(glue, key=len, reverse=True):
            if s.endswith(g) and len(s) > len(g):
                s = strip_punct(s[: -len(g)])
                changed = True
                break
    return s


def leading_verb(text: str, verbs: tuple[str, ...]) -> tuple[str, str] | None:
    """句首是不是这些动词之一。返回 (动词, 剩下的)；动词取最长的那个匹配。"""
    s = strip_leading_glue(text)
    for v in sorted(verbs, key=len, reverse=True):
        if s.startswith(v) and len(s) > len(v):
            return v, strip_punct(s[len(v):])
    return None


_BA_RE = re.compile(r"^把(?P<obj>.+?)(?P<verb>打开|开启|启动|运行|关闭|关掉|关了|关上|退出|结束)$")


def ba_construction(text: str) -> tuple[str, str] | None:
    """「把微信关掉」→ ('关掉', '微信')。"""
    m = _BA_RE.match(strip_trailing_glue(strip_leading_glue(text)))
    if not m:
        return None
    return m["verb"], strip_punct(m["obj"])


_PRONOUN_HEADS = ("我", "你", "您", "他", "她", "它", "咱", "咱们", "我们", "你们")


def is_negated(text: str) -> bool:
    """句首（客套话之后）就是否定词：「不要打开记事本」「别关了」。

    代词要算进去。实测「我不想打开计算器」——否定词是「不想」，它前面挡着一个
    「我」。只看第一个词就会漏掉，于是这句话被判成"要打开"，真的把计算器打开了。

    **判否定的时机在剥客套话之前**，这一点反直觉但必须如此：
    `strip_leading_glue` 的表里「我不想」本身就是一条客套话，先剥的话
    「我不想打开计算器」会变成「打开计算器」——否定词被剥掉了，越剥越像肯定句。

    所以顺序是：先按原样看句首 → 再剥客套话看句首 → 再按「代词 + 否定词」组合看。
    三条都看，任何一条成立就算否定。

    只看句首。「我不想让它关机」这种句中的否定交给后面的意图判断——
    宁可漏判也不要误伤：「别出声」是个静音动作，不是否定句。
    """
    raw = strip_punct(text).lower()
    s = strip_leading_glue(text).lower()
    candidates = (raw, s, strip_leading_glue(s).lower())
    if any(c.startswith(n) for n in NEGATIONS for c in candidates):
        return True
    # 代词 + 否定词：否定词不是句首而是第二个词（「我不想…」）
    return any(
        c.startswith(p) and c[len(p):].startswith(n)
        for c in candidates
        for p in _PRONOUN_HEADS
        for n in NEGATIONS
    )


def verb_kind(text: str) -> str | None:
    """这句话想让某个应用「开」还是「关」：force / close / open / negate / None。

    给别名匹配做极性检查用：配置里的「打开微信」动作，不该被「关闭微信」触发。
    """
    s = strip_leading_glue(text)
    if is_negated(text):
        return "negate"
    if any(v in s for v in FORCE_VERBS):
        return "force"
    if any(s.startswith(v) for v in CLOSE_VERBS) or any(v in s for v in _CLOSE_ANYWHERE):
        return "close"
    if ba := ba_construction(text):
        return "close" if ba[0] in CLOSE_VERBS or ba[0] in _CLOSE_ANYWHERE else "open"
    if any(s.startswith(v) for v in OPEN_VERBS if len(v) > 1):
        return "open"
    return None


# --------------------------------------------------------------------------- #
# 确认 / 否认（对确认卡的语音回答）
# --------------------------------------------------------------------------- #

YES_WORDS = frozenset({
    "确认", "确定", "好", "好的", "好吧", "行", "可以", "对", "对的", "是", "是的", "没错", "嗯", "ok",
    "okay", "同意", "执行", "就这样", "没问题", "可以的", "确认执行", "是啊", "没错的", "对啊", "yes",
})
NO_WORDS = frozenset({
    "取消", "不要", "不用", "算了", "不对", "错了", "不是", "否", "别", "停", "不", "拒绝", "不行",
    "撤销", "放弃", "不要了", "取消吧", "不用了", "不对的", "no", "别执行",
})


def parse_yes_no(text: str) -> bool | None:
    """对一张确认卡的语音回答。只认**很短的整句**——「好的我想打开微信」是新命令，
    不是对确认卡的回答，否则会把正常指令吃掉。"""
    s = strip_punct(text).lower()
    if not s or len(s) > 8:
        return None
    s2 = strip_trailing_glue(s) or s
    if s in YES_WORDS or s2 in YES_WORDS:
        return True
    if s in NO_WORDS or s2 in NO_WORDS:
        return False
    return None


__all__ = [
    "CLOSE_VERBS",
    "FORCE_VERBS",
    "NEGATIONS",
    "NO_WORDS",
    "OPEN_VERBS",
    "YES_WORDS",
    "ba_construction",
    "is_negated",
    "leading_verb",
    "parse_yes_no",
    "strip_leading_glue",
    "strip_punct",
    "strip_trailing_glue",
    "verb_kind",
]
