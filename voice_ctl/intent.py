"""意图层：把**一整句话**读成「要做什么 + 对谁做 + 什么时候」。

别名匹配的模型是「话里出现了某个别名就执行那个动作」。这对「打开微信」很好，
对下面这些真实说法就是错的——全部是实测出来的：

  说「设置今天下午三点的日程我要玩游戏」
      别名「设置」被命中（0.912），于是打开 Windows 设置。用户要的是日程。
      别名是**句子里的一部分**，动词「设置」的宾语是「日程」，不是别名本身。
  说「关闭微信」
      别名「微信」被命中（0.950），于是**打开**微信。动词和否定被完全忽略。
  说「打开QQ」
      配置里没有 QQ 这条动作，于是什么都匹配不上——尽管开始菜单里就有 QQ。
      想要「打开任何一个装了的软件」都得手写一段 [[action]]。

这一层做的事，就是把「哪几个字是名字」之外的信息也用上：

  1. **动词**：打开 / 关闭 / 设置…（词表在 lexicon.py，可单测）
  2. **否定**：「不要打开记事本」不该打开记事本
  3. **时间**：「今天下午三点」是参数，不是名字的一部分（timeparse.py）
  4. **对象**：名字到应用的解析不只查配置，还查**已安装应用索引**（apps.py）

三个刻意的决定：

  原文优先。`Intent.source` 保存 ASR 原文。归一化会把「一个/那个/一下」删掉、
  把同音字换掉——那对匹配是好事，对日程标题是灾难（「我要玩游戏」里的字不能被改）。
  只有必须做字面比较的地方（找动词、找名字）才用归一化后的那份。

  拿不准就问，不猜。「下午三点」没说上午还是下午、两个应用得分几乎一样——
  这些都不替用户决定，交给确认卡。`Intent.question` 就是那句话。

  解析不出对象不算失败。返回 None，下游还有别名匹配兜着。
  这一层是**加法**：它只负责吃掉别名匹配搞不定的句子，不吃掉它搞得定的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import lexicon
from .apps import AppHit, AppIndex, decide
from .normalize import Normalizer
from .timeparse import TimeSpan, parse_when, strip_spans

# --------------------------------------------------------------------------- #
# 意图
# --------------------------------------------------------------------------- #

APP = "app"
"""开/关一个应用。"""
SCHEDULE = "schedule"
"""建一条日程/提醒。"""
QUESTION = "question"
"""想在多个候选里问用户一句，但还缺信息（比如没说出具体时间）。"""


@dataclass(frozen=True)
class ResolvedApp:
    """一个解析出来的应用：既能启动，也能被关闭。"""

    name: str
    """显示名（「微信」「Visual Studio Code」）。"""
    action_id: str | None = None
    """配置里已有的动作 id。有它就照常走动作表，行为跟以前完全一致。"""
    appid: str = ""
    """开始菜单的 AppID（exe 路径 / UWP 的 `包名!应用`）。"""
    system: str = ""
    """系统命令名（notepad / calc…）。"""
    score: float = 1.0
    how: str = "action"
    """action / exact / name / nickname / pinyin… —— 事后解释"为什么是它"。"""


@dataclass
class Intent:
    kind: str
    polarity: str
    """open / close / force / schedule / negate。"""
    verb: str = ""
    app: ResolvedApp | None = None
    when: TimeSpan | None = None
    title: str = ""
    source: str = ""
    """ASR 原文，一个字都没改。日程标题、日志都用它。"""
    payload: str = ""
    """剥掉动词、时间、客套话之后剩下的正文。日程标题从它里面提炼。"""
    question: str = ""
    """要问用户的那句话；空串表示不用问。"""
    candidates: list[AppHit] = field(default_factory=list)
    why: str = ""
    """一句话说明这个判断是怎么来的，给 `simulate --dry-run` 和日志看。"""

    @property
    def needs_confirm(self) -> bool:
        return bool(self.question)


# --------------------------------------------------------------------------- #
# 日程意图的线索词
# --------------------------------------------------------------------------- #

SCHEDULE_NOUNS = ("日程", "提醒", "闹钟", "待办", "事项", "备忘录", "计时器")
"""出现这些词，基本可以确定是在建日程。"""

SCHEDULE_VERBS = (
    "提醒我", "叫我", "喊我", "通知我", "记得", "备忘", "安排", "预约", "定个", "订个",
    "加个", "添加", "记一下", "记下", "计时", "倒计时",
)
"""没有「日程」这类名词，但动词本身就说明了意图。"""

_QUOTES = re.compile(r"[「『\"“']([^」』\"”']{1,40})[」』\"”']")


def _has_schedule_cue(text: str, when: TimeSpan | None) -> bool:
    """这句话是不是在建日程。

    三条线索，任意一条成立即可。刻意**不**把「解析出了时间」单独当线索——
    timeparse 连「今天几号」里的「今天」都会解析成日期，那样什么话都成了日程。
    """
    if any(n in text for n in SCHEDULE_NOUNS):
        return True
    if any(v in text for v in SCHEDULE_VERBS):
        return True
    # 「X点干什么」：有钟点、而且后面还跟着正文，才像日程。否则「今天天气」也会中招。
    if when is not None and when.has_clock and when.spans:
        rest = strip_spans(text, when.spans)
        return len(rest) >= 2
    return False


# --------------------------------------------------------------------------- #
# 标题提炼
# --------------------------------------------------------------------------- #

_TITLE_PREFIXES = (
    "帮我设置一个", "帮我设置", "帮我把", "安排一个", "安排", "添加一个", "添加", "新建一个", "新建",
    "设置一个", "设置", "创建一个", "创建", "定一个", "定个", "订一个", "订个", "加一个", "加个",
    "记一下", "记下", "提醒我", "叫我", "喊我", "通知我", "提醒", "预约", "备忘",
)

_TITLE_GLUE = (
    "的日程", "的提醒", "的闹钟", "的待办", "的备忘", "日程", "提醒", "闹钟", "待办", "备忘",
    "内容", "标题", "事情", "事儿",
)

_TITLE_LEAD = (
    "帮我", "麻烦", "请", "我得", "是", "在", "到", "有", "个", "一个", "的",
    "一下", "然后", "就", "还", "再", "把", "给",
)

_MEANINGLESS_TITLE = frozenset({
    "的", "了", "个", "一个", "是", "在", "要", "要的", "的的", "我", "你", "他", "她", "它",
    "我们", "你们", "咱们", "一下", "这个", "那个", "什么", "点", "些",
    "日程", "提醒", "闹钟", "待办", "备忘", "事情", "事儿", "内容", "标题", "名字",
})
"""剥完壳只剩这些字，说明用户压根没说标题。

「日程/提醒」这类**名词本身**也算：说「设置日程」时剩下的是「日程」，
它不是标题，而是用户用来指明"我要建日程"的那个词。拿它当日程名，
列表里就会出现一条叫「日程」的日程——用户完全认不出那是哪一条。
这种时候退回默认标题（时间本身），至少能对上号。"""

_CLAUSE_START = ("我", "你", "他", "她", "它", "咱们", "我们", "你们")
"""这些字一出现，就说明**用户自己的话**开始了，壳子到此为止。

实测：「设置今天下午三点的日程我要玩游戏」剥掉「设置」「的日程」之后是「我要玩游戏」。
再顺手把「我要」当客套话剥掉，标题就只剩「玩游戏」——用户的原话被改短了。
日程标题必须逐字保留用户的说法，所以遇到「我」就停手。"""


def extract_title(payload: str, *, when: TimeSpan | None = None, fallback: str = "") -> str:
    """从正文里提炼日程标题。

    输入是**剥掉时间表达式之后**的正文（`strip_spans` 的产物），所以这里只需要
    处理「设置…的日程」这类壳子。三种情况实测都会遇到：

        「设置今天下午三点的日程我要玩游戏」 → 正文「设置的日程我要玩游戏」→「我要玩游戏」
        引号里的优先：「设置日程『我要玩游戏』」 → 「我要玩游戏」
        「内容是X」：「设置一个日程，内容是玩游戏」 → 「玩游戏」

    实在没有线索时，返回 `fallback`（通常是时间本身，比如「下午三点的提醒」）——
    空标题的日程在列表里没法认。
    """
    text = lexicon.strip_punct(payload)

    # 1) 引号里的是用户明确划出来的标题，优先
    if m := _QUOTES.search(text):
        return lexicon.strip_punct(m[1])

    # 2) 「内容是X」「标题是X」
    if m := re.search(r"(?:内容|标题|名字)\s*(?:是|叫|为)\s*(.+)", text):
        cand = lexicon.strip_punct(m[1])
        if cand:
            return cand

    # 3) 剥壳：前缀（设置/安排…）+ 中间的「的日程」
    s = text
    for _ in range(3):  # 反复剥：「帮我设置一个…」要剥掉两层
        if s.startswith(_CLAUSE_START):
            break  # 用户的话开始了，壳子剥到这里为止
        before = s
        for p in sorted(_TITLE_PREFIXES, key=len, reverse=True):
            if s.startswith(p) and len(s) > len(p):
                s = lexicon.strip_punct(s[len(p):])
                break
        for g in sorted(_TITLE_GLUE, key=len, reverse=True):
            if g in s and len(s) > len(g):
                s = lexicon.strip_punct(s.replace(g, "", 1))
                break
        if s == before:
            break

    s = lexicon.strip_leading_glue(s, _TITLE_LEAD)
    s = lexicon.strip_trailing_glue(s, ("吧", "啊", "呀", "哦", "嘛", "谢谢"))
    if s and s not in _MEANINGLESS_TITLE:
        return s
    return fallback


def _default_title(when: TimeSpan | None) -> str:
    from .timeparse import format_when

    if when is None:
        return "提醒"
    return f"{format_when(when.start)}的提醒"


# --------------------------------------------------------------------------- #
# 对象解析
# --------------------------------------------------------------------------- #

_LEADING_VERB_WORDS = tuple(
    sorted(
        set(lexicon.OPEN_VERBS) | set(lexicon.CLOSE_VERBS) | set(lexicon.FORCE_VERBS),
        key=len, reverse=True,
    )
)
"""要剥掉的句首动词，**三个词表都要**。

漏掉 FORCE_VERBS 的表现（实测）：`strip_verb("强制关闭微信")` 原样返回整句，
`_closable_target` 拿「强制关闭微信」当应用名去查索引，查不到，于是整句
`interpret` 返回 None，掉到下游别名匹配——而别名匹配对动词无感，
「强制关闭微信」会被判成**打开**微信。"""


def strip_verb(text: str) -> str:
    """剥掉句首的开关动词和客套话。

    注意「一下」这类口语填充词**不在这里剥**——那是 Normalizer 的活
    （`inline_fillers`）。所以单独调它时 `"打开一下微信"` 得到 `"一下微信"`；
    走完整的 `interpret` 流水线时输入已经归一化过，拿到的就是 `"微信"`。
    这一层只负责动词，两边各管一件事。
    """
    s = lexicon.strip_leading_glue(text)
    for _ in range(3):  # 「强制关闭」要剥两层（强制 + 关闭）
        changed = False
        for v in _LEADING_VERB_WORDS:
            if s.startswith(v) and len(s) > len(v):
                s = lexicon.strip_punct(s[len(v):])
                changed = True
                break
        if not changed:
            break
    return lexicon.strip_trailing_glue(s) or s


def resolve_app(
    name: str,
    *,
    index: AppIndex | None,
    actions: list | None = None,
    normalizer: Normalizer | None = None,
) -> tuple[ResolvedApp | None, list[AppHit]]:
    """把一个名字解析成应用。返回 (解析结果, 候选列表)。

    解析顺序刻意如此：

      1. **配置里的动作**（精确/包含/模糊）——用户亲手写的别名优先级最高，
         而且命中它就走原来的动作表，行为和以前一模一样。
      2. **已安装应用索引**——「打开QQ」能用的关键：开始菜单里就有的名字，
         不需要用户先在 config.toml 里写一段。
      3. 都没有 → (None, 候选)，交给调用方决定是问一句还是当没找到。
    """
    q = (normalizer or Normalizer()).normalize(name) if normalizer else name
    hits: list[AppHit] = []
    if index is not None:
        hits = index.search(q)
        if not hits or decide(hits) == "none":
            # 一个都不像：用户可能是**刚装好**这个软件。限频地重扫一次开始菜单
            # 再试。没有这一步，新装的软件要重启 voice-ctl 才认（旧版就是这样）。
            if index.refresh():
                hits = index.search(q)

    if actions:
        best: tuple[float, object, str] | None = None
        for a in actions:
            if not getattr(a, "enabled", True):
                continue
            for alias in list(a.aliases) + [a.id]:
                na = (normalizer or Normalizer()).normalize(alias) if normalizer else alias
                s = _alias_score(q, na)
                if s > 0 and (best is None or s > best[0]):
                    best = (s, a, alias)
        if best is not None and best[0] >= 0.9:
            a = best[1]
            return (
                ResolvedApp(
                    name=getattr(a, "aliases", [None])[0] or a.id,
                    action_id=a.id,
                    score=best[0],
                    how="action",
                ),
                hits,
            )

    if not hits:
        return None, []

    verdict = decide(hits)
    if verdict == "none":
        return None, hits
    top = hits[0]
    return (
        ResolvedApp(
            name=top.entry.name,
            appid=top.entry.appid,
            system=top.entry.system,
            score=top.score,
            how=top.how,
        ),
        hits,
    )


def _alias_score(text: str, alias: str) -> float:
    """别名匹配的近似实现，只用于"这句话里的名字对上哪条配置动作"。

    真正的别名匹配在 matcher.py（带拼音、区分字消歧），这里不求完全一致——
    它只回答"剥掉动词之后剩下的名字，是不是某条配置动作"。用严格的方向敏感
    比较，避免「QQ音乐」落到「QQ」上。
    """
    from .appfind import strict_score

    return strict_score(text, alias)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def interpret(
    text: str,
    *,
    index: AppIndex | None = None,
    actions: list | None = None,
    normalizer: Normalizer | None = None,
    now: datetime | None = None,
) -> Intent | None:
    """读一句话，返回意图；读不懂返回 None（下游还有别名匹配兜着）。"""
    norm = (normalizer or Normalizer()).normalize(text)
    if not norm:
        return None

    # 1) 否定优先于一切：「不要打开记事本」里的「打开」是动词，
    #    但整句的意思是不做。顺序反了就会把否定句执行掉。
    if lexicon.is_negated(norm):
        return Intent("none", "negate", source=text, payload=norm,
                      why="句首是否定词，不执行")

    # 2) 日程：先看线索词，再看时间。顺序反了的话「今天天气怎么样」
    #    会因为解析出「今天」而被当成日程。
    when = parse_when(text, now)
    if _has_schedule_cue(norm, when):
        rest = strip_spans(text, when.spans) if when else text
        title = extract_title(rest, when=when, fallback=_default_title(when))
        reason = ""
        if when is None:
            reason = "说了要提醒，但没听出时间"
        elif when.in_past:
            reason = "说的是一个已经过去的时间"
        return Intent(
            SCHEDULE, "schedule", verb="设置", when=when, title=title,
            source=text, payload=rest, question=reason,
            why=f"日程线索 + 时间解析（{when.note() if when else '无'}）",
        )

    polarity = lexicon.verb_kind(norm)
    if polarity == "negate":
        return Intent("none", "negate", source=text, payload=norm, why="否定")

    # 3) 关闭：动词是关，就找被关的对象。找不到就交给别名匹配。
    if polarity in ("close", "force"):
        target = _closable_target(norm, index=index, normalizer=normalizer)
        if target is None:
            return None
        app, hits = target
        lv = lexicon.leading_verb(norm, lexicon.CLOSE_VERBS + lexicon.FORCE_VERBS)
        return Intent(
            APP, polarity, verb=lv[0] if lv else polarity, app=app,
            source=text, payload=norm, candidates=hits,
            why=f"动词是{'强制关闭' if polarity == 'force' else '关闭'}，对象「{app.name}」",
        )

    # 4) 打开：把动词和客套话剥掉，剩下的当名字去解析。
    #
    # 只有**确实像个打开命令**时才继续。判据是句首那个开关动词不是孤零零一个字：
    # 「打开QQ音乐」是命令（哪怕 QQ 音乐没装，也要告诉用户），
    # 而「今天天气怎么样」「随便说点什么」里的「今天/随便」不是动词，
    # 压根不该被当成"打开某个应用失败"。
    obj = strip_verb(norm)
    if not obj:
        return None
    opens = any(
        norm.startswith(v) for v in lexicon.OPEN_VERBS if len(v) > 1
    )
    app, hits = resolve_app(obj, index=index, actions=actions, normalizer=normalizer)
    if app is None:
        if not opens:
            return None  # 不是在要求打开什么，交给下游
        # 解析不出对象时也要留下**为什么**：下游会把它显示给用户。
        # 只报"别名没命中"等于什么都没说——用户明明说了「打开QQ音乐」。
        why = f"动词是打开，但没找到叫「{obj}」的应用"
        if hits:
            from .apps import names_for_display

            why += f"；最接近的是 {'、'.join(names_for_display(hits))}"
        return Intent("none", "open", verb="打开", source=text, payload=obj, why=why)
    question = ""
    if app.action_id is None and decide(hits) == "ask":
        from .apps import names_for_display

        others = "、".join(names_for_display(hits[1:]))
        question = f"你说的「{app.name}」是指 {app.name}" + (f"，还是 {others}？" if others else "？")
    return Intent(
        APP, "open", verb="打开", app=app, source=text, payload=obj, candidates=hits,
        question=question,
        why=f"动词是打开，对象「{obj}」解析为「{app.name}」（{app.how} {app.score:.2f}）",
    )


def _closable_target(
    norm: str, *, index: AppIndex | None, normalizer: Normalizer | None
) -> tuple[ResolvedApp, list[AppHit]] | None:
    """从「关闭微信」「把微信关掉」里取出要关的那个应用。"""
    if ba := lexicon.ba_construction(norm):
        obj = ba[1]
    else:
        obj = strip_verb(norm)
    if not obj:
        return None
    app, hits = resolve_app(obj, index=index, normalizer=normalizer)
    return (app, hits) if app is not None else None


__all__ = [
    "APP",
    "QUESTION",
    "SCHEDULE",
    "SCHEDULE_NOUNS",
    "SCHEDULE_VERBS",
    "Intent",
    "ResolvedApp",
    "extract_title",
    "interpret",
    "resolve_app",
    "strip_verb",
]
