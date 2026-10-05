"""意图层：一整句话 → 「要做什么 + 对谁做 + 什么时候」。

这一层是**加法**：别名匹配搞不定的句子（「关闭微信」被当成打开、
「设置今天下午三点的日程我要玩游戏」被当成打开 Windows 设置）由它接手；
它读不懂的句子必须原样还回去（返回 None），不能吃掉下游别名匹配本来能命中的口令。

两条贯穿全文件的纪律：
  * 时间一律用固定的 now=2026-10-05 10:12（和 test_timeparse 同一个「现在」），
    不碰系统时钟，跑多少次结果都一样。
  * 应用清单是假的（`AppIndex(loader=...)`，和 test_apps 一样），
    不读真实开始菜单、不起进程、不联网。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from voice_ctl.apps import AppIndex
from voice_ctl.config import ActionConfig
from voice_ctl.intent import APP, SCHEDULE, Intent, extract_title, interpret, strip_verb
from voice_ctl.lexicon import is_negated, verb_kind

NOW = datetime(2026, 10, 5, 10, 12)  # 周一，和 test_timeparse 的 NOW 保持一致

APPS = [
    ("QQ", r"C:\QQ\QQ.exe"),
    ("微信", r"E:\weixin\Weixin.exe"),
    ("Steam", r"C:\Steam\steam.exe"),
    ("记事本", "Microsoft.WindowsNotepad_x!App"),
]

ACTIONS = [
    ActionConfig(id="open.wechat", handler="open_app", aliases=["微信"], target=r"E:\weixin\Weixin.exe"),
]


@pytest.fixture()
def idx() -> AppIndex:
    return AppIndex(loader=lambda: list(APPS))


def say(text: str, idx: AppIndex, *, actions: list[ActionConfig] | None = None) -> Intent | None:
    return interpret(text, index=idx, actions=actions, now=NOW)


# --------------------------------------------------------------------------- #
# 读不懂就还回去
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    ["今天天气怎么样", "随便说点什么", "把音量调到五十", "我想吃一点点东西", "。", "   ", ""],
)
def test_unrelated_speech_returns_none(idx: AppIndex, text: str):
    """读不懂返回 None，下游的别名匹配才有机会。

    「今天天气怎么样」里有「今天」，timeparse 确实会解出日期；如果意图层把
    「解析出了时间」当成日程线索，用户随便一句话就变成一条日程。
    """
    assert say(text, idx) is None, f"{text!r} 不是命令，意图层不该吃掉它"


def test_unknown_name_reports_why_instead_of_nothing(idx: AppIndex):
    """「打开XX」而 XX 没装：要**说得出原因**，而不是含混地"没命中"。

    这里和 `test_unrelated_speech_returns_none` 的分界是句首有没有一个
    真正的开关动词。「打开完全没装的软件xyz」有「打开」，所以它算一次
    失败的打开命令——用户需要知道"没找到这个应用"；
    「今天天气怎么样」没有动词，那就不是命令，返回 None。
    """
    it = say("打开完全没装的软件xyz", idx)
    assert it is not None and it.kind == "none" and it.polarity == "open"
    assert "没找到" in it.why and "完全没装的软件xyz" in it.why


# --------------------------------------------------------------------------- #
# 否定
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text", ["不要打开记事本", "别关微信", "不用打开微信", "不要关闭微信", "不需要打开计算器"]
)
def test_sentence_initial_negation_is_not_executed(idx: AppIndex, text: str):
    """否定优先于动词：「不要打开记事本」里的「打开」是动词，但整句的意思是不做。

    顺序反了就会真的去打开记事本——这一条必须比任何动词判断都早。
    """
    it = say(text, idx, actions=ACTIONS)
    assert it is not None, f"{text!r} 应当判成「不做」，而不是读不懂"
    assert it.kind == "none" and it.polarity == "negate", f"{text!r} 被当成了要做的事：{it.polarity}"
    assert it.app is None and it.when is None, "否定句不能带出任何可执行的对象"
    assert it.source == text


@pytest.mark.parametrize("text", ["我不想打开计算器", "我不要打开记事本", "我不想打开微信"])
def test_pronoun_in_front_of_the_negation_is_still_negation(idx: AppIndex, text: str):
    """否定词前面挡着一个代词也算句首否定：「我不想打开计算器」不能真去打开计算器。

    只看第一个词就会漏掉它——「我」不是否定词；而「我不想」一旦被当成客套话剥掉，
    剩下的「打开计算器」反而更像肯定句。实测就是这么把计算器打开的。
    """
    assert is_negated(text), "「代词 + 否定词」是句首否定的一种，漏了就会做出反的效果"
    it = say(text, idx, actions=ACTIONS)
    assert it is not None, f"{text!r} 应当判成「不做」"
    assert it.kind == "none" and it.polarity == "negate", f"{text!r} → {it.polarity}"
    assert it.app is None, "否定句不能带出要操作的应用"


# --------------------------------------------------------------------------- #
# 打开
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "want"),
    [("打开QQ", "QQ"), ("打开微信", "微信"), ("打开记事本", "记事本"), ("打开steam", "Steam")],
)
def test_open_resolves_through_the_installed_index(idx: AppIndex, text: str, want: str):
    """没在 config.toml 里写过一行，也要能打开「开始菜单里就有的软件」。

    「打开QQ」以前什么都不匹配，就是因为名字只查配置、不查已安装应用。
    """
    it = say(text, idx)
    assert it is not None and it.kind == APP and it.polarity == "open", f"{text!r} → {it}"
    assert it.app is not None and it.app.name == want, f"对象解析错了：{it.app}"
    assert it.app.action_id is None, "这条不是配置动作，是索引里找到的已安装应用"
    assert it.app.how in ("exact", "name") and it.app.score >= 0.9, f"命中理由说不清：{it.app.how}"
    assert it.app.appid, "解析结果要带得动启动参数（exe 路径或 AppID）"


def test_configured_alias_wins_over_the_index(idx: AppIndex):
    """用户亲手写的别名优先级最高，而且命中它就照旧走动作表——行为跟加意图层之前一样。"""
    it = say("打开微信", idx, actions=ACTIONS)
    assert it is not None and it.app is not None
    assert it.app.action_id == "open.wechat" and it.app.how == "action"
    assert it.app.name == "微信"


def test_index_is_the_fallback_when_no_action_is_configured(idx: AppIndex):
    it = say("打开微信", idx)
    assert it is not None and it.app is not None
    assert it.app.action_id is None and it.app.how in ("exact", "name")
    assert it.app.appid.endswith("Weixin.exe"), "没有配置动作时，靠索引拿到的路径要能直接启动"


@pytest.mark.parametrize("text", ["打开微信", "请帮我打开微信", "打开一下微信"])
def test_open_strips_the_verb_and_the_politeness(idx: AppIndex, text: str):
    """「请帮我」「一下」是客套话，不能变成名字的一部分——否则「微信」就找不到了。"""
    it = say(text, idx, actions=ACTIONS)
    assert it is not None and it.app is not None, f"{text!r} 没剥干净"
    assert it.app.name == "微信" and it.payload == "微信", f"剥剩的正文是 {it.payload!r}"


# --------------------------------------------------------------------------- #
# 关闭
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", ["关闭微信", "关掉微信", "把微信关掉", "退出微信", "结束微信"])
def test_close_sentences_resolve_the_target(idx: AppIndex, text: str):
    """四种说法都要落到「关微信」上，包括把字句——动词在句尾。

    「把微信关掉」动词不在句首，靠 strip_verb 是剥不出来的，得走 ba_construction。
    """
    it = say(text, idx)
    assert it is not None, f"{text!r} 没读出关闭意图"
    assert it.kind == APP and it.polarity == "close", f"{text!r} → {it.polarity}"
    assert it.app is not None and it.app.name == "微信", f"要关的对象不对：{it.app}"


def test_close_never_comes_back_as_open(idx: AppIndex):
    """头号缺陷之一：「关闭微信」命中「微信」别名（0.950）→ **打开**微信。

    这里特意带上一条「打开微信」的配置动作：关闭意图绝不能借它执行。
    """
    it = say("关闭微信", idx, actions=ACTIONS)
    assert it is not None and it.app is not None
    assert it.polarity != "open", "「关闭微信」变成打开微信就是把用户的指令做反了"
    assert it.polarity == "close" and it.app.name == "微信"
    assert it.app.action_id != "open.wechat", "关闭不能落到那条打开动作上"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("强制关闭微信", "微信"),
        ("强行关闭微信", "微信"),
        ("杀掉微信", "微信"),
        ("干掉微信", "微信"),
        ("结束进程微信", "微信"),
        ("强行关闭QQ", "QQ"),
    ],
)
def test_force_close_resolves_the_target_and_keeps_force_polarity(
    idx: AppIndex, text: str, want: str
):
    """强杀类动词要剥两层（「强制」+「关闭」）才露得出对象，而且极性必须是 force。

    「强制关闭微信」以前剥不掉「强制关闭」，整串被当成应用名去索引里查，查不到就
    返回 None，掉到下游别名匹配——而别名匹配对动词无感，反而会把微信**打开**。
    极性还决定 runner 走「结束进程」而不是普通的关窗口，所以两者都要钉住。
    """
    assert verb_kind(text) == "force", "强杀类动词的极性判断"
    assert strip_verb(text) == want, "强制类动词没剥干净，整串拿去查索引就什么都查不到"
    it = say(text, idx, actions=ACTIONS)
    assert it is not None and it.kind == APP, f"{text!r} 没读出强制关闭意图"
    assert it.polarity == "force", f"{text!r} 判成了 {it.polarity}"
    assert it.app is not None and it.app.name == want, f"要关的对象不对：{it.app}"
    assert it.app.appid, "强杀也要拿到进程线索（exe 路径 / AppID）"


@pytest.mark.parametrize("text", ["强制关掉微信", "强行关掉微信"])
def test_force_verb_that_uses_a_softer_close_word(idx: AppIndex, text: str):
    """「强制关掉」也算强杀。

    这条本来是钉住缺口的：FORCE_VERBS 里只有「强制关闭/强行关闭」，没有
    「强制关掉」。于是「强制关掉微信」的极性靠句中的「关掉」判成 close，
    而 strip_verb 剥不掉句首的「强制」，整串进索引查不到 → None，
    又掉回对动词无感的别名匹配（很可能反而**打开**微信）。
    补上这四个组合之后它应当和「强制关闭微信」一样。
    """
    assert verb_kind(text) == "force"
    it = say(text, idx, actions=ACTIONS)
    assert it is not None and it.polarity == "force"
    assert it.app is not None and it.app.name == "微信"


def test_plain_close_is_still_plain_close(idx: AppIndex):
    """补 FORCE_VERBS 不能把普通关闭也变成强杀——那会让微信没法优雅退出。"""
    for text in ("关闭微信", "关掉微信", "把微信关掉", "退出微信"):
        assert verb_kind(text) == "close", text
        it = say(text, idx, actions=ACTIONS)
        assert it is not None and it.polarity == "close", text


# --------------------------------------------------------------------------- #
# 日程：两个真实缺陷里的头一个
# --------------------------------------------------------------------------- #


def test_schedule_sentence_is_not_an_app_intent(idx: AppIndex):
    """头号缺陷之一：别名「设置」被命中（0.912）→ 打开 Windows 设置。用户要的是日程。

    别名是**句子里的一部分**：动词「设置」的宾语是「日程」，不是「设置」这个名字。
    """
    text = "设置今天下午三点的日程我要玩游戏"
    it = say(text, idx, actions=ACTIONS)
    assert it is not None, f"{text!r} 应当判成日程"
    assert it.kind == SCHEDULE and it.polarity == "schedule", f"判成了 {it.kind}/{it.polarity}"
    assert it.app is None, "日程句不能同时给出一个要打开的应用"
    assert it.when is not None and it.when.start == datetime(2026, 10, 5, 15, 0), f"时间不对：{it.when}"
    assert it.title == "我要玩游戏", f"标题要逐字保留用户的原话，现在是 {it.title!r}"
    assert it.source == text


@pytest.mark.parametrize(
    ("text", "want_start", "want_title"),
    [
        ("明天早上八点提醒我开会", datetime(2026, 10, 6, 8, 0), "开会"),
        ("设置日程「我要玩游戏」今天下午三点", datetime(2026, 10, 5, 15, 0), "我要玩游戏"),
        ("设置一个今天下午三点的日程，内容是玩游戏", datetime(2026, 10, 5, 15, 0), "玩游戏"),
    ],
)
def test_schedule_takes_title_and_time_apart(
    idx: AppIndex, text: str, want_start: datetime, want_title: str
):
    """时间表达式是**参数**，不是标题的一部分；引号和「内容是」是用户划出的标题。"""
    it = say(text, idx)
    assert it is not None and it.kind == SCHEDULE, f"{text!r} 没读出日程"
    assert it.when is not None and it.when.start == want_start, f"时间不对：{it.when and it.when.start}"
    assert it.title == want_title, f"标题应为 {want_title!r}，实际 {it.title!r}"


def test_relative_schedule_counts_from_now(idx: AppIndex):
    it = say("半小时后提醒我喝水", idx)
    assert it is not None and it.when is not None
    assert it.when.start == NOW + timedelta(minutes=30) == datetime(2026, 10, 5, 10, 42)
    assert it.when.relative
    assert it.title == "喝水", "「半小时后」要整段剥掉，不能留在标题里"


def test_default_title_is_the_time_not_the_leftover_word(idx: AppIndex):
    """「下午三点的日程」压根没说标题：不能拿连接词「的」当日程名。"""
    it = say("下午三点的日程", idx)
    assert it is not None and it.when is not None
    assert it.title != "的", "剥完壳只剩「的」，它只是连接词，不是日程名"
    assert "15:00" in it.title, f"没有标题时用时间本身兜底，现在是 {it.title!r}"
    assert it.payload == "的日程", "正文剩什么由剥时间决定，标题会退回兜底值"


def test_ambiguous_clock_is_reported_not_guessed(idx: AppIndex):
    """「三点提醒我喝水」没说上午下午：给个最合理的时间，但必须标出来。

    确认卡靠 `ambiguous` + `note()` 写「没说上午/下午，按下午算」，不能悄悄替用户决定。
    """
    it = say("三点提醒我喝水", idx)
    assert it is not None and it.when is not None
    assert it.when.ambiguous is True, "没说上午下午却没标出来，等于替用户猜了"
    assert it.when.note(), "标了 ambiguous 就要有一句给用户看的话"
    assert "下午" in it.when.note(), f"得说清按哪半天算的：{it.when.note()!r}"
    assert it.title == "喝水"


def test_schedule_without_a_time_asks_a_question(idx: AppIndex):
    """说了要提醒但没听出时间：不猜时间，把问题交出去（runner 会拿它当确认问题）。"""
    it = say("设置日程", idx)
    assert it is not None and it.kind == SCHEDULE and it.polarity == "schedule"
    assert it.when is None, "没说的时间不能凭空补一个"
    assert it.question, "没听出时间就得问一句"
    assert it.needs_confirm is True
    assert it.title, "标题不能是空的，日程列表里得认得出来"


# --------------------------------------------------------------------------- #
# source / payload 纪律
# --------------------------------------------------------------------------- #


def test_source_is_the_raw_asr_text(idx: AppIndex):
    """原文优先：归一化会删口语词、换同音字，日程标题和日志要的是用户真正说的那句。"""
    text = "设置今天下午三点的日程我要玩游戏"
    it = say(text, idx)
    assert it is not None and it.source == text, "source 必须逐字等于 ASR 原文"


def test_homophone_is_normalized_for_matching_but_never_for_source(idx: AppIndex):
    """ASR 把「微信」听成「威信」是常态：匹配用归一化后的字，原文留着不动。"""
    it = say("打开威信", idx)
    assert it is not None and it.app is not None and it.app.name == "微信"
    assert it.source == "打开威信", "source 要保留用户说的那个字"
    assert it.payload == "微信", "拿去查索引的是归一化后的正文"


@pytest.mark.parametrize(
    "text", ["设置今天下午三点的日程我要玩游戏", "设置一个今天下午三点的日程，内容是玩游戏"]
)
def test_payload_does_not_contain_the_time_expression(idx: AppIndex, text: str):
    """时间要被整段剥掉，否则它会跟着进日程标题（「今天下午三点的日程」）。"""
    it = say(text, idx)
    assert it is not None and it.when is not None
    assert "今天下午三点" not in it.payload, f"时间表达式漏进正文了：{it.payload!r}"
    for i, j in it.when.spans:
        assert text[i:j] not in it.payload, f"spans 里的 {text[i:j]!r} 还在正文里：{it.payload!r}"


# --------------------------------------------------------------------------- #
# extract_title：单测，不经 interpret
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "want"),
    [
        ("设置「开会」的日程，内容是玩游戏", "开会"),
        ("设置日程「我要玩游戏」今天下午三点", "我要玩游戏"),
        ("安排『牙医』明天下午三点", "牙医"),
    ],
)
def test_title_in_quotes_wins(payload: str, want: str):
    """用户加引号就是在明确划标题，它比「内容是」之类的线索都优先。"""
    assert extract_title(payload, fallback="兜底") == want


@pytest.mark.parametrize(
    ("payload", "want"),
    [
        ("设置的日程，内容是玩游戏", "玩游戏"),
        ("设置一个日程，内容是我要玩游戏", "我要玩游戏"),
        ("设置日程，标题是买菜", "买菜"),
    ],
)
def test_title_after_the_content_marker(payload: str, want: str):
    assert extract_title(payload, fallback="兜底") == want


@pytest.mark.parametrize("payload", ["", "的", "了", "设置的日程", "的日程"])
def test_title_falls_back_when_only_glue_is_left(payload: str):
    """剥完壳只剩「的/了」这种连接词，说明用户没说标题，退回兜底值（通常是时间）。"""
    assert extract_title(payload, fallback="兜底") == "兜底", f"{payload!r} 里没有标题"


def test_leading_wo_yao_is_kept():
    """「…的日程我要玩游戏」剥壳后是「我要玩游戏」。

    再顺手把「我要」当客套话剥掉，用户的原话就短了一截——日程标题必须逐字保留。
    """
    title = extract_title("设置的日程我要玩游戏", fallback="兜底")
    assert title == "我要玩游戏", f"用户的原话被改短了：{title!r}"
    assert title.startswith("我要"), "「我要」是用户话的一部分，不是客套话"
