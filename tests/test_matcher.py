"""别名匹配测试。这是第 0 层的地基，必须覆盖到位。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl.config import ActionConfig, load_config
from voice_ctl.matcher import Matcher, ratio
from voice_ctl.normalize import NormalizeConfig, Normalizer

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = ROOT / "config.toml"


def mk_matcher(actions: list[ActionConfig], **kw) -> Matcher:
    return Matcher(actions, normalizer=Normalizer(NormalizeConfig()), **kw)


@pytest.fixture(scope="module")
def real_matcher() -> Matcher:
    cfg = load_config(REAL_CONFIG)
    return Matcher(cfg.enabled_actions, threshold=cfg.match.threshold)


# --------------------------------------------------------------------------- #
# ratio
# --------------------------------------------------------------------------- #


def test_ratio_identical():
    assert ratio("打开微信", "打开微信") == 1.0


def test_ratio_empty():
    assert ratio("", "x") == 0.0
    assert ratio("x", "") == 0.0


def test_ratio_containment_bonus():
    """短串被包含时应当拿到明显高分——这是「音量加」命中「把音量加起来」的基础。"""
    assert ratio("音量加", "把音量加起来") > 0.7


def test_ratio_unrelated_is_low():
    assert ratio("打开微信", "计算器") < 0.4


# --------------------------------------------------------------------------- #
# 真实配置上的匹配
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("打开微信", "open.wechat"),
        ("微信", "open.wechat"),
        ("帮我打开微信", "open.wechat"),
        ("打开记事本", "open.notepad"),
        ("记事本", "open.notepad"),
        ("截屏", "sys.screenshot"),
        ("截图", "sys.screenshot"),
        ("锁屏", "sys.lock"),
        ("静音", "sys.mute"),
        ("打开计算器", "open.calc"),
        ("计算器", "open.calc"),
        ("任务管理器", "open.taskmgr"),
        ("资源管理器", "open.explorer"),
        ("打开b站", "web.bilibili"),
        ("github", "web.github"),
        ("音量加", "sys.volume_up"),
        ("音量减", "sys.volume_down"),
        ("显示桌面", "sys.show_desktop"),
    ],
)
def test_real_utterances_match(real_matcher: Matcher, utterance: str, expected: str):
    m = real_matcher.best(utterance)
    assert m is not None, f"{utterance!r} 没有匹配到任何动作"
    assert m.action_id == expected, f"{utterance!r} 匹配到 {m.action_id}，期望 {expected}"


@pytest.mark.parametrize(
    "utterance",
    ["今天天气怎么样", "帮我写一首诗", "这个周末去哪玩比较好呢", "", "   "],
)
def test_unrelated_text_does_not_match(real_matcher: Matcher, utterance: str):
    """不能乱命中——错误执行比不执行更糟（比如误触发关机）。"""
    assert real_matcher.best(utterance) is None, f"{utterance!r} 不该命中"


def test_homophone_corrected_before_matching(real_matcher: Matcher):
    """「威信」经归一化后应当命中微信。"""
    m = real_matcher.best("打开威信")
    assert m is not None
    assert m.action_id == "open.wechat"


def test_filler_words_stripped_then_matched(real_matcher: Matcher):
    m = real_matcher.best("请帮我打开一下记事本")
    assert m is not None
    assert m.action_id == "open.notepad"


def test_alias_hit_beats_weak_similarity(real_matcher: Matcher):
    m = real_matcher.best("打开微信")
    assert m is not None
    assert m.strategy in ("exact", "alias-hit")
    assert m.score > 0.8


def test_exact_match_scores_1(real_matcher: Matcher):
    m = real_matcher.best("微信")
    assert m is not None
    assert m.score == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 阈值与结构
# --------------------------------------------------------------------------- #


def test_threshold_filters_low_scores():
    actions = [ActionConfig(id="x", handler="open_app", aliases=["微信"], target="notepad.exe")]
    strict = mk_matcher(actions, threshold=99)
    assert strict.best("微信") is not None, "精确命中 1.0 必须过 99 阈值"
    assert strict.best("打开那个绿色的东西") is None


def test_rank_is_sorted_desc(real_matcher: Matcher):
    ranked = real_matcher.rank("打开微信")
    assert len(ranked) >= 2
    scores = [m.score for m in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[0].action_id == "open.wechat"


def test_disabled_actions_excluded():
    actions = [
        ActionConfig(id="on", handler="open_app", aliases=["记事本"], target="notepad.exe"),
        ActionConfig(id="off", handler="open_app", aliases=["计算器"], target="calc.exe", enabled=False),
    ]
    m = mk_matcher(actions)
    assert m.action_ids == ["on"]
    assert m.best("计算器") is None


def test_action_id_itself_is_matchable():
    """动作 id 的末段也应当可匹配，方便用户直接说英文 id。"""
    actions = [ActionConfig(id="web.github", handler="open_url", aliases=[], target="https://github.com")]
    m = mk_matcher(actions)
    assert m.best("github") is not None
    assert m.best("web.github") is not None


def test_explain_is_readable(real_matcher: Matcher):
    text = real_matcher.explain("打开微信")
    assert "归一化后" in text
    assert "open.wechat" in text


def test_empty_matcher_is_safe():
    m = mk_matcher([])
    assert m.best("打开微信") is None
    assert m.rank("打开微信") == []
