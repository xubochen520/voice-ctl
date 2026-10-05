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
    """去掉首尾的标点和空白。

    ASCII 的 `,` `;` `:` 必须在内。归一化器**不会**动它们（它只收拾中文标点），
    而 Windows 的语音输入和不少 ASR 输出给的就是半角逗号——实测
    「打开百度网盘,呸」里那个逗号原来剥不掉，于是 `endswith("呸")` 之类按
    整串做的判断全部落空。
    """
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


# --------------------------------------------------------------------------- #
# 自我更正 / 撤回
# --------------------------------------------------------------------------- #

CORRECTION_MARKERS = (
    "呸呸呸", "我呸", "呸", "说错了", "我说错了", "说错", "讲错了", "口误",
    "不对不对", "不对", "重说", "重新说", "当我没说",
)
"""用户说到一半自己撤回时的那几个词。

「打开百度网盘，呸」是用户亲口给的用例：这里「呸」不是要打开的什么东西，
而是**作废整句**。没有这一层，那句话会照常去开百度网盘——用户刚说完"不要"，
程序却执行了，这是最伤信任的一类错误。

分两个方向用（`detect_correction`）：
  * 结尾出现 → 整句作废
  * 中间出现 → 丢掉它前面的部分，用后面的（「打开百度，不对，打开淘宝」）

**「算了」「取消」不在表里**，这是量过的：「打开微信，算了」的切分结果是
`['打开微信', '算了']`，段数 > 1 而「不对」恰好不是最后一段的首词，于是整句
被误作废——用户明明说了要开微信。这两个词在 `NO_WORDS` 里是对的（它们是对
确认卡的回答），但当**整句的尾巴**讲没有这个意思。宁可少收回一次，
也不要把正常指令吃掉。
"""

_CLAUSE_SEP = re.compile(r"[,，。.．;；:：!！?？~～、\s]+")
r"""切停顿用的分隔符，写成一个显式字符类。

**不要写成 `[,，。;；!！?？\s]+` 这种区间形式**：汉字在 Unicode 里是连续的，
`；`(U+FF1B) 到 `？`(U+FF1F) 之间就夹着 `＜＝＞` 和一个变体选择符，`!`(U+0021)
到 `！`(U+FF01) 之间更是横跨几千个码位——实测那样写会把几乎所有汉字都当成
分隔符，「打开微信，算了」被切成 6 段。
"""


def _clauses(text: str) -> list[str]:
    """按停顿把一句话切成小段。

    只在**明确的停顿标点**上切。空格也算——实测「打开百度网盘 呸」里那个空格
    就是用户用来分开「呸」的，而「打开 / 百度 / 网盘」这种把名字切碎的情况
    不影响结果（切碎之后拿去查站点表查不到，就是查不到）。
    """
    return [c for c in _CLAUSE_SEP.split(strip_punct(text)) if c]


def _starts_command(text: str) -> bool:
    """这段文字看起来是不是一条新的命令（句首是开关动词）。"""
    return any(text.startswith(v) for v in OPEN_VERBS + CLOSE_VERBS + FORCE_VERBS if len(v) > 1)


def detect_correction(text: str) -> tuple[str, bool]:
    """看这句话有没有被用户自己撤回。返回 (可用文本, 是否整句作废)。

    从**最后一个**标记开始试着套两条规则：

      A. 标记后面还有一条新命令（后面那段以开关动词开头，或者标记本身就占了一整
         段）→ 用户改口了，丢掉标记及其之前的部分，用后面那条命令。
         「打开百度，不对，打开淘宝」→「打开淘宝」
         「打开百度不对打开淘宝」→「打开淘宝」（ASR 常常不给逗号，所以不能只看标点）
      B. 标记在结尾、后面什么都没有 → 整句作废。
         「打开百度网盘，呸」→ 不执行

    两条都不成立就原样返回。这是刻意的保守：**宁可漏判成没撤回，也不要误伤**。
    实测「提醒我不对账」里就夹着一个「不对」，它后面既没有新命令、也不在结尾，
    所以整句照常处理——把这种句子作废掉，用户会觉得程序在乱猜。
    """
    s = strip_punct(text)
    if not s:
        return s, False

    # 在**整句**里从右往左找标记，不是只在最后一段里找：「打开百度，不对，打开淘宝」
    # 的标记在中间那一段，只扫最后一段就会把它漏掉。
    hits: list[tuple[int, str]] = []
    for m in CORRECTION_MARKERS:
        start = 0
        while (i := s.find(m, start)) >= 0:
            hits.append((i, m))
            start = i + 1
    # 位置靠右的优先；同一位置取最长的标记（「说错了」要赢过「说错」）
    hits.sort(key=lambda t: (t[0], len(t[1])), reverse=True)

    for i, m in hits:
        after = strip_punct(strip_trailing_glue(s[i + len(m):]))
        before = strip_punct(s[:i])
        at_clause_start = not before or _CLAUSE_SEP.fullmatch(before[-1]) is not None
        if after:
            # 改口要成立，后面必须**真的跟着一条新命令**：以开关动词开头
            # （「打开百度不对打开淘宝」），或者标记本身独占一段
            # （「打开百度，不对，打开淘宝」）。
            #
            # 只要求"后面还有字"是不够的——实测「说错了」会被拆成「说错」+残余的
            # 「了」，那个「了」不是命令，却让整句变成一条叫「了」的新命令。
            if _starts_command(after) or (at_clause_start and len(after) >= 2):
                return after, False
            continue
        if before:
            # 标记在结尾：整句作废。用户刚说过"呸/不对"，不该再执行它。
            return "", True
    # 整句就是一个标记（「呸」「不对」「说错了」）。这是用户在被听错之后最常见的
    # 反应——上一次听岔了，于是这次只丢一个字出来。它必须走"作废"这条路，否则会
    # 被当成一句看不懂的话，报告成「别名没命中」——那等于没接住用户的意图。
    #
    # 先剥尾巴再比、也比不剥的那份：「说错了」的尾巴「了」在 TRAILING_GLUE 里，
    # 剥完只剩「说错」，而表里两条都写着，两种写法都要认。
    if s in CORRECTION_MARKERS or strip_punct(strip_trailing_glue(s)) in CORRECTION_MARKERS:
        return "", True
    return s, False


# --------------------------------------------------------------------------- #
# 网页类线索词
# --------------------------------------------------------------------------- #

WEB_CATEGORY_WORDS = ("网页", "官网", "网站", "网址", "主页", "首页", "页面")
"""「打开百度**网页**」里的类别词。

它说明用户要的是**网站**而不是本地程序。剥掉它之后剩下的「百度」才是名字——
不剥的话会拿「百度网页」当应用名去查索引，什么也查不到（实测就是这样），
然后告诉用户"没找到叫「百度网页」的应用"，而用户明明只是想开个网页。

「网」一个字不算：它是「淘宝网」「新浪网」「百度网盘」这些**名字本身**的一部分，
剥掉就把名字弄坏了。
"""


def strip_web_cue(text: str) -> tuple[str, bool]:
    """剥掉句尾的网页类别词。返回 (剩下的名字, 句尾有没有出现过类别词)。"""
    s = strip_punct(text)
    for w in sorted(WEB_CATEGORY_WORDS, key=len, reverse=True):
        if s.endswith(w) and len(s) > len(w):
            rest = strip_punct(s[: -len(w)])
            if rest:
                return rest, True
    return s, False


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
    "CORRECTION_MARKERS",
    "FORCE_VERBS",
    "NEGATIONS",
    "NO_WORDS",
    "OPEN_VERBS",
    "WEB_CATEGORY_WORDS",
    "YES_WORDS",
    "ba_construction",
    "detect_correction",
    "is_negated",
    "leading_verb",
    "parse_yes_no",
    "strip_leading_glue",
    "strip_punct",
    "strip_trailing_glue",
    "strip_web_cue",
    "verb_kind",
]
