"""日程存储 / .ics 导出 / 到点提醒。

重点不是"能存能取"，而是三条数据安全底线：
  * JSON 坏了不能当成空文件覆盖掉用户的日程
  * 提醒最多响一次（先落盘再弹窗）
  * 睡眠/关机之后：错过的补响并标明，太久以前的不翻旧账
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from voice_ctl.schedule import (
    Event,
    ReminderService,
    ScheduleStore,
    default_store_path,
    to_ics,
    write_ics,
)

T0 = datetime(2026, 10, 5, 10, 0)


@pytest.fixture()
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedule.json", clock=lambda: T0)


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


def test_add_get_and_sorted(store: ScheduleStore):
    b = store.add("乙", T0 + timedelta(hours=5))
    a = store.add("甲", T0 + timedelta(hours=1))
    assert [e.title for e in store.all()] == ["甲", "乙"], "必须按开始时间排序"
    assert store.get(a.id) is a
    assert len(store) == 2 and b.id != a.id


def test_roundtrip_through_disk(tmp_path: Path):
    p = tmp_path / "s.json"
    s1 = ScheduleStore(p, clock=lambda: T0)
    e = s1.add("我要玩游戏", T0 + timedelta(hours=5), kind="reminder", remind_before=10,
               end=T0 + timedelta(hours=6), note="设置今天下午三点的日程我要玩游戏")
    s2 = ScheduleStore(p, clock=lambda: T0)
    got = s2.get(e.id)
    assert got is not None
    assert (got.title, got.start, got.end, got.kind, got.remind_before, got.note) == (
        e.title, e.start, e.end, e.kind, e.remind_before, e.note
    )


def test_save_is_atomic_and_leaves_no_temp(store: ScheduleStore):
    store.add("x", T0)
    assert not store.path.with_name(store.path.name + ".tmp").exists()
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 1


def test_corrupt_file_is_kept_not_overwritten(tmp_path: Path):
    """读失败 ≠ 没有日程。把坏文件覆盖掉，用户的数据就悄悄没了。"""
    p = tmp_path / "s.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    s = ScheduleStore(p, clock=lambda: T0)
    assert s.load_error and "读不了" in s.load_error
    assert len(s) == 0
    kept = list(tmp_path.glob("s.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{ 这不是 json"
    s.add("新的", T0)  # 之后正常使用，不影响留下的那份
    assert kept[0].read_text(encoding="utf-8") == "{ 这不是 json"
    assert len(ScheduleStore(p, clock=lambda: T0)) == 1


def test_wrong_shape_is_also_treated_as_corrupt(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text('{"events": "不是数组"}', encoding="utf-8")
    assert ScheduleStore(p).load_error


def test_bad_entries_are_dropped_good_ones_kept(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"events": [
        {"id": "ok1", "title": "好的", "start": "2026-10-05T15:00:00"},
        {"id": "bad", "title": "没有开始时间"},
        {"title": "没有 id", "start": "2026-10-05T15:00:00"},
        {"id": "bad2", "title": "时间格式错", "start": "明天"},
        "不是对象",
    ]}), encoding="utf-8")
    s = ScheduleStore(p)
    assert [e.id for e in s.all()] == ["ok1"] and not s.load_error


def test_unknown_kind_and_status_fall_back(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"events": [
        {"id": "a", "start": "2026-10-05T15:00:00", "kind": "怪", "status": "怪", "remind_before": -3},
    ]}), encoding="utf-8")
    e = ScheduleStore(p).all()[0]
    assert (e.kind, e.status, e.remind_before, e.title) == ("event", "pending", 0, "日程")


def test_missing_file_is_empty_without_error(tmp_path: Path):
    s = ScheduleStore(tmp_path / "nope" / "s.json")
    assert len(s) == 0 and not s.load_error


def test_blank_title_gets_default(store: ScheduleStore):
    assert store.add("  ", T0, kind="alarm").title == "闹钟"
    assert store.add("", T0).title == "日程"


def test_duplicate_detection(store: ScheduleStore):
    e = store.add("开会", T0 + timedelta(hours=2))
    assert store.find_duplicate("开会", T0 + timedelta(hours=2)) is e
    assert store.find_duplicate(" 开会 ", T0 + timedelta(hours=2)) is e
    assert store.find_duplicate("开会", T0 + timedelta(hours=3)) is None
    store.mark_fired(e.id)
    assert store.find_duplicate("开会", T0 + timedelta(hours=2)) is None, "已触发的不算重复"


def test_remove(store: ScheduleStore):
    e = store.add("x", T0)
    assert store.remove(e.id) is True
    assert store.remove(e.id) is False
    assert len(ScheduleStore(store.path)) == 0, "删除要落盘"


def test_status_changes_and_clear_finished(store: ScheduleStore):
    a, b, c, d = (store.add(n, T0 + timedelta(hours=i)) for i, n in enumerate("abcd"))
    store.mark_fired(a.id)
    store.set_status(b.id, "done")
    store.set_status(c.id, "cancelled")
    assert store.clear_finished() == 3
    assert [e.id for e in store.all()] == [d.id]
    with pytest.raises(ValueError):
        store.set_status(d.id, "瞎写")
    assert store.set_status("不存在", "done") is False


def test_queries(store: ScheduleStore):
    today = store.add("今天的", T0 + timedelta(hours=5))
    tomorrow = store.add("明天的", T0 + timedelta(days=1, hours=5))
    past = store.add("早上的", T0 - timedelta(hours=2))
    assert [e.id for e in store.on_day(date(2026, 10, 5))] == [past.id, today.id]
    assert [e.id for e in store.on_day(date(2026, 10, 6))] == [tomorrow.id]
    assert [e.id for e in store.upcoming(T0)] == [today.id, tomorrow.id]
    assert [e.id for e in store.due(T0)] == [past.id]
    assert store.next_due() == past.due_at
    store.mark_fired(past.id)
    assert store.next_due() == today.due_at
    assert past.id in [e.id for e in store.on_day(date(2026, 10, 5), include_finished=True)]
    assert past.id not in [e.id for e in store.on_day(date(2026, 10, 5))]


def test_due_at_accounts_for_remind_before_and_snooze(store: ScheduleStore):
    e = store.add("会", T0 + timedelta(hours=2), remind_before=15)
    assert e.due_at == T0 + timedelta(hours=1, minutes=45)
    store.snooze(e.id, 10, now=T0)
    assert e.due_at == T0 + timedelta(minutes=10)
    assert e.status == "pending"


def test_snooze_revives_a_fired_event(store: ScheduleStore):
    e = store.add("x", T0)
    store.mark_fired(e.id)
    assert e.status == "fired"
    store.snooze(e.id, 5, now=T0)
    assert e.status == "pending" and e.snoozed_until == T0 + timedelta(minutes=5)
    store.mark_fired(e.id)
    assert e.snoozed_until is None, "触发后推迟标记要清掉，否则下次 due_at 还是旧的"


def test_default_store_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("VOICE_CTL_SCHEDULE_FILE", str(tmp_path / "x.json"))
    assert default_store_path() == tmp_path / "x.json"
    monkeypatch.delenv("VOICE_CTL_SCHEDULE_FILE")
    monkeypatch.setenv("VOICE_CTL_DATA", str(tmp_path / "d"))
    assert default_store_path() == tmp_path / "d" / "schedule.json"


def test_concurrent_adds_do_not_lose_events(store: ScheduleStore):
    def work(i: int) -> None:
        for j in range(10):
            store.add(f"{i}-{j}", T0 + timedelta(minutes=i * 10 + j))

    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(store) == 60
    assert len(ScheduleStore(store.path)) == 60


# --------------------------------------------------------------------------- #
# .ics
# --------------------------------------------------------------------------- #


def unfold(ics: str) -> list[str]:
    return ics.replace("\r\n ", "").split("\r\n")


def mk(title: str = "开会", **kw) -> Event:  # noqa: ANN003
    return Event(id="abc12345", title=title, start=datetime(2026, 10, 5, 15, 0), **kw)


def test_ics_basic_structure():
    ics = to_ics([mk()], now=T0)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    lines = unfold(ics)
    assert "VERSION:2.0" in lines and "BEGIN:VEVENT" in lines and "END:VEVENT" in lines
    assert "UID:abc12345@voice-ctl" in lines
    assert "SUMMARY:开会" in lines
    assert "\n" not in ics.replace("\r\n", ""), "只能有 CRLF，不能混进裸 LF"


def test_ics_times_are_utc_and_match_local_conversion():
    ics = to_ics([mk()], now=T0)
    lines = unfold(ics)
    start = next(ln for ln in lines if ln.startswith("DTSTART:"))
    end = next(ln for ln in lines if ln.startswith("DTEND:"))
    assert re.fullmatch(r"DTSTART:\d{8}T\d{6}Z", start)
    expect = datetime(2026, 10, 5, 15, 0).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    assert start == f"DTSTART:{expect}"
    assert end == "DTEND:" + datetime(2026, 10, 5, 16, 0).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), \
        "没给结束时间默认一小时"


def test_ics_explicit_end_and_default_minutes():
    e = mk(end=datetime(2026, 10, 5, 17, 30))
    assert "DTEND:" + datetime(2026, 10, 5, 17, 30).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") \
        in unfold(to_ics([e]))
    assert "DTEND:" + datetime(2026, 10, 5, 15, 30).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") \
        in unfold(to_ics([mk()], default_minutes=30))


def test_ics_alarm_trigger_reflects_remind_before():
    assert "TRIGGER:-PT0M" in unfold(to_ics([mk()]))
    assert "TRIGGER:-PT10M" in unfold(to_ics([mk(remind_before=10)]))


def test_ics_escapes_special_characters():
    ics = to_ics([mk("买菜, 水果; 路径C:\\x\n第二行")])
    summary = next(ln for ln in unfold(ics) if ln.startswith("SUMMARY:"))
    assert summary == "SUMMARY:买菜\\, 水果\\; 路径C:\\\\x\\n第二行"


def test_ics_description_carries_original_speech():
    ics = to_ics([mk(note="设置今天下午三点的日程我要玩游戏")])
    assert any(ln.startswith("DESCRIPTION:语音原话：") for ln in unfold(ics))


def test_ics_lines_fold_at_75_bytes_and_never_split_a_character():
    title = "这是一个很长很长的日程标题" * 6 + "，包含中文标点、English words and 123"
    ics = to_ics([mk(title)])
    for physical in ics.split("\r\n"):
        assert len(physical.encode("utf-8")) <= 75, f"超过 75 字节：{physical!r}"
        physical.encode("utf-8").decode("utf-8")  # 不会在多字节字符中间切开
    summary = next(ln for ln in unfold(ics) if ln.startswith("SUMMARY:"))
    assert summary == "SUMMARY:" + title.replace(",", "\\,"), "展开后必须和原文一致"


def test_ics_multiple_events_and_empty_calendar():
    ics = to_ics([mk("甲"), Event(id="b", title="乙", start=datetime(2026, 10, 6, 9, 0))])
    assert ics.count("BEGIN:VEVENT") == 2 and ics.count("END:VEVENT") == 2
    empty = to_ics([])
    assert "BEGIN:VCALENDAR" in empty and "VEVENT" not in empty


def test_write_ics_keeps_crlf_exactly(tmp_path: Path):
    p = write_ics(tmp_path / "sub" / "a.ics", [mk()])
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\r\r\n" not in raw, "文本模式写会把 CRLF 变成 CRCRLF，日历软件会拒收"


# --------------------------------------------------------------------------- #
# 提醒服务
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_service(tmp_path: Path, now: datetime = T0, **kw):  # noqa: ANN003, ANN202
    clock = Clock(now)
    store = ScheduleStore(tmp_path / "s.json", clock=clock)
    fired: list[tuple[Event, bool]] = []
    svc = ReminderService(store, lambda e, late: fired.append((e, late)), clock=clock, **kw)
    return clock, store, fired, svc


def test_fires_once_when_due(tmp_path: Path):
    clock, store, fired, svc = make_service(tmp_path)
    e = store.add("开会", T0 + timedelta(minutes=5))
    assert svc.tick() == 0
    clock.now = T0 + timedelta(minutes=5)
    assert svc.tick() == 1 and fired[0][0].id == e.id and fired[0][1] is False
    assert e.status == "fired"
    assert svc.tick() == 0, "同一条提醒最多响一次"


def test_fires_in_start_order(tmp_path: Path):
    clock, store, fired, svc = make_service(tmp_path)
    store.add("后", T0 + timedelta(seconds=-20))
    store.add("先", T0 + timedelta(seconds=-60))
    svc.tick()
    assert [e.title for e, _ in fired] == ["先", "后"]


def test_state_is_persisted_before_callback(tmp_path: Path):
    """先落盘再弹窗：响到一半程序崩了，下次启动不能再响一遍。"""
    clock, store, _fired, _svc = make_service(tmp_path)
    e = store.add("x", T0)
    seen: list[str] = []

    def on_fire(ev: Event, late: bool) -> None:  # noqa: ARG001
        on_disk = json.loads(store.path.read_text(encoding="utf-8"))["events"][0]
        seen.append(on_disk["status"])

    ReminderService(store, on_fire, clock=clock).tick()
    assert seen == ["fired"], f"回调执行时磁盘上的状态是 {seen}，应当已经是 fired"
    assert e.status == "fired"


def test_late_event_is_replayed_and_marked_missed(tmp_path: Path):
    """电脑睡了一觉 / 程序没开：补响一次，并标明是错过的。"""
    clock, store, fired, svc = make_service(tmp_path, now=T0 + timedelta(minutes=30))
    e = store.add("开会", T0)
    svc.tick()
    assert fired and fired[0][1] is True
    assert e.status == "missed"


def test_slightly_late_is_not_flagged(tmp_path: Path):
    clock, store, fired, svc = make_service(tmp_path, now=T0 + timedelta(seconds=30))
    e = store.add("x", T0)
    svc.tick()
    assert fired[0][1] is False and e.status == "fired"


def test_stale_events_are_marked_but_not_popped_up(tmp_path: Path):
    """开机被三天前的提醒淹没是灾难：太久以前的只标记，不弹窗。"""
    stale: list[Event] = []
    clock, store, fired, svc = make_service(
        tmp_path, now=T0 + timedelta(days=3), on_stale=stale.append
    )
    old = store.add("旧的", T0)
    recent = store.add("刚错过", T0 + timedelta(days=3) - timedelta(minutes=10))
    svc.tick()
    assert [e.id for e, _ in fired] == [recent.id]
    assert [e.id for e in stale] == [old.id]
    assert old.status == "missed"


def test_callback_exception_does_not_stop_others(tmp_path: Path):
    clock = Clock(T0)
    store = ScheduleStore(tmp_path / "s.json", clock=clock)
    got: list[str] = []

    def on_fire(e: Event, late: bool) -> None:  # noqa: ARG001
        got.append(e.title)
        if e.title == "炸":
            raise RuntimeError("弹窗失败")

    svc = ReminderService(store, on_fire, clock=clock)
    store.add("炸", T0 - timedelta(seconds=30))
    store.add("好", T0 - timedelta(seconds=10))
    assert svc.tick() == 2
    assert got == ["炸", "好"]


def test_snooze_fires_again_later(tmp_path: Path):
    clock, store, fired, svc = make_service(tmp_path)
    e = store.add("x", T0)
    svc.tick()
    store.snooze(e.id, 10, now=clock.now)
    clock.now = T0 + timedelta(minutes=9)
    assert svc.tick() == 0
    clock.now = T0 + timedelta(minutes=10)
    assert svc.tick() == 1 and len(fired) == 2


def test_remind_before_fires_early(tmp_path: Path):
    clock, store, fired, svc = make_service(tmp_path)
    store.add("会", T0 + timedelta(minutes=30), remind_before=10)
    clock.now = T0 + timedelta(minutes=19)
    assert svc.tick() == 0
    clock.now = T0 + timedelta(minutes=20)
    assert svc.tick() == 1


def test_thread_loop_fires_with_real_clock(tmp_path: Path):
    store = ScheduleStore(tmp_path / "s.json")
    got = threading.Event()
    svc = ReminderService(store, lambda e, late: got.set(), poll=0.05)
    svc.start()
    try:
        store.add("马上", datetime.now() + timedelta(milliseconds=200))
        assert got.wait(3), "到点后 3 秒内没有触发"
    finally:
        svc.stop()
    assert not svc.running


def test_wake_interrupts_a_long_sleep(tmp_path: Path):
    """轮询间隔设得很长时，新增事件靠 wake() 立刻重新计算下一个到期时刻。"""
    store = ScheduleStore(tmp_path / "s.json")
    got = threading.Event()
    svc = ReminderService(store, lambda e, late: got.set(), poll=30.0)
    svc.start()
    try:
        time.sleep(0.1)  # 让线程先进入长等待
        store.add("急", datetime.now() + timedelta(milliseconds=300))
        svc.wake()
        assert got.wait(3), "wake() 之后应当按新事件的到期时刻醒来，而不是睡满 30 秒"
    finally:
        svc.stop()


def test_start_is_idempotent_and_stop_is_safe(tmp_path: Path):
    store = ScheduleStore(tmp_path / "s.json")
    svc = ReminderService(store, lambda e, late: None, poll=0.05)
    svc.stop()  # 没启动时 stop 不炸
    svc.start()
    t = svc._thread
    svc.start()
    assert svc._thread is t
    svc.stop()
    svc.stop()
