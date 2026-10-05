"""中文时间表达式解析：「明天下午三点半」「下周三上午十点」「半小时后」→ datetime。

为什么自己写而不用 dateparser/jionlp：

  * 日程口令的语法子集很小（日期词 + 时段 + 钟点），手写 300 行比引入一个带词典的
    大依赖更可控，每条规则都能写测试。
  * ASR 的输出有它自己的毛病，通用库不会替我们处理：**中文数字和阿拉伯数字混着来**
    （实测 `use_itn=true` 下「下午三点」仍是中文数字，别的声音可能是「3点」）、
    没有标点、偶尔丢首字（见 asr.DEFAULT_PAD_MS）。
  * 需要把**用了哪几段文字**报出来（spans），调用方才能把它们剔掉，剩下的就是日程标题。

三条原则：

  1. **不替用户悄悄猜**：没说上午/下午、没说几点、时间已经过了——这些情况都照样给出
     一个最合理的结果，但同时在 `ambiguous / defaulted / rolled / in_past` 里标出来，
     界面会把它们写在确认卡上（「没说上午/下午，按下午算」）。
  2. **只有确定的才返回**：日期不合法（2月30号）、分钟超过 59，直接当没解析出来，
     而不是挑个近似值。
  3. **纯函数**：`now` 由调用方传入，测试和 `simulate --now` 都靠它复现。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# --------------------------------------------------------------------------- #
# 数字
# --------------------------------------------------------------------------- #

_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

NUM = r"(?:\d{1,4}|[零〇一二两三四五六七八九十百]{1,5})"

_FULLWIDTH = str.maketrans("０１２３４５６７８９：／．－～", "0123456789:/.-~")


def to_halfwidth(text: str) -> str:
    """全角数字/冒号换成半角。**逐字符一一对应**，所以不会改变任何下标——
    spans 是按这个字符串算的，调用方拿它去切原文也是对的。"""
    return text.translate(_FULLWIDTH)


def cn2int(s: str) -> int | None:
    """「二十三」「十五」「两」「2026」「二零二六」→ int；不是数字返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if all(ch in _DIGIT for ch in s):
        # 全是数字字：按位读（二零二六 → 2026，三 → 3）
        return int("".join(str(_DIGIT[ch]) for ch in s))
    total, num = 0, 0
    for ch in s:
        if ch in _DIGIT:
            num = _DIGIT[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            return None
    return total + num


# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #

# 「今晚」「明早」这类合成词：(相对今天的天数, 时段)
_COMPOUND = {
    "今晚": (0, "evening"), "今夜": (0, "night"), "今早": (0, "morning"), "今晨": (0, "morning"),
    "明早": (1, "morning"), "明晨": (1, "morning"), "明晚": (1, "evening"),
    "昨晚": (-1, "evening"), "昨夜": (-1, "night"), "昨早": (-1, "morning"),
}
_DAYWORD = {
    "大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明儿": 1,
    "今天": 0, "今日": 0, "今儿": 0, "昨天": -1, "昨日": -1, "前天": -2,
}
_PERIOD = {
    "凌晨": "dawn", "清晨": "morning", "早晨": "morning", "早上": "morning", "早间": "morning",
    "一早": "morning", "上午": "forenoon", "中午": "noon", "午间": "noon", "午后": "afternoon",
    "下午": "afternoon", "傍晚": "dusk", "黄昏": "dusk", "晚上": "evening", "晚间": "evening",
    "夜里": "night", "夜晚": "night", "半夜": "midnight", "深夜": "midnight",
}
# 只说了时段没说几点时的默认钟点
_PERIOD_DEFAULT_HOUR = {
    "dawn": 5, "morning": 8, "forenoon": 9, "noon": 12, "afternoon": 15,
    "dusk": 18, "evening": 20, "night": 21, "midnight": 23,
}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
            "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
WEEKDAY_NAMES = "一二三四五六日"

DEFAULT_TIME = (9, 0)
"""只说了日期、没说几点时用的钟点。"""

# 钟点后面紧跟这些量词时，前面那个数字多半不是「分钟」而是数量（「三点三个人开会」）
_COUNTER = "个位人次只块元号日楼层米克斤条件份杯瓶本张天周月年岁"

_ALT = lambda words: "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))  # noqa: E731

_RE_COMPOUND = re.compile(_ALT(_COMPOUND))
_RE_DAYWORD = re.compile(_ALT(_DAYWORD))
_RE_PERIOD = re.compile(rf"(?P<p>{_ALT(_PERIOD)})(?!茶)")  # 「下午茶」不是时段
_RE_WEEKDAY = re.compile(
    r"(?P<pre>下下个?|下个?|这个?|本|上个?)?(?:周|星期|礼拜)(?P<d>[一二三四五六日天1-7])"
)
_RE_WEEKEND = re.compile(r"(?P<pre>下下个?|下个?|这个?|本|上个?)?周末")
_RE_FULLDATE = re.compile(
    rf"(?P<y>\d{{4}}|[零〇一二三四五六七八九]{{4}})年\s*(?P<mo>{NUM})月\s*(?P<d>{NUM})\s*(?:日|号)?"
)
_RE_ISODATE = re.compile(r"(?P<y>\d{4})[-/.](?P<mo>\d{1,2})[-/.](?P<d>\d{1,2})")
_RE_MONTHDAY = re.compile(rf"(?P<mo>{NUM})月\s*(?P<d>{NUM})\s*(?:日|号)")
_RE_NEXTMONTHDAY = re.compile(rf"(?P<pre>下个?月|这个?月|本月)\s*(?P<d>{NUM})\s*(?:日|号)")
_RE_MONTHEND = re.compile(r"(?:(?P<pre>下个?月|这个?月|本月)(?:底|末|尾)|(?P<bare>月底|月末|月尾))")
_RE_DAYONLY = re.compile(rf"(?<![月年\d])(?P<d>{NUM})\s*(?:号|日)(?!程|期|常|记|志|线|楼|院|门|口|店|车|座|层)")
_RE_OFFSET = re.compile(
    rf"(?P<n>{NUM})\s*个?\s*(?P<u>天|日|周|星期|礼拜|月|年)\s*(?:以后|之后|后)"
)
_RE_CLOCK = re.compile(
    rf"(?P<h>{NUM})\s*(?:点钟|点整|点|时|:)\s*"
    rf"(?P<m>一刻|三刻|半|整|{NUM}(?:分钟|分)?(?![{_COUNTER}]))?"
)
_RE_RANGE_TAIL = re.compile(rf"\s*(?:到|至|-|~)\s*(?P<p>{_ALT(_PERIOD)})?\s*")

# 相对时长：「十分钟后」「半小时后」「一个半小时后」「两小时十分钟后」
_RE_REL_HALFH = re.compile(rf"(?P<h>{NUM})\s*个半\s*(?:小时|钟头)\s*(?:以后|之后|后)")
_RE_REL_HALF = re.compile(r"半\s*个?\s*(?:小时|钟头)\s*(?:以后|之后|后)")
_RE_REL_HM = re.compile(
    rf"(?P<h>{NUM})\s*个?\s*(?:小时|钟头)\s*(?:零|又)?\s*(?P<m>半|{NUM}\s*(?:分钟|分)?)?\s*(?:以后|之后|后)"
)
_RE_REL_M = re.compile(rf"(?P<m>{NUM})\s*(?:分钟|分)\s*(?:以后|之后|后)")
_RE_REL_S = re.compile(rf"(?P<s>{NUM})\s*(?:秒钟|秒)\s*(?:以后|之后|后)")
# 没有「后」的倒计时：「设置一个十分钟的倒计时」
_RE_TIMER_HALFH = re.compile(
    rf"(?P<h>{NUM})\s*个半\s*(?:小时|钟头)\s*(?:的)?\s*(?:倒计时|定时器|计时器|计时)"
)
_RE_TIMER = re.compile(
    rf"(?P<n>{NUM}|半)\s*个?\s*(?P<u>小时|钟头|分钟|分|秒钟|秒)\s*(?:的)?\s*(?:倒计时|定时器|计时器|计时)"
)


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #


@dataclass
class TimeSpan:
    start: datetime
    spans: list[tuple[int, int]] = field(default_factory=list)
    """用到的那几段文字在输入里的 [i, j)。剔掉它们，剩下的就是标题。"""

    end: datetime | None = None
    has_date: bool = False
    has_clock: bool = False
    ambiguous: bool = False
    """没说上午/下午，是按常识猜的。"""
    defaulted: bool = False
    """没说几点，用了默认时刻。"""
    rolled: bool = False
    """没说日期，今天这个钟点已经过了，滚到了明天。"""
    in_past: bool = False
    """明确说的日期/时间，但已经过去了。"""
    relative: bool = False
    """「N 分钟后」这类相对现在的时间。"""
    timer: bool = False
    """倒计时（「十分钟的倒计时」），不是日历上的某个时刻。"""
    notes: list[str] = field(default_factory=list)

    def note(self) -> str:
        return "；".join(self.notes)


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """把 spans 对应的文字从 text 里剔掉（先按位置从后往前删，下标才不会错位）。"""
    out = text
    for i, j in sorted(spans, reverse=True):
        out = out[:i] + out[j:]
    return out


# --------------------------------------------------------------------------- #
# 钟点换算
# --------------------------------------------------------------------------- #


def _minute(token: str | None) -> int | None:
    """「半」「一刻」「三刻」「整」「十五分」「5」→ 分钟；None 表示没有；-1 表示非法。"""
    if not token:
        return None
    t = token.strip()
    if t == "半":
        return 30
    if t == "一刻":
        return 15
    if t == "三刻":
        return 45
    if t == "整":
        return 0
    t = re.sub(r"(分钟|分)$", "", t).strip()
    v = cn2int(t)
    if v is None or not 0 <= v <= 59:
        return -1
    return v


def hour24(h: int, period: str | None, *, alarm: bool = False) -> tuple[int, int, bool, str]:
    """口语里的钟点 → (24 小时制的时, 要额外加的天数, 是否是猜的, 说明)。

    h 是嘴上说的数字（0-24）。13 以上和 0 本来就是 24 小时制，不看时段。
    """
    if h == 24:
        return 0, 1, False, ""
    if h >= 13 or h == 0:
        return h, 0, False, ""

    if period is None:
        if h == 12:
            return 12, 0, True, "没说上午/下午，十二点按中午算"
        if 1 <= h <= 6 and not alarm:
            return h + 12, 0, True, "没说上午/下午，按下午算"
        return h, 0, True, "没说上午/下午，按上午算"

    if period == "dawn":
        return (0 if h == 12 else h), 0, False, ""
    if period in ("morning", "forenoon"):
        return h, 0, False, ""
    if period == "noon":
        if 1 <= h <= 4:
            return h + 12, 0, False, ""
        return h, 0, False, ""
    if period in ("afternoon", "dusk"):
        return (h + 12 if h < 12 else 12), 0, False, ""
    if period in ("evening", "night"):
        if h == 12:
            return 0, 1, False, "晚上十二点按次日 0 点算"
        if h <= 5:
            return h, 1, True, "晚上这个钟点按次日凌晨算"
        return h + 12, 0, False, ""
    # midnight：半夜/深夜
    if h == 12:
        return 0, 1, False, "半夜十二点按次日 0 点算"
    if h <= 5:
        return h, 1, False, "按次日凌晨算"
    return (h + 12 if h < 12 else h), 0, False, ""


# --------------------------------------------------------------------------- #
# 日期
# --------------------------------------------------------------------------- #


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _valid_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _weekday_date(prefix: str | None, target: int, today: date) -> tuple[date, bool]:
    """返回 (日期, 是不是「裸的周X」——需要看钟点决定今天还是下周)。"""
    pre = (prefix or "").strip()
    if pre.startswith("下下"):
        return _monday(today) + timedelta(days=14 + target), False
    if pre.startswith("下"):
        return _monday(today) + timedelta(days=7 + target), False
    if pre.startswith("上"):
        return _monday(today) + timedelta(days=target - 7), False
    if pre:  # 这/这个/本
        return _monday(today) + timedelta(days=target), False
    # 裸的「周三」：今天起最近的那个周三
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead), True


class _Pick:
    """已经被占用的文字区间。同一段字不能被两个成分重复使用。"""

    def __init__(self) -> None:
        self.spans: list[tuple[int, int]] = []

    def free(self, i: int, j: int) -> bool:
        return all(j <= a or i >= b for a, b in self.spans)

    def take(self, i: int, j: int) -> None:
        self.spans.append((i, j))


def _first_free(rx: re.Pattern[str], text: str, pick: _Pick):  # noqa: ANN202
    for m in rx.finditer(text):
        if pick.free(*m.span()):
            return m
    return None


# --------------------------------------------------------------------------- #
# 相对时长
# --------------------------------------------------------------------------- #


def _parse_relative(text: str, now: datetime) -> TimeSpan | None:
    delta: timedelta | None = None
    span: tuple[int, int] | None = None
    timer = False

    m = _RE_REL_HALFH.search(text)
    if m and (h := cn2int(m["h"])) is not None:
        delta, span = timedelta(hours=h, minutes=30), m.span()
    if delta is None and (m := _RE_REL_HALF.search(text)):
        delta, span = timedelta(minutes=30), m.span()
    if delta is None and (m := _RE_REL_HM.search(text)):
        h = cn2int(m["h"])
        mm = _minute(m["m"]) if m["m"] else 0
        if h is not None and mm != -1:
            delta, span = timedelta(hours=h, minutes=mm or 0), m.span()
    if delta is None and (m := _RE_REL_M.search(text)):
        v = cn2int(m["m"])
        if v is not None:
            delta, span = timedelta(minutes=v), m.span()
    if delta is None and (m := _RE_REL_S.search(text)):
        v = cn2int(m["s"])
        if v is not None:
            delta, span = timedelta(seconds=v), m.span()
    if delta is None and (m := _RE_TIMER_HALFH.search(text)):
        h = cn2int(m["h"])
        if h is not None:
            delta, span, timer = timedelta(hours=h, minutes=30), m.span(), True
    if delta is None and (m := _RE_TIMER.search(text)):
        n = 0.5 if m["n"] == "半" else cn2int(m["n"])
        if n is not None:
            unit = m["u"]
            if unit in ("小时", "钟头"):
                delta = timedelta(hours=n)
            elif unit in ("分钟", "分"):
                delta = timedelta(minutes=n)
            else:
                delta = timedelta(seconds=n)
            span, timer = m.span(), True

    if delta is None or span is None or delta <= timedelta(0):
        return None
    start = (now + delta).replace(microsecond=0)
    return TimeSpan(start=start, spans=[span], relative=True, timer=timer,
                    has_clock=True, has_date=False)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def parse_when(
    text: str,
    now: datetime | None = None,
    *,
    alarm: bool = False,
    default_time: tuple[int, int] = DEFAULT_TIME,
) -> TimeSpan | None:
    """在 text 里找第一个时间表达式。找不到（或不合法）返回 None。

    `alarm`：这句话是闹钟/叫醒语义（「叫我起床」）。没说上午下午的 1-6 点按
    上午算——「明天五点叫我起床」没人想要下午五点被叫醒。
    """
    now = (now or datetime.now()).replace(microsecond=0)
    t = to_halfwidth(text or "")
    if not t.strip():
        return None

    rel = _parse_relative(t, now)
    if rel is not None:
        return rel

    today = now.date()
    pick = _Pick()
    base: date | None = None
    bare_weekday = False
    has_date = False
    invalid_date = False
    """匹配到了日期的写法，但日期本身不存在（2 月 30 号）。"""
    period: str | None = None

    # ---- 日期成分：按"信息量从大到小"取第一个 --------------------------------
    def try_date() -> None:
        nonlocal base, bare_weekday, has_date, invalid_date, period
        for rx, kind in (
            (_RE_FULLDATE, "full"), (_RE_ISODATE, "iso"), (_RE_NEXTMONTHDAY, "nextmd"),
            (_RE_MONTHDAY, "md"), (_RE_MONTHEND, "monthend"), (_RE_OFFSET, "offset"),
            (_RE_COMPOUND, "compound"), (_RE_DAYWORD, "dayword"),
            (_RE_WEEKDAY, "weekday"), (_RE_WEEKEND, "weekend"), (_RE_DAYONLY, "dayonly"),
        ):
            m = _first_free(rx, t, pick)
            if m is None:
                continue
            got: date | None = None
            if kind in ("full", "iso"):
                y = cn2int(m["y"]); mo = cn2int(m["mo"]); d = cn2int(m["d"])
                got = _valid_date(y, mo, d) if None not in (y, mo, d) else None
            elif kind == "md":
                mo = cn2int(m["mo"]); d = cn2int(m["d"])
                if mo and d:
                    got = _valid_date(today.year, mo, d)
                    if got is not None and got < today:
                        got = _valid_date(today.year + 1, mo, d)
            elif kind == "nextmd":
                d = cn2int(m["d"])
                ref = _add_months(today.replace(day=1), 1 if m["pre"].startswith("下") else 0)
                got = _valid_date(ref.year, ref.month, d) if d else None
            elif kind == "monthend":
                nxt = bool(m["pre"] and m["pre"].startswith("下"))
                ref = _add_months(today.replace(day=1), 1 if nxt else 0)
                got = date(ref.year, ref.month, calendar.monthrange(ref.year, ref.month)[1])
            elif kind == "offset":
                n = cn2int(m["n"]); u = m["u"]
                if n is not None and n > 0:
                    if u in ("天", "日"):
                        got = today + timedelta(days=n)
                    elif u in ("周", "星期", "礼拜"):
                        got = today + timedelta(days=7 * n)
                    elif u == "月":
                        got = _add_months(today, n)
                    elif u == "年":
                        got = _add_months(today, 12 * n)
            elif kind == "compound":
                off, per = _COMPOUND[m.group(0)]
                got = today + timedelta(days=off)
                if period is None:
                    period = per
            elif kind == "dayword":
                got = today + timedelta(days=_DAYWORD[m.group(0)])
            elif kind == "weekday":
                got, bare_weekday = _weekday_date(m["pre"], _WEEKDAY[m["d"]], today)
            elif kind == "weekend":
                pre = (m["pre"] or "").strip()
                sat, _ = _weekday_date(pre or None, 5, today)
                if not pre and today.weekday() >= 5:
                    sat = today
                got = sat
            elif kind == "dayonly":
                d = cn2int(m["d"])
                if d and 1 <= d <= 31:
                    for k in range(0, 4):
                        ref = _add_months(today.replace(day=1), k)
                        cand = _valid_date(ref.year, ref.month, d)
                        if cand is not None and cand >= today:
                            got = cand
                            break
            if got is None:
                invalid_date = True
                continue  # 不合法（2 月 30 号）：不挑近似值，换下一类看看有没有别的写法
            base, has_date = got, True
            pick.take(*m.span())
            return

    try_date()
    if base is None and invalid_date:
        # 用户明确说了一个日期，但它不存在。这时只认钟点会把「2月30号下午三点」
        # 变成「今天下午三点」——错得很合理，所以宁可整个不解析
        return None

    # ---- 时段 ---------------------------------------------------------------
    pm = _first_free(_RE_PERIOD, t, pick)
    if pm is not None:
        period = _PERIOD[pm["p"]]
        pick.take(*pm.span())

    # ---- 钟点 ---------------------------------------------------------------
    clock = None
    for m in _RE_CLOCK.finditer(t):
        if not pick.free(*m.span()):
            continue
        if m["h"] == "一" and (t[m.start() - 1:m.start()] in set("早晚快慢多少大小高低好再稍轻重远近长短")
                               or t[m.end():m.end() + 1] in ("点", "儿")):
            continue  # 「早一点」「一点点」不是 1 点
        h = cn2int(m["h"])
        if h is None or h > 24:
            continue
        mm = _minute(m["m"]) if m["m"] else 0
        if mm == -1:
            mm = 0
            # 分钟非法：只认钟点，把非法的那截留给标题
            m_span = (m.start(), m.start("m")) if m["m"] else m.span()
        else:
            m_span = m.span()
        clock = (h, mm or 0, m_span, m)
        break

    if clock is None and period is None and base is None:
        return None
    # 只有时段（「下午开会」）会走到下面的默认钟点：今天下午 15:00

    has_clock = clock is not None
    notes: list[str] = []
    ambiguous = defaulted = rolled = False
    day_shift = 0

    if clock is not None:
        h, mi, span, _m = clock
        pick.take(*span)
        hh, day_shift, amb, note = hour24(h, period, alarm=alarm)
        ambiguous = amb
        if note:
            notes.append(note)
    else:
        if period is not None:
            hh, mi = _PERIOD_DEFAULT_HOUR[period], 0
        else:
            hh, mi = default_time
        defaulted = True
        notes.append(f"没说几点，按 {hh:02d}:{mi:02d} 算")

    day = base if base is not None else today
    start = datetime.combine(day, time(hh, mi)) + timedelta(days=day_shift)

    # 裸的「周三」：今天就是周三但钟点已过 → 指下周三
    if bare_weekday and base == today and start < now:
        start += timedelta(days=7)

    # 没说上午/下午又已经过了：先试另一半天（「今天七点」现在 10:12，早上七点早过了，
    # 指的多半是晚上七点）。只在同一天内换，换了还是过去就不换。
    if start < now and ambiguous and period is None:
        for alt in (start + timedelta(hours=12), start - timedelta(hours=12)):
            if alt >= now and alt.date() == start.date():
                start = alt
                notes.append("这个钟点今天已过，按另一半天算")
                break

    in_past = False
    if start < now:
        if not has_date and has_clock:
            # 只说了钟点：滚到明天的这个钟点
            start += timedelta(days=1)
            rolled = True
            notes.append("今天这个时间已经过了，按明天算")
        else:
            in_past = True
            notes.append("这个时间已经过去了")

    result = TimeSpan(
        start=start, spans=sorted(pick.spans), has_date=has_date, has_clock=has_clock,
        ambiguous=ambiguous, defaulted=defaulted, rolled=rolled, in_past=in_past, notes=notes,
    )

    # ---- 结束时间：「下午三点到五点」 ------------------------------------------
    if clock is not None:
        tail = _RE_RANGE_TAIL.match(t, clock[2][1])
        if tail:
            m2 = _RE_CLOCK.match(t, tail.end())
            if m2 and pick.free(*m2.span()):
                h2 = cn2int(m2["h"])
                m2m = _minute(m2["m"]) if m2["m"] else 0
                if h2 is not None and h2 <= 24 and m2m != -1:
                    per2 = _PERIOD.get(tail["p"]) if tail["p"] else period
                    eh, eshift, _a, _n = hour24(h2, per2, alarm=alarm)
                    end = datetime.combine(start.date() - timedelta(days=day_shift), time(eh, m2m or 0)) \
                        + timedelta(days=eshift)
                    if end <= start and per2 == period and h2 < 12 and eh + 12 < 24:
                        end += timedelta(hours=12)  # 「三点到五点」两个都没说时段：五点按下午
                    if end <= start:
                        end += timedelta(days=1)
                    result.end = end
                    result.spans = sorted([*result.spans, (tail.start(), m2.end())])
    return result


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #


def format_when(dt: datetime, now: datetime | None = None) -> str:
    """给确认卡/日志看的写法：「今天（10月5日 周一）15:00」。

    日期和星期都写出来：用户要核对的恰恰是「我说的周三到底是几号」。
    """
    now = now or datetime.now()
    delta = (dt.date() - now.date()).days
    rel = {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get(delta, "")
    wd = "周" + WEEKDAY_NAMES[dt.weekday()]
    stamp = f"{dt.month}月{dt.day}日 {wd}"
    clock = dt.strftime("%H:%M")
    if rel:
        return f"{rel}（{stamp}）{clock}"
    if dt.year != now.year:
        stamp = f"{dt.year}年{stamp}"
    return f"{stamp} {clock}"


def format_delta(delta: timedelta) -> str:
    """「还有 2 小时 5 分钟」「已过 3 分钟」。"""
    secs = int(delta.total_seconds())
    past = secs < 0
    secs = abs(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts: list[str] = []
    if d:
        parts.append(f"{d} 天")
    if h:
        parts.append(f"{h} 小时")
    if m and not d:
        parts.append(f"{m} 分钟")
    if not parts:
        parts.append("不到 1 分钟")
    return ("已过 " if past else "还有 ") + " ".join(parts)


__all__ = [
    "DEFAULT_TIME",
    "TimeSpan",
    "cn2int",
    "format_delta",
    "format_when",
    "hour24",
    "parse_when",
    "strip_spans",
    "to_halfwidth",
]
