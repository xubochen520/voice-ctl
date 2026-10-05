"""事件总线。

这块是「界面能看到日志」的地基，全是并发细节，手工点不出来——必须单测。
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

from voice_ctl import events


def drain(bus: events.EventBus, timeout: float = 1.0) -> None:
    assert bus.flush(timeout), "事件队列没有在超时前排空"


# --------------------------------------------------------------------------- #
# 基本行为
# --------------------------------------------------------------------------- #


def test_emit_assigns_increasing_seq():
    bus = events.EventBus()
    a = bus.emit("info", "one")
    b = bus.emit("warn", "two")
    assert (a.seq, b.seq) == (1, 2)
    assert bus.last_seq == 2


def test_since_returns_only_newer():
    bus = events.EventBus()
    for i in range(5):
        bus.emit("info", f"m{i}")
    got = bus.since(2)
    assert [e.text for e in got] == ["m2", "m3", "m4"]


def test_since_bypasses_sinks_entirely():
    """UI 靠 since() 增量读取，不能依赖后台线程是否跑过。"""
    bus = events.EventBus()
    bus.emit("info", "x")
    assert [e.text for e in bus.since(0)] == ["x"]


def test_unknown_level_falls_back_to_info():
    bus = events.EventBus()
    assert bus.emit("nonsense", "x").level == "info"


def test_ring_buffer_is_bounded_and_counts_drops():
    """常驻几周的进程必须有上限，而且丢了多少要能解释。"""
    bus = events.EventBus(capacity=3)
    for i in range(10):
        bus.emit("info", f"m{i}")
    assert len(bus.snapshot()) == 3
    assert bus.dropped == 7
    assert [e.text for e in bus.snapshot()] == ["m7", "m8", "m9"]


def test_clear_keeps_seq_so_ui_cursor_does_not_rewind():
    """清空缓冲不能把 seq 归零——否则界面的增量游标会倒流并丢事件。"""
    bus = events.EventBus()
    bus.emit("info", "a")
    bus.emit("info", "b")
    seq = bus.last_seq
    bus.clear()
    assert bus.snapshot() == []
    assert bus.last_seq == seq
    bus.emit("info", "c")
    assert [e.text for e in bus.since(seq)] == ["c"]


# --------------------------------------------------------------------------- #
# 订阅
# --------------------------------------------------------------------------- #


def test_sink_receives_events_in_order():
    bus = events.EventBus()
    got: list[str] = []
    bus.subscribe(lambda e: got.append(e.text))
    for i in range(20):
        bus.emit("info", f"m{i}")
    drain(bus)
    assert got == [f"m{i}" for i in range(20)]


def test_emit_does_not_run_sink_inline():
    """emit 必须在微秒级返回：它跑在键盘钩子线程里，钩子超时会被系统摘掉。"""
    bus = events.EventBus()
    release = threading.Event()
    started = threading.Event()

    def slow(_e: events.Event) -> None:
        started.set()
        release.wait(2.0)

    bus.subscribe(slow)
    t0 = time.perf_counter()
    bus.emit("info", "x")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"emit 花了 {elapsed * 1000:.1f}ms，说明 sink 是同步跑的"
    assert started.wait(1.0), "sink 没被后台线程调用"
    release.set()
    drain(bus)


def test_sink_exception_does_not_break_others_or_propagate():
    bus = events.EventBus()
    got: list[str] = []

    def bad(_e: events.Event) -> None:
        raise RuntimeError("sink 炸了")

    bus.subscribe(bad)
    bus.subscribe(lambda e: got.append(e.text))
    bus.emit("info", "still here")  # 不该抛
    drain(bus)
    assert got == ["still here"]


def test_unsubscribe_stops_delivery():
    bus = events.EventBus()
    got: list[str] = []
    off = bus.subscribe(lambda e: got.append(e.text))
    bus.emit("info", "a")
    drain(bus)
    off()
    bus.emit("info", "b")
    drain(bus)
    assert got == ["a"]


def test_sink_that_emits_does_not_deadlock():
    """sink 里再 emit 是很自然的写法，持锁调用就会死锁。"""
    bus = events.EventBus()

    def echo(ev: events.Event) -> None:
        if ev.kind == "app":
            bus.emit("debug", f"echo:{ev.text}", kind="echo")

    bus.subscribe(echo)
    bus.emit("info", "a")
    drain(bus)
    assert [e.text for e in bus.snapshot()] == ["a", "echo:a"]


# --------------------------------------------------------------------------- #
# 控制台 sink
# --------------------------------------------------------------------------- #


def test_console_sink_respects_min_level():
    bus = events.EventBus()
    out = io.StringIO()
    events.attach_console(bus, stream=out, min_level="warn", color=False)
    bus.emit("info", "看不见")
    bus.emit("warn", "看得见")
    drain(bus)
    assert "看不见" not in out.getvalue()
    assert "看得见" in out.getvalue()


def test_console_sink_skips_echoed_stdout_lines():
    """tee 已经把裸输出原样写到控制台了；sink 再打一遍就是重复。"""
    bus = events.EventBus()
    out = io.StringIO()
    events.attach_console(bus, stream=out, min_level="debug", color=False)
    bus.emit("info", "裸行", kind="stdout")
    bus.emit("info", "自己的事件")
    drain(bus)
    body = out.getvalue()
    assert "裸行" not in body
    assert "自己的事件" in body


def test_console_sink_indents_continuation_lines():
    bus = events.EventBus()
    out = io.StringIO()
    events.attach_console(bus, stream=out, min_level="debug", color=False)
    bus.emit("ok", "第一行\n第二行")
    drain(bus)
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 2
    assert lines[1].startswith(" ")
    assert "第二行" in lines[1]


def test_console_sink_survives_dead_stream():
    bus = events.EventBus()

    class Boom(io.StringIO):
        def write(self, _s):  # noqa: ANN001, ANN202
            raise OSError("控制台没了")

    events.attach_console(bus, stream=Boom(), min_level="debug", color=False)
    bus.emit("error", "x")
    drain(bus)  # 不抛就算过


# --------------------------------------------------------------------------- #
# 文件 sink
# --------------------------------------------------------------------------- #


def test_file_sink_writes_and_includes_detail(tmp_path: Path):
    bus = events.EventBus()
    log = tmp_path / "a.log"
    events.attach_file(bus, log)
    bus.emit("ok", "干活了", kind="engine", n=3)
    drain(bus)
    body = log.read_text(encoding="utf-8")
    assert "干活了" in body and "n=3" in body and "[engine]" in body


def test_file_sink_rotates_once(tmp_path: Path):
    bus = events.EventBus()
    log = tmp_path / "a.log"
    events.attach_file(bus, log, max_bytes=200)
    for i in range(400):
        bus.emit("info", f"line-{i}-" + "x" * 40)
    drain(bus, 3.0)
    assert log.is_file()
    assert (tmp_path / "a.log.1").is_file(), "超过上限后应该轮转出一份 .1"


def test_file_sink_creates_parent_dir(tmp_path: Path):
    bus = events.EventBus()
    log = tmp_path / "deep" / "nested" / "a.log"
    events.attach_file(bus, log)
    bus.emit("info", "x")
    drain(bus)
    assert log.is_file()


# --------------------------------------------------------------------------- #
# stdout tee
# --------------------------------------------------------------------------- #


def test_stream_tee_forwards_and_captures():
    raw = io.StringIO()
    bus = events.EventBus()
    tee = events.StreamTee(raw, bus, "stdout")
    tee.write("hello ")
    tee.write("world\n")
    drain(bus)
    assert raw.getvalue() == "hello world\n"
    assert [e.text for e in bus.snapshot()] == ["hello world"]


def test_stream_tee_handles_partial_lines_and_flush():
    raw = io.StringIO()
    bus = events.EventBus()
    tee = events.StreamTee(raw, bus, "stdout")
    tee.write("半行")
    tee.flush()
    drain(bus)
    assert [e.text for e in bus.snapshot()] == ["半行"]


def test_stream_tee_guesses_level_for_markers():
    raw = io.StringIO()
    bus = events.EventBus()
    tee = events.StreamTee(raw, bus, "stdout")
    tee.write("✗ 出错了\n✓ 好了\nplain\n")
    drain(bus)
    assert [e.level for e in bus.snapshot()] == ["error", "ok", "info"]


def test_stream_tee_stderr_is_always_error():
    raw = io.StringIO()
    bus = events.EventBus()
    tee = events.StreamTee(raw, bus, "stderr")
    tee.write("随便一句\n")
    drain(bus)
    assert bus.snapshot()[0].level == "error"


def test_stream_tee_passes_through_unknown_attributes():
    raw = io.StringIO()
    tee = events.StreamTee(raw, events.EventBus(), "stdout")
    assert tee.encoding == raw.encoding
    assert tee.isatty() is False


def test_install_stream_tee_is_idempotent_and_restorable():
    import sys

    before = sys.stdout
    restore = events.install_stream_tee(events.EventBus())
    try:
        assert sys.stdout is not before
        again = events.install_stream_tee(events.EventBus())
        assert again is restore, "重复安装应该返回同一个还原函数"
    finally:
        restore()
    assert sys.stdout is before


# --------------------------------------------------------------------------- #
# 事件本身
# --------------------------------------------------------------------------- #


def test_event_line_has_clock_glyph_and_text():
    ev = events.Event(1, 1700000000.25, "warn", "k", "小心")
    line = ev.line(millis=True)
    assert "⚠" in line and "小心" in line
    assert line.endswith("250 ⚠ 小心")


def test_event_clock_without_millis():
    ev = events.Event(1, 1700000000.25, "info", "k", "x")
    assert "." not in ev.clock()


def test_event_detail_truncates_long_values():
    ev = events.Event(1, 0.0, "info", "k", "x", {"blob": "y" * 500})
    assert len(ev.detail()) < 200


def test_event_detail_empty_when_no_data():
    assert events.Event(1, 0.0, "info", "k", "x").detail() == ""


def test_global_helpers_use_module_bus():
    old = events.get_bus()
    bus = events.EventBus()
    events.set_bus(bus)
    try:
        events.ok("好了")
        events.error("坏了")
        assert [e.level for e in bus.snapshot()] == ["ok", "error"]
    finally:
        events.set_bus(old)
