"""自我更正：「打开百度网盘，呸」不该打开网盘。

这条是用户亲口提的用例。它属于**最伤信任**的一类缺陷：用户刚说完"不要"，
程序却执行了。

难点全在**别误伤**上：中文里「不对」「算了」出现在正常的命令里毫不稀奇
（「提醒我不对账」），把这种句子作废掉，用户会觉得程序在乱猜。所以这里的
用例一半是"必须撤回"，一半是"绝不许撤回"。
"""

from __future__ import annotations

import pytest

from voice_ctl.lexicon import detect_correction
from voice_ctl.normalize import Normalizer

N = Normalizer()


# --------------------------------------------------------------------------- #
# 必须撤回
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "打开百度网盘，呸",
        "打开百度网盘 呸",
        "打开百度网盘呸",
        "打开百度网盘,呸",  # 半角逗号（语音输入和不少 ASR 给的就是它）
        "打开百度网盘。呸",
        "打开微信，我呸",
        "打开微信，说错了",
        "打开微信，口误",
    ],
)
def test_trailing_correction_voids_the_command(text: str):
    rest, voided = detect_correction(N.normalize(text))
    assert voided, f"{text!r} 应当整句作废，实际 rest={rest!r}"


# --------------------------------------------------------------------------- #
# 绝不许撤回
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "打开微信",
        "打开记事本",
        "关闭微信",
        "打开计算器",
        "设置今天下午三点的日程我要玩游戏",
        "提醒我不对账",  # 「不对」夹在正文里，后面没有新命令
        "明天早上八点提醒我不对账",
        "别打开计算器",
        "打开百度网盘",  # 没有标记，正常指令
    ],
)
def test_normal_commands_are_untouched(text: str):
    rest, voided = detect_correction(N.normalize(text))
    assert not voided, f"{text!r} 不该被作废"
    assert rest == N.normalize(text), f"{text!r} 的文本被改动了：{rest!r}"


def test_computed_suanle_is_not_a_correction():
    """「算了」不在撤回词表里，这是量过的。

    它在 `NO_WORDS` 里是对的（那是对**确认卡**的回答），但当整句的尾巴时
    「打开微信，算了」的意思是"算了别开"，而我们的切分拿不准这句话到底是不是
    在反悔。宁可少收回一次，也不要把「打开微信」这种正常指令吃掉。
    """
    rest, voided = detect_correction(N.normalize("打开微信，算了"))
    assert not voided
    assert rest == N.normalize("打开微信，算了")


@pytest.mark.parametrize("text", ["呸", "不对", "我呸", "说错了", "说错", "口误", "不对不对"])
def test_a_bare_marker_voids_the_utterance(text: str):
    """整句就是一个标记。

    这是被听错之后最常见的反应：上一次听岔了，于是这次只丢一个字出来。
    它必须走"作废"，否则会被当成一句看不懂的话报成「别名没命中」——
    那等于没接住用户。
    """
    rest, voided = detect_correction(N.normalize(text))
    assert voided, f"{text!r} 应当作废，实际 rest={rest!r}"


# --------------------------------------------------------------------------- #
# 改口：丢掉前面，用后面那条命令
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "打开百度，不对，打开淘宝",
        "打开百度不对打开淘宝",  # ASR 常常不给逗号
        "打开百度 不对 打开淘宝",
    ],
)
def test_midsentence_change_of_mind_keeps_the_later_command(text: str):
    rest, voided = detect_correction(N.normalize(text))
    assert not voided
    assert rest == "打开淘宝", f"{text!r} -> {rest!r}"


def test_change_of_mind_requires_an_actual_new_command():
    """标记后面那截必须是**真命令**，不能只是"还有字"。

    实测这个坑：「说错了」会被拆成「说错」+残余的「了」，而那个「了」不是命令。
    只判"后面还有字"的话，整句就变成一条叫「了」的新命令——用户说什么都执行不了，
    还会得到一句莫名其妙的报告。
    """
    rest, voided = detect_correction(N.normalize("说错了"))
    assert voided, f"应当作废，实际 rest={rest!r}"


def test_change_of_mind_needs_an_actual_new_command():
    """标记后面没有新命令时不算改口——否则「不对账」这类正文会被截断。"""
    rest, voided = detect_correction(N.normalize("打开百度不对"))
    assert voided, "标记在结尾，应当作废"


# --------------------------------------------------------------------------- #
# 端到端：不执行
# --------------------------------------------------------------------------- #


def test_the_users_exact_case_creates_no_intent():
    from voice_ctl.intent import interpret

    it = interpret("打开百度网盘，呸", normalizer=N)
    assert it is not None
    assert it.kind == "none", f"不该产生可执行的意图：{it}"
    assert it.polarity == "negate"
