"""一句话里说了两件事。

实测来源：日志里那句「打开浏览器并且打开百度页面」原来只执行前半句，后半句
**没有任何提示地消失**——用户以为程序没听见，其实是听懂了但丢掉了。

这个功能的失败方式不对称，所以拆分规则做得**很保守**：

  * 少切一刀：后半句没执行，但前面那句是对的，用户至少看到了部分效果；
  * 多切一刀：把一条正常指令劈成两条读不懂的片段，**两件事都做不成**。

所以「和」「跟」「以及」刻意不在连接词表里，而且切完每一段还得像命令才认。
"""

from __future__ import annotations

import pytest

from voice_ctl.app import COMPOUND_SEPS, split_commands
from voice_ctl.config import ActionConfig, AppConfig
from voice_ctl.matcher import Matcher
from voice_ctl.app import Pipeline
from voice_ctl.normalize import Normalizer


# --------------------------------------------------------------------------- #
# 拆分本身
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("打开浏览器并且打开百度页面", ["打开浏览器", "打开百度页面"]),
        ("打开记事本然后打开计算器", ["打开记事本", "打开计算器"]),
        ("打开记事本，然后打开计算器", ["打开记事本", "打开计算器"]),
        ("打开百度页面并且打开淘宝", ["打开百度页面", "打开淘宝"]),
        ("打开微信还有就是打开 QQ", ["打开微信", "打开 QQ"]),
    ],
)
def test_two_commands_are_split(text: str, want: list[str]):
    assert split_commands(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "打开微信",
        "打开记事本",
        # 「和」「跟」不在连接词表里：它们在名字里出现的概率太高
        "打开记事本和计算器",
        "打开哔哩哔哩和它的朋友们",
        # 后半段不像命令 → 整句不拆（宁可少切一刀）
        "打开浏览器并且很快",
        "设置今天下午三点的日程我要玩游戏",
    ],
)
def test_ambiguous_sentences_are_left_alone(text: str):
    assert split_commands(text) == [text]


def test_every_separator_is_covered():
    """表里每个连接词都要真的能切开，否则等于写着好看。"""
    for sep in COMPOUND_SEPS:
        assert split_commands(f"打开记事本{sep}打开计算器") == ["打开记事本", "打开计算器"], sep


# --------------------------------------------------------------------------- #
# 端到端：两件事都要执行
# --------------------------------------------------------------------------- #


def _pipe() -> Pipeline:
    cfg = AppConfig(
        actions=[
            ActionConfig(id="open.notepad", handler="open_app", aliases=["记事本"],
                         target="notepad.exe", describe="打开记事本"),
            ActionConfig(id="open.calc", handler="open_app", aliases=["计算器"],
                         target="calc.exe", describe="打开计算器"),
            ActionConfig(id="open.web", handler="open_url", aliases=["网页"]),
            ActionConfig(id="open.target", handler="open_target", aliases=["打开应用"]),
        ]
    )
    n = Normalizer()
    return Pipeline(
        asr=None,  # type: ignore[arg-type]
        matcher=Matcher(cfg.enabled_actions, normalizer=n, threshold=cfg.match.threshold),
        registry=__import__("voice_ctl.actions", fromlist=["build_registry"]).build_registry(
            cfg.enabled_actions
        ),
        actions=cfg.enabled_actions,
        normalizer=n,
        app_index=None,
        intent_enabled=True,
    )


def test_both_halves_actually_run():
    out = _pipe().process_text("打开记事本然后打开计算器", dry_run=True)
    assert len(out.steps) == 2, out.report()
    ids = [action for _, action, _ in out.steps]
    assert ids == ["open.notepad", "open.calc"], ids


def test_report_lists_every_step():
    """报告里必须逐条列出来。只说第一条的话，用户看不出来第二件成没成。"""
    out = _pipe().process_text("打开记事本然后打开计算器", dry_run=True)
    text = out.report(verbose=True)
    assert "打开记事本" in text and "打开计算器" in text
    assert "2 件事" in text


def test_single_command_has_no_steps():
    """单件事时不该走拆分那条路——那会让报告多出一堆没用的行。"""
    out = _pipe().process_text("打开记事本", dry_run=True)
    assert out.steps == []
    assert "拆分" not in out.report(verbose=True)


def test_partial_failure_is_not_reported_as_success():
    """一件成一件败时，整体不能报成功——那会让用户以为都做成了。"""
    out = _pipe().process_text("打开记事本然后打开不存在的东西", dry_run=True)
    assert len(out.steps) == 2
    assert not out.result.ok, out.report()
    assert "1 件成功" in out.result.message or "没做成" in out.result.message
