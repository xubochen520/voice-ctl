"""区分字消歧测试。

背景（实测发现的真实缺陷）：别名「音量加」和「音量减」只差最后一个字，
模糊相似度会把「把音量调小一点」判成 volume_up。错的分数本身过了阈值，
所以光调 threshold 没用——必须看哪个候选拥有「输入里出现、对手没有」的字。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl.config import ActionConfig, load_config
from voice_ctl.matcher import Matcher
from voice_ctl.normalize import Normalizer

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def real() -> Matcher:
    cfg = load_config(ROOT / "config.toml")
    return Matcher(cfg.enabled_actions, threshold=cfg.match.threshold)


def mk(actions: list[ActionConfig]) -> Matcher:
    return Matcher(actions, normalizer=Normalizer())


# --------------------------------------------------------------------------- #
# 音量方向
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("把音量调小一点", "sys.volume_down"),
        ("音量小一点", "sys.volume_down"),
        ("声音小点", "sys.volume_down"),
        ("小声点", "sys.volume_down"),
        ("音量减", "sys.volume_down"),
        ("把音量调大一点", "sys.volume_up"),
        ("音量大一点", "sys.volume_up"),
        ("声音大点", "sys.volume_up"),
        ("大声点", "sys.volume_up"),
        ("音量加", "sys.volume_up"),
    ],
)
def test_volume_direction(real: Matcher, utterance: str, expected: str):
    m = real.best(utterance)
    assert m is not None, f"{utterance!r} 未命中"
    assert m.action_id == expected, (
        f"{utterance!r} 判成了 {m.action_id}（score={m.score:.2f} {m.strategy}）"
    )


def test_disambiguation_is_recorded_in_strategy(real: Matcher):
    """翻盘必须留痕，否则出问题时无法解释。"""
    ranked = real.rank("把音量调小一点")
    assert ranked[0].action_id == "sys.volume_down"
    assert "distinct" in ranked[0].strategy, f"没留下消歧痕迹：{ranked[0].strategy}"


# --------------------------------------------------------------------------- #
# 安全边界：不该乱翻盘
# --------------------------------------------------------------------------- #


def test_no_swap_when_scores_far_apart():
    """分差大时不许动——否则会引入难以预测的行为。"""
    actions = [
        ActionConfig(id="a", handler="open_app", aliases=["记事本"], target="notepad.exe"),
        ActionConfig(id="b", handler="open_app", aliases=["计算器"], target="calc.exe"),
    ]
    m = mk(actions)
    ranked = m.rank("记事本")
    assert ranked[0].action_id == "a"
    assert ranked[0].score == pytest.approx(1.0)


def test_no_swap_when_both_have_distinctive_chars():
    """双方都有区分字时保持原序——信息不足，不猜。"""
    actions = [
        ActionConfig(id="up", handler="sysctl", aliases=["音量加"], target="volume_up"),
        ActionConfig(id="down", handler="sysctl", aliases=["音量减"], target="volume_down"),
    ]
    m = mk(actions)
    # 输入同时含「加」和「减」→ 两边都有区分字 → 不动
    ranked = m.rank("音量加减")
    assert ranked[0].action_id == "up", "信息冲突时不该翻盘"


def test_no_swap_when_neither_has_distinctive_char():
    actions = [
        ActionConfig(id="a", handler="open_app", aliases=["记事本"], target="notepad.exe"),
        ActionConfig(id="b", handler="open_app", aliases=["记事簿"], target="notepad.exe"),
    ]
    m = mk(actions)
    # 输入两个都不完全匹配，没有独有字 → 保持原序
    ranked = m.rank("记事")
    assert len(ranked) == 2
    assert ranked[0].action_id in ("a", "b")


def test_identical_ids_never_swap():
    actions = [ActionConfig(id="only", handler="open_app", aliases=["记事本"], target="notepad.exe")]
    m = mk(actions)
    ranked = m.rank("记事本")
    assert len(ranked) == 1
    assert ranked[0].action_id == "only"


def test_swap_respects_threshold():
    """没有区分字证据时，低分依然要被阈值挡住——消歧不能变成万能放行。"""
    actions = [
        ActionConfig(id="a", handler="open_app", aliases=["记事本"], target="notepad.exe"),
        ActionConfig(id="b", handler="open_app", aliases=["记事簿"], target="notepad.exe"),
    ]
    strict = Matcher(actions, threshold=95, normalizer=Normalizer())
    assert strict.best("打开那个本子") is None, "无区分字证据时必须被阈值拦住"


def test_disambiguated_match_scores_full(real: Matcher):
    """翻盘后的胜者应当是满分：区分字是决定性证据，不该卡在阈值下方。"""
    ranked = real.rank("把音量调小一点")
    assert ranked[0].action_id == "sys.volume_down"
    assert ranked[0].score == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 其他成对动作不该被误伤
# --------------------------------------------------------------------------- #


def test_lock_and_screenshot_still_correct(real: Matcher):
    assert real.best("锁屏").action_id == "sys.lock"
    assert real.best("截屏").action_id == "sys.screenshot"
    assert real.best("静音").action_id == "sys.mute"


def test_mute_and_volume_not_confused(real: Matcher):
    """静音 / 音量加 / 音量减三者要分得开。"""
    assert real.best("静音").action_id == "sys.mute"
    assert real.best("音量加").action_id == "sys.volume_up"
    assert real.best("音量减").action_id == "sys.volume_down"
