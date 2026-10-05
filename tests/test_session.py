"""会话状态机测试。

这批测试针对的是**并发路径**：pynput 监听线程调 press/release，主循环调
timer.tick() → timeout()。这些交错在手工测试里几乎撞不到，但一旦出错
表现就是"偶尔卡住不响应"或"录到一半崩"，极难排查。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from voice_ctl.session import SessionController, SessionStats


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #


class RecorderError(RuntimeError):
    """测试用的 Recorder 异常类型。"""


@dataclass
class FakeRecording:
    duration_s: float = 1.0
    peak: float = 0.2
    rms: float = 0.02

    def gate_reason(self, min_peak: float) -> str | None:
        if min_peak > 0 and self.peak < min_peak:
            return f"静音（peak {self.peak:.4f} < {min_peak}）"
        return None


@dataclass
class FakeRecorder:
    result: FakeRecording = field(default_factory=FakeRecording)
    fail_on_start: bool = False
    fail_on_stop: bool = False
    starts: int = 0
    stops: int = 0
    aborts: int = 0
    last_at: float | None = None

    def start(self, at: float | None = None) -> None:
        if self.fail_on_start:
            raise RecorderError("假装打不开设备")
        self.starts += 1
        self.last_at = at

    def stop(self) -> FakeRecording:
        if self.fail_on_stop:
            raise RecorderError("假装停不下来")
        self.stops += 1
        return self.result

    def abort(self) -> None:
        self.aborts += 1


@dataclass
class FakeTimer:
    armed: int = 0
    disarmed: int = 0

    def arm(self) -> None:
        self.armed += 1

    def disarm(self) -> None:
        self.disarmed += 1


@dataclass
class Spy:
    started: int = 0
    ended: int = 0
    ready: list = field(default_factory=list)
    too_short: list = field(default_factory=list)
    gated: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def build(**kw) -> tuple[SessionController, FakeRecorder, FakeTimer, Spy]:
    kw.setdefault("result", FakeRecording())
    rec = FakeRecorder(**{k: v for k, v in kw.items() if k in FakeRecorder.__dataclass_fields__})
    timer = FakeTimer()
    spy = Spy()
    c = SessionController(
        recorder=rec,
        timer=timer,
        on_recording_start=lambda: setattr(spy, "started", spy.started + 1),
        on_recording_end=lambda: setattr(spy, "ended", spy.ended + 1),
        on_recording_ready=spy.ready.append,
        on_too_short=spy.too_short.append,
        on_gated=spy.gated.append,
        on_error=spy.errors.append,
        min_duration_ms=kw.get("min_duration_ms", 200),
        min_peak=kw.get("min_peak", 0.01),
    )
    return c, rec, timer, spy


# --------------------------------------------------------------------------- #
# 正常流程
# --------------------------------------------------------------------------- #


def test_normal_press_release():
    c, rec, timer, spy = build()
    c.press()
    assert c.recording
    assert rec.starts == 1 and timer.armed == 1 and spy.started == 1
    c.release()
    assert not c.recording
    assert rec.stops == 1 and timer.disarmed == 1 and spy.ended == 1
    assert len(spy.ready) == 1
    assert c.stats.pressed == 1 and c.stats.finished == 1


def test_multiple_cycles():
    c, rec, _, spy = build()
    for _ in range(5):
        c.press()
        c.release()
    assert rec.starts == 5 and rec.stops == 5
    assert len(spy.ready) == 5


def test_release_without_press_is_noop():
    c, rec, timer, spy = build()
    c.release()
    assert rec.stops == 0
    assert timer.disarmed == 0, "没录音时不该去 disarm"
    assert spy.ended == 0


# --------------------------------------------------------------------------- #
# 重复按下 / 重复松开
# --------------------------------------------------------------------------- #


def test_repeated_press_is_ignored():
    """键盘自动重复会连发按下事件，绝不能重复开录音。"""
    c, rec, timer, spy = build()
    c.press()
    c.press()
    c.press()
    assert rec.starts == 1, "重复按下不该重复 start"
    assert timer.armed == 1
    assert c.stats.pressed == 1


def test_repeated_release_stops_only_once():
    """pynput 有时会重复投递松开事件。"""
    c, rec, _, spy = build()
    c.press()
    c.release()
    c.release()
    c.release()
    assert rec.stops == 1
    assert len(spy.ready) == 1
    assert spy.ended == 1


def test_press_after_release_works():
    """收尾后必须能重新开始（状态没被卡死）。"""
    c, rec, _, spy = build()
    c.press()
    c.release()
    c.press()
    assert c.recording
    c.release()
    assert rec.starts == 2 and rec.stops == 2
    assert len(spy.ready) == 2


# --------------------------------------------------------------------------- #
# 超时与松开的竞争
# --------------------------------------------------------------------------- #


def test_timeout_finishes_recording():
    c, rec, _, spy = build()
    c.press()
    c.timeout()
    assert not c.recording
    assert rec.stops == 1
    assert c.stats.timeouts == 1
    assert len(spy.ready) == 1, "超时收尾拿到的也是有效录音，应当处理"


def test_timeout_then_release_does_not_double_stop():
    """超时收尾后用户才松手 —— 不能再 stop 一次。"""
    c, rec, _, spy = build()
    c.press()
    c.timeout()
    c.release()
    assert rec.stops == 1, "重复收尾了"
    assert len(spy.ready) == 1


def test_release_then_timeout_does_not_double_stop():
    """反过来：用户松手后超时才到。"""
    c, rec, _, spy = build()
    c.press()
    c.release()
    c.timeout()
    assert rec.stops == 1
    assert c.stats.timeouts == 0, "已经收尾就不算超时"
    assert len(spy.ready) == 1


def test_timeout_when_idle_is_noop():
    c, rec, _, _ = build()
    c.timeout()
    assert rec.stops == 0
    assert c.stats.timeouts == 0


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #


def test_start_failure_resets_state():
    """start 失败后必须复位，否则会永远卡在"以为在录音"。"""
    c, rec, timer, spy = build(fail_on_start=True)
    c.press()
    assert not c.recording, "start 失败却仍标记为录音中"
    assert rec.starts == 0
    assert timer.armed == 0, "start 失败不该 arm 计时器"
    assert c.stats.start_failed == 1
    assert spy.errors and "打开录音设备失败" in spy.errors[0]

    # 修好后应当能正常开始
    rec.fail_on_start = False
    c.press()
    assert c.recording
    assert rec.starts == 1


def test_stop_failure_does_not_lock_state():
    c, rec, _, spy = build(fail_on_stop=True)
    c.press()
    c.release()
    assert not c.recording, "stop 失败也必须复位状态"
    assert spy.errors and "停止录音失败" in spy.errors[0]
    # 后续仍可用
    rec.fail_on_stop = False
    c.press()
    c.release()
    assert len(spy.ready) == 1


# --------------------------------------------------------------------------- #
# 过滤
# --------------------------------------------------------------------------- #


def test_too_short_is_filtered():
    c, rec, _, spy = build(result=FakeRecording(duration_s=0.05))
    c.press()
    c.release()
    assert len(spy.too_short) == 1
    assert spy.ready == []
    assert c.stats.too_short == 1
    assert c.stats.finished == 0, "被过滤的不算完成"


def test_gated_silence_is_filtered():
    c, rec, _, spy = build(result=FakeRecording(duration_s=1.0, peak=0.001))
    c.press()
    c.release()
    assert len(spy.gated) == 1
    assert "静音" in spy.gated[0]
    assert spy.ready == []
    assert c.stats.gated == 1


def test_boundary_duration_is_accepted():
    """正好等于最短时长应当放行（边界要包含，否则用户会觉得"有时候不灵"）。"""
    c, _, _, spy = build(result=FakeRecording(duration_s=0.2), min_duration_ms=200)
    c.press()
    c.release()
    assert len(spy.ready) == 1


def test_min_peak_zero_disables_gate():
    c, _, _, spy = build(result=FakeRecording(peak=0.0), min_peak=0.0)
    c.press()
    c.release()
    assert len(spy.ready) == 1


# --------------------------------------------------------------------------- #
# abort
# --------------------------------------------------------------------------- #


def test_abort_during_recording():
    c, rec, timer, _ = build()
    c.press()
    c.abort()
    assert not c.recording
    assert rec.aborts == 1
    assert timer.disarmed == 1


def test_abort_when_idle_is_noop():
    c, rec, _, _ = build()
    c.abort()
    assert rec.aborts == 0


def test_abort_then_press_works():
    c, rec, _, spy = build()
    c.press()
    c.abort()
    c.press()
    c.release()
    assert rec.starts == 2
    assert len(spy.ready) == 1


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def test_stats_accumulate():
    c, _, _, spy = build()
    c.press(); c.release()               # 第 1 次
    c.press(); c.release()               # 第 2 次
    c.press(); c.release(); c.release()  # 第 3 次，末尾多一个 no-op 的松开
    assert c.stats.pressed == 3
    assert c.stats.finished == 3
    assert c.stats.processed == 3
    assert len(spy.ready) == 3
    assert "按下 3" in c.stats.summary()
    assert "完成 3" in c.stats.summary()


def test_stats_default_values():
    s = SessionStats()
    assert s.pressed == 0 and s.finished == 0
    assert isinstance(s.summary(), str)


def test_error_type_is_configurable():
    """recorder_error 让调用方能收窄异常类型（比如只捕 RecorderError）。"""
    class Other(Exception):
        pass

    rec = FakeRecorder()
    c = SessionController(
        recorder=rec, timer=FakeTimer(), min_duration_ms=0, min_peak=0.0,
        recorder_error=Other,
    )
    c.press()
    c.release()
    assert len(c.stats.__dict__) > 0  # 不抛异常即可


@pytest.mark.parametrize("duration_ms,expect_ready", [(199, False), (200, True), (5000, True)])
def test_duration_boundaries(duration_ms: int, expect_ready: bool):
    c, _, _, spy = build(result=FakeRecording(duration_s=duration_ms / 1000), min_duration_ms=200)
    c.press()
    c.release()
    assert (len(spy.ready) == 1) is expect_ready


# --------------------------------------------------------------------------- #
# capture_async：开流丢后台线程（钩子线程不能等 300ms）
# --------------------------------------------------------------------------- #


class SlowRecorder(FakeRecorder):
    """start() 会卡住，模拟冷开流那 300ms。"""

    def __init__(self, delay: float = 0.15, **kw) -> None:  # noqa: ANN003
        super().__init__(**kw)
        self.delay = delay
        self.entered = threading.Event()
        self.release_gate = threading.Event()

    def start(self, at: float | None = None) -> None:
        self.entered.set()
        self.release_gate.wait(timeout=2.0)
        super().start(at)


def build_async(rec=None, **kw):  # noqa: ANN001, ANN202
    kw.setdefault("result", FakeRecording())
    rec = rec or FakeRecorder()
    timer = FakeTimer()
    spy = Spy()
    c = SessionController(
        recorder=rec,
        timer=timer,
        on_recording_start=lambda: setattr(spy, "started", spy.started + 1),
        on_recording_end=lambda: setattr(spy, "ended", spy.ended + 1),
        on_recording_ready=spy.ready.append,
        on_too_short=spy.too_short.append,
        on_gated=spy.gated.append,
        on_error=spy.errors.append,
        min_duration_ms=kw.get("min_duration_ms", 0),
        min_peak=kw.get("min_peak", 0.0),
        capture_async=True,
    )
    return c, rec, timer, spy


def _wait(pred, timeout: float = 2.0) -> bool:  # noqa: ANN001
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_async_press_returns_before_the_stream_is_open():
    """press() 必须立刻返回——它跑在键盘钩子线程里，钩子只有 300ms 预算，超了
    Windows 会悄悄摘钩子（症状：按几次之后热键忽然没反应，日志上还看不到）。"""
    rec = SlowRecorder()
    c, _, _, spy = build_async(rec)
    t = time.perf_counter()
    c.press()
    call_ms = (time.perf_counter() - t) * 1000
    assert call_ms < 30, f"press 花了 {call_ms:.1f}ms，必须几乎为零"
    assert c.recording, "状态要立刻翻转，否则重复按下拦不住"
    assert rec.entered.wait(1.0), "后台线程应当去开流了"
    rec.release_gate.set()
    assert _wait(lambda: spy.started == 1)
    c.release()
    assert _wait(lambda: len(spy.ready) == 1)


def test_async_release_before_stream_opens_still_delivers_the_recording():
    """手快的人：流还没开好就松开了。

    这段录音不能丢——开头本来就在预滚缓冲里。以前这条路径会直接什么都不做。
    """
    rec = SlowRecorder()
    c, _, _, spy = build_async(rec)
    c.press()
    assert rec.entered.wait(1.0)
    c.release()  # 开流还没返回就松手
    rec.release_gate.set()
    assert _wait(lambda: len(spy.ready) == 1), "录音必须照常交出去"
    assert rec.stops == 1
    assert spy.errors == [], f"不该报错：{spy.errors}"
    assert not c.recording


def test_async_release_before_start_never_leaves_the_mic_on():
    """松开早于开流完成时，后台线程不能把状态又写成"在录"。"""
    rec = SlowRecorder()
    c, _, _, _ = build_async(rec)
    c.press()
    assert rec.entered.wait(1.0)
    c.release()
    rec.release_gate.set()
    assert _wait(lambda: not c.recording)
    time.sleep(0.05)
    assert not c.recording, "后台线程跑完后不能复活这次会话"


def test_async_repeat_press_is_still_ignored():
    rec = SlowRecorder()
    c, _, _, spy = build_async(rec)
    c.press()
    c.press()  # 键盘自动重复
    c.press()
    rec.release_gate.set()
    assert _wait(lambda: spy.started == 1)
    c.release()
    assert _wait(lambda: len(spy.ready) == 1)
    assert rec.starts == 1
    assert c.stats.pressed == 1


def test_async_start_failure_resets_state():
    rec = SlowRecorder(fail_on_start=True)
    c, _, _, spy = build_async(rec)
    rec.release_gate.set()
    c.press()
    assert _wait(lambda: spy.errors), "开流失败要报出来"
    assert _wait(lambda: not c.recording), "失败后必须复位，不能卡在'以为在录'"
    c.release()  # 松手不该再引发一次收尾
    assert rec.stops == 0
    assert c.stats.start_failed == 1


def test_async_abort_while_opening_stops_the_capture():
    rec = SlowRecorder()
    c, _, _, spy = build_async(rec)
    c.press()
    assert rec.entered.wait(1.0)
    c.abort()
    rec.release_gate.set()
    assert _wait(lambda: rec.aborts == 1)
    time.sleep(0.05)
    assert len(spy.ready) == 0, "放弃的会话不该交出去"


def test_async_release_timing_sweep_never_loses_the_recording():
    """松手时机扫一遍：开流前 / 开流中 / 开流后，一次都不能把录音丢掉。

    这条是回归测试。曾经丢过一次，而且是**静默**丢的：release 先把
    _recording 置 False 并把 capturing 读成 False 提前返回，开流线程随后才把
    capturing 置 True，于是没有任何一方去收尾——录音凭空消失，日志上只有
    "录音中" 没有下文。分三次读同一组状态就会这样，所以现在进录音态和读
    "是否已松手"必须在同一把锁里一次读完。
    """
    for i in range(60):
        rec = SlowRecorder(delay=0.005)
        c, _, _, spy = build_async(rec)
        c.press()
        if i % 3 == 0:
            pass  # 立刻松手：开流还没开始
        elif i % 3 == 1:
            assert rec.entered.wait(1.0)
        else:
            time.sleep(0.02)  # 等开流完成
        c.release()
        rec.release_gate.set()
        assert _wait(lambda: bool(spy.ready or spy.errors), timeout=1.0), (
            f"第 {i} 轮既没交付也没报错，录音被静默丢弃"
        )
        assert len(spy.ready) == 1, f"第 {i} 轮丢了：errors={spy.errors}"
        assert spy.errors == [], f"第 {i} 轮报错：{spy.errors}"
        assert not c.recording


def test_async_press_passes_the_keypress_timestamp():

    """开流晚了，时长基准要拨回按下那一刻，否则报出来的时长比实际按住时间短。"""
    c, rec, _, _ = build_async()
    before = time.perf_counter()
    c.press()
    assert _wait(lambda: rec.last_at is not None)
    assert rec.last_at is not None
    assert before <= rec.last_at <= time.perf_counter()
