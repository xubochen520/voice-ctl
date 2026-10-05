"""中文时间表达式解析。

固定「现在」= 2026-10-05（周一）10:12，所有预期值都据此手算。
覆盖三类容易出错的地方：
  * ASR 的数字写法（中文数字 / 阿拉伯数字 / 全角）混着来
  * 没说上午下午、没说几点、时间已经过了——这些必须**给出合理结果并标出来**，不能悄悄猜
  * 不合法的日期（2月30号）宁可不解析，也不挑个近似值
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from voice_ctl.timeparse import (
    cn2int,
    format_delta,
    format_when,
    hour24,
    parse_when,
    strip_spans,
    to_halfwidth,
)

NOW = datetime(2026, 10, 5, 10, 12)  # 周一


def dt(m: int, d: int, h: int = 0, mi: int = 0, y: int = 2026) -> datetime:
    return datetime(y, m, d, h, mi)


def when(text: str, **kw):  # noqa: ANN003, ANN201
    return parse_when(text, NOW, **kw)


# --------------------------------------------------------------------------- #
# 数字
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("s", "want"),
    [("十", 10), ("十一", 11), ("二十", 20), ("二十三", 23), ("一百二十", 120), ("一百零五", 105),
     ("两", 2), ("零", 0), ("三", 3), ("二零二六", 2026), ("2026", 2026), ("3", 3), ("〇", 0),
     ("九十九", 99), ("", None), ("abc", None), ("三x", None)],
)
def test_cn2int(s: str, want: int | None):
    assert cn2int(s) == want


def test_halfwidth_keeps_every_index():
    s = "下午３：３０提醒我"
    h = to_halfwidth(s)
    assert h == "下午3:30提醒我" and len(h) == len(s), "必须逐字符对应，spans 才能拿去切原文"


# --------------------------------------------------------------------------- #
# 钟点 + 时段
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("今天下午三点", dt(10, 5, 15)),
        ("今天下午3点", dt(10, 5, 15)),
        ("今天下午三点半", dt(10, 5, 15, 30)),
        ("今天下午三点一刻", dt(10, 5, 15, 15)),
        ("今天下午三点三刻", dt(10, 5, 15, 45)),
        ("今天下午三点十五分", dt(10, 5, 15, 15)),
        ("今天下午三点十五", dt(10, 5, 15, 15)),
        ("今天下午3点15分", dt(10, 5, 15, 15)),
        ("今天下午三点整", dt(10, 5, 15)),
        ("今天下午15:00", dt(10, 5, 15)),
        ("今天下午３：３０", dt(10, 5, 15, 30)),
        ("明天早上八点半", dt(10, 6, 8, 30)),
        ("明天早上8点", dt(10, 6, 8)),
        ("明天上午十点", dt(10, 6, 10)),
        ("明天中午十二点", dt(10, 6, 12)),
        ("明天中午一点", dt(10, 6, 13)),
        ("明天傍晚六点", dt(10, 6, 18)),
        ("明天晚上七点半", dt(10, 6, 19, 30)),
        ("明天凌晨三点", dt(10, 6, 3)),
        ("后天晚上七点", dt(10, 7, 19)),
        ("大后天上午十点", dt(10, 8, 10)),
        ("明天十五点三十分", dt(10, 6, 15, 30)),
        ("明天两点", dt(10, 6, 14)),
        ("明天二十三点", dt(10, 6, 23)),
    ],
)
def test_day_period_clock(text: str, want: datetime):
    r = when(text)
    assert r is not None, f"{text!r} 没解析出来"
    assert r.start == want, f"{text!r} → {r.start}，应为 {want}"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("今晚八点", dt(10, 5, 20)),
        ("明早七点半", dt(10, 6, 7, 30)),
        ("明晚八点", dt(10, 6, 20)),
        ("明晚十二点", dt(10, 7, 0)),  # 晚上十二点 = 次日 0 点
        ("今晚十二点", dt(10, 6, 0)),
        ("明天晚上十二点", dt(10, 7, 0)),
        ("明天半夜两点", dt(10, 7, 2)),
        ("明天晚上两点", dt(10, 7, 2)),
        ("明天二十四点", dt(10, 7, 0)),
    ],
)
def test_compound_words_and_midnight(text: str, want: datetime):
    r = when(text)
    assert r is not None and r.start == want, f"{text!r} → {r and r.start}"


# --------------------------------------------------------------------------- #
# 日期
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("下周三上午十点", dt(10, 14, 10)),
        ("下周一上午十点", dt(10, 12, 10)),
        ("下星期三上午十点", dt(10, 14, 10)),
        ("下礼拜三上午十点", dt(10, 14, 10)),
        ("下个周三上午十点", dt(10, 14, 10)),
        ("下下周一上午九点", dt(10, 19, 9)),
        ("这周五下午两点", dt(10, 9, 14)),
        ("本周五下午两点", dt(10, 9, 14)),
        ("周五下午两点", dt(10, 9, 14)),
        ("周日上午十点", dt(10, 11, 10)),
        ("周天上午十点", dt(10, 11, 10)),
        ("星期天上午十点", dt(10, 11, 10)),
        ("周末上午十点", dt(10, 10, 10)),
        ("下周末上午十点", dt(10, 17, 10)),
        ("周一下午两点", dt(10, 5, 14)),  # 今天就是周一，钟点还没到：今天
        ("周一上午九点", dt(10, 12, 9)),  # 今天周一但九点已过：指下周一
        ("10月20号下午三点", dt(10, 20, 15)),
        ("十月二十号下午三点", dt(10, 20, 15)),
        ("十月二十日下午三点", dt(10, 20, 15)),
        ("20号下午三点", dt(10, 20, 15)),
        ("3号下午三点", dt(11, 3, 15)),  # 本月 3 号已过 → 下个月
        ("5号下午三点", dt(10, 5, 15)),  # 今天
        ("2027年1月1日上午十点", dt(1, 1, 10, y=2027)),
        ("二零二七年一月一号上午十点", dt(1, 1, 10, y=2027)),
        ("2027-01-01上午十点", dt(1, 1, 10, y=2027)),
        ("1月3号上午十点", dt(1, 3, 10, y=2027)),  # 今年的已过 → 明年
        ("月底下午五点", dt(10, 31, 17)),
        ("这个月底下午五点", dt(10, 31, 17)),
        ("下个月底下午五点", dt(11, 30, 17)),
        ("下个月五号上午九点", dt(11, 5, 9)),
        ("三天后晚上七点半", dt(10, 8, 19, 30)),
        ("3天后晚上七点半", dt(10, 8, 19, 30)),
        ("两周后上午十点", dt(10, 19, 10)),
        ("一个月后上午十点", dt(11, 5, 10)),
        ("一年后上午十点", dt(2027, 10, 5, 10) if False else datetime(2027, 10, 5, 10)),
    ],
)
def test_dates(text: str, want: datetime):
    r = when(text)
    assert r is not None, f"{text!r} 没解析出来"
    assert r.start == want, f"{text!r} → {r.start}，应为 {want}"


def test_month_arithmetic_clips_day():
    """1 月 31 号 + 1 个月 = 2 月 28 号，不能抛异常也不能溢出到 3 月。"""
    r = parse_when("一个月后上午十点", datetime(2026, 1, 31, 8, 0))
    assert r is not None and r.start == datetime(2026, 2, 28, 10)


def test_year_rollover_for_next_month():
    r = parse_when("下个月五号上午九点", datetime(2026, 12, 20, 8, 0))
    assert r is not None and r.start == datetime(2027, 1, 5, 9)


def test_day_only_skips_months_without_that_day():
    r = parse_when("31号上午十点", datetime(2026, 11, 5, 8, 0))  # 11 月没有 31 号
    assert r is not None and r.start == datetime(2026, 12, 31, 10)


# --------------------------------------------------------------------------- #
# 相对时长
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("半小时后", timedelta(minutes=30)),
        ("半个小时后", timedelta(minutes=30)),
        ("十分钟后", timedelta(minutes=10)),
        ("10分钟后", timedelta(minutes=10)),
        ("五分钟之后", timedelta(minutes=5)),
        ("一个小时后", timedelta(hours=1)),
        ("两小时后", timedelta(hours=2)),
        ("一个半小时后", timedelta(hours=1, minutes=30)),
        ("两个半小时后", timedelta(hours=2, minutes=30)),
        ("两小时十分钟后", timedelta(hours=2, minutes=10)),
        ("1小时30分钟后", timedelta(hours=1, minutes=30)),
        ("一小时半后", timedelta(hours=1, minutes=30)),
        ("三十秒后", timedelta(seconds=30)),
    ],
)
def test_relative_durations(text: str, delta: timedelta):
    r = when(text)
    assert r is not None and r.relative
    assert r.start == NOW + delta


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("设置一个十分钟的倒计时", timedelta(minutes=10)),
        ("来个半小时倒计时", timedelta(minutes=30)),
        ("一个半小时的倒计时", timedelta(hours=1, minutes=30)),
        ("两分钟计时", timedelta(minutes=2)),
    ],
)
def test_timers_without_hou(text: str, delta: timedelta):
    r = when(text)
    assert r is not None and r.timer and r.relative
    assert r.start == NOW + delta


def test_relative_beats_other_components():
    """「十分钟后」就是 10:22，旁边出现别的数字不该干扰。"""
    r = when("十分钟后提醒我三点的会议")
    assert r is not None and r.start == NOW + timedelta(minutes=10)


# --------------------------------------------------------------------------- #
# 不确定性必须被标出来
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "want", "word"),
    [
        ("三点", dt(10, 5, 15), "下午"),  # 1-6 点：多半是下午
        ("五点", dt(10, 5, 17), "下午"),
        ("十二点", dt(10, 5, 12), "中午"),
        ("十一点", dt(10, 5, 11), "上午"),
        ("明天七点", dt(10, 6, 7), "上午"),
        ("明天八点", dt(10, 6, 8), "上午"),
    ],
)
def test_no_period_is_flagged_ambiguous(text: str, want: datetime, word: str):
    r = when(text)
    assert r is not None and r.start == want
    assert r.ambiguous and word in r.note(), f"没把「猜的」标出来：{r.notes}"


def test_explicit_period_is_not_ambiguous():
    r = when("下午三点")
    assert r is not None and not r.ambiguous and r.notes == []


def test_24h_clock_is_not_ambiguous():
    for text in ("15:00", "十五点", "今天22点", "零点"):
        r = when(text)
        assert r is not None and not r.ambiguous, text


def test_alarm_hint_reads_small_hours_as_morning():
    """「明天五点叫我起床」没人想要下午五点被叫醒。"""
    assert when("明天五点", alarm=True).start == dt(10, 6, 5)  # type: ignore[union-attr]
    assert when("明天五点", alarm=False).start == dt(10, 6, 17)  # type: ignore[union-attr]


def test_ambiguous_hour_already_past_today_picks_other_half():
    """现在 10:12 说「今天七点」，早上七点早过了——指的多半是晚上七点。"""
    r = when("今天七点")
    assert r is not None and r.start == dt(10, 5, 19)
    assert "另一半天" in r.note()
    r2 = when("八点")
    assert r2 is not None and r2.start == dt(10, 5, 20)
    r3 = when("九点一刻")
    assert r3 is not None and r3.start == dt(10, 5, 21, 15)


def test_date_without_clock_uses_default_and_says_so():
    r = when("明天")
    assert r is not None and r.start == dt(10, 6, 9) and r.defaulted
    assert "09:00" in r.note()
    r2 = when("明天下午")
    assert r2 is not None and r2.start == dt(10, 6, 15) and r2.defaulted
    r3 = when("明天", default_time=(8, 30))
    assert r3 is not None and r3.start == dt(10, 6, 8, 30)


def test_time_only_already_passed_rolls_to_tomorrow():
    r = when("凌晨三点")
    assert r is not None and r.start == dt(10, 6, 3) and r.rolled
    assert "明天" in r.note()
    r2 = when("零点")
    assert r2 is not None and r2.start == dt(10, 6, 0) and r2.rolled


def test_explicit_past_is_reported_not_silently_moved():
    r = when("今天上午九点")
    assert r is not None and r.start == dt(10, 5, 9)
    assert r.in_past and not r.rolled, "明确说了今天，已经过了就如实说过了，不能偷偷改成明天"
    assert when("昨天下午三点").in_past  # type: ignore[union-attr]
    assert when("2025年1月1日上午十点").in_past  # type: ignore[union-attr]


def test_future_is_not_in_past():
    for text in ("今天下午三点", "明天早上八点", "下周三上午十点", "十分钟后"):
        assert not when(text).in_past, text  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# 时间范围
# --------------------------------------------------------------------------- #


def test_range_with_period():
    r = when("明天下午三点到五点")
    assert r is not None and r.start == dt(10, 6, 15) and r.end == dt(10, 6, 17)


def test_range_without_second_period_inherits():
    r = when("明天上午九点到十一点")
    assert r is not None and r.start == dt(10, 6, 9) and r.end == dt(10, 6, 11)


def test_range_both_unspecified_reads_end_as_later():
    r = when("三点到五点")
    assert r is not None and r.start == dt(10, 5, 15) and r.end == dt(10, 5, 17)


def test_range_dash_and_fullwidth_tilde():
    assert when("明天下午3点-5点").end == dt(10, 6, 17)  # type: ignore[union-attr]
    assert when("明天下午3点～5点").end == dt(10, 6, 17)  # type: ignore[union-attr]


def test_range_spans_cover_the_whole_range():
    text = "明天下午三点到五点开会"
    r = when(text)
    assert r is not None
    assert strip_spans(text, r.spans) == "开会"


def test_no_range_when_single_time():
    assert when("明天下午三点").end is None  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# spans：剔掉它们，剩下的就是标题
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("设置今天下午三点的日程我要玩游戏", "设置的日程我要玩游戏"),
        ("明天早上八点半提醒我开会", "提醒我开会"),
        ("下周三上午十点预约牙医", "预约牙医"),
        ("三天后晚上七点半提醒我给妈妈打电话", "提醒我给妈妈打电话"),
        ("开会，明天下午两点", "开会，"),
        ("十分钟后提醒我关火", "提醒我关火"),
    ],
)
def test_spans_leave_the_title(text: str, rest: str):
    r = when(text)
    assert r is not None
    assert strip_spans(text, r.spans) == rest


def test_strip_spans_handles_unsorted_and_adjacent():
    assert strip_spans("0123456789", [(6, 8), (0, 2)]) == "234589"
    assert strip_spans("abc", []) == "abc"


# --------------------------------------------------------------------------- #
# 宁可不解析，也不挑近似值
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", ["2月30号下午三点", "13月5号下午三点", "2026年2月29日上午十点", "下个月32号九点"])
def test_invalid_dates_are_rejected_not_approximated(text: str):
    assert when(text) is None


@pytest.mark.parametrize("text", ["", "   ", "你好", "打开微信", "把音量调到五十", "下午茶", "早一点出发", "我想吃一点点东西"])
def test_non_time_text_returns_none(text: str):
    assert when(text) is None, f"{text!r} 不该被当成时间"


def test_quantity_after_clock_is_not_minutes():
    """「三点三个人开会」——后面那个三是人数，不是 3 分。"""
    r = when("明天下午三点三个人开会")
    assert r is not None and r.start == dt(10, 6, 15)
    assert strip_spans("明天下午三点三个人开会", r.spans) == "三个人开会"


def test_invalid_minutes_only_take_the_hour():
    r = when("明天下午三点六十分")
    assert r is not None and r.start == dt(10, 6, 15)


def test_bare_weather_sentence_parses_a_day_word():
    """parse_when 本身只管"这句话里有没有时间"。「今天天气怎么样」里有「今天」，
    会不会被当成日程由调用方的**意图判断**负责（见 ScheduleSkill 的触发条件），
    这里把这个事实钉住，免得谁以为解析器自己会拦。"""
    r = when("今天天气怎么样")
    assert r is not None and r.defaulted


# --------------------------------------------------------------------------- #
# 钟点换算表
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("h", "period", "want"),
    [
        (3, "afternoon", 15), (12, "afternoon", 12), (6, "dusk", 18),
        (8, "morning", 8), (12, "dawn", 0), (3, "dawn", 3),
        (1, "noon", 13), (12, "noon", 12), (11, "noon", 11),
        (8, "evening", 20), (11, "night", 23),
        (15, None, 15), (0, None, 0), (13, "morning", 13),
    ],
)
def test_hour24_table(h: int, period: str | None, want: int):
    assert hour24(h, period)[0] == want


def test_hour24_midnight_shifts_the_day():
    assert hour24(12, "evening") == (0, 1, False, "晚上十二点按次日 0 点算")
    assert hour24(2, "evening")[:2] == (2, 1)
    assert hour24(24, None)[:2] == (0, 1)
    assert hour24(12, "midnight")[:2] == (0, 1)


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #


def test_format_when_relative_words():
    assert format_when(dt(10, 5, 15), NOW) == "今天（10月5日 周一）15:00"
    assert format_when(dt(10, 6, 8, 30), NOW) == "明天（10月6日 周二）08:30"
    assert format_when(dt(10, 7, 19), NOW) == "后天（10月7日 周三）19:00"


def test_format_when_far_dates_have_weekday_and_year_if_needed():
    assert format_when(dt(10, 20, 15), NOW) == "10月20日 周二 15:00"
    assert format_when(dt(1, 1, 10, y=2027), NOW) == "2027年1月1日 周五 10:00"


def test_format_delta():
    assert format_delta(timedelta(hours=2, minutes=5)) == "还有 2 小时 5 分钟"
    assert format_delta(timedelta(minutes=-3)) == "已过 3 分钟"
    assert format_delta(timedelta(days=1, hours=3)) == "还有 1 天 3 小时"
    assert format_delta(timedelta(seconds=20)) == "还有 不到 1 分钟"


# --------------------------------------------------------------------------- #
# 默认 now
# --------------------------------------------------------------------------- #


def test_now_defaults_to_the_system_clock():
    r = parse_when("十分钟后")
    assert r is not None
    assert abs((r.start - datetime.now()).total_seconds() - 600) < 5
