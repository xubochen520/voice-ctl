"""日程 / 提醒：存储、`.ics` 导出、到点提醒。

默认方案是**零配置、纯本地**：

  * 事件存在一个 JSON 文件里（`schedule.json`，放可写数据目录）
  * 本程序本来就是常驻的，所以到点提醒由它自己负责——不依赖 Outlook、不依赖任何账号
  * 同时能导出标准 `.ics`，用户的日历（Outlook / Google / 飞书 / 手机）想要的话导进去

为什么不直接写进某个日历：这台机器没装经典 Outlook（COM 走不通），`.ics` 的默认
关联也是个 UWP 应用；而国内常用的日历（飞书、滴答清单）要账号授权。先把"说一句话
就有一条会准时响的提醒"做扎实，再把 CalDAV 之类做成可替换的后端。

三个必须守住的约束：

  1. **用户数据不能因为一次读失败就丢**。JSON 坏了不能当成空文件覆盖——先把坏文件
     改名留着，再从空开始，并且把这件事告诉调用方。
  2. **提醒最多响一次**。先把"已触发"落盘，再去弹窗/响铃：反过来的话，响到一半
     程序崩了，下次启动会再响一遍。
  3. **睡眠/关机之后不能哑火，也不能翻旧账**。错过的提醒补响一次并标明"已错过"；
     太久以前的（默认 12 小时）只标记、不弹窗，否则开机就被三天前的提醒淹没。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

KINDS = ("event", "reminder", "alarm", "timer")
KIND_LABEL = {"event": "日程", "reminder": "提醒", "alarm": "闹钟", "timer": "倒计时"}
STATUSES = ("pending", "fired", "missed", "done", "cancelled")
STATUS_LABEL = {
    "pending": "待提醒", "fired": "已提醒", "missed": "已错过", "done": "已完成", "cancelled": "已取消",
}

LATE_AFTER = timedelta(seconds=90)
"""比应提醒时刻晚超过这么久才触发，就算"错过"（程序没开 / 电脑睡眠了）。"""

STALE_AFTER = timedelta(hours=12)
"""错过超过这么久的，只标记不弹窗。"""


def default_store_path() -> Path:
    """日程文件位置。`VOICE_CTL_SCHEDULE_FILE` 可以整个改掉（测试隔离、多份日程）。"""
    override = os.environ.get("VOICE_CTL_SCHEDULE_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    from . import bootstrap

    return bootstrap.data_dir() / "schedule.json"


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _parse(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    """本地时间（naive）。导出 .ics 时才换算成 UTC。"""

    end: datetime | None = None
    kind: str = "event"
    status: str = "pending"
    created: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))
    fired_at: datetime | None = None
    snoozed_until: datetime | None = None
    remind_before: int = 0
    """提前几分钟提醒。"""
    note: str = ""
    """用户原话。纠错、核对「我当时到底说了什么」时用。"""

    @property
    def due_at(self) -> datetime:
        """下一次该提醒的时刻：被推迟过就用推迟后的。"""
        if self.snoozed_until is not None:
            return self.snoozed_until
        return self.start - timedelta(minutes=self.remind_before)

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, "日程")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "start": _iso(self.start), "end": _iso(self.end),
            "kind": self.kind, "status": self.status, "created": _iso(self.created),
            "fired_at": _iso(self.fired_at), "snoozed_until": _iso(self.snoozed_until),
            "remind_before": self.remind_before, "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event | None":
        start = _parse(d.get("start"))
        if start is None or not d.get("id"):
            return None  # 缺关键字段的条目丢掉，别让一条坏数据拖垮整份日程
        kind = d.get("kind") if d.get("kind") in KINDS else "event"
        status = d.get("status") if d.get("status") in STATUSES else "pending"
        rb = d.get("remind_before", 0)
        return cls(
            id=str(d["id"]), title=str(d.get("title") or "日程"), start=start,
            end=_parse(d.get("end")), kind=kind, status=status,
            created=_parse(d.get("created")) or datetime.now().replace(microsecond=0),
            fired_at=_parse(d.get("fired_at")), snoozed_until=_parse(d.get("snoozed_until")),
            remind_before=rb if isinstance(rb, int) and rb >= 0 else 0, note=str(d.get("note") or ""),
        )


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


class ScheduleStore:
    """线程安全的日程存储。每次改动立即写盘（数据量很小，不值得做延迟写）。"""

    def __init__(self, path: str | Path | None = None, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self.path = Path(path) if path else default_store_path()
        self.clock = clock
        self._lock = threading.RLock()
        self._events: dict[str, Event] = {}
        self.load_error: str = ""
        """上次加载是否出过问题（文件坏了）。非空时调用方应当告诉用户。"""
        self.load()

    # -- 读写 ------------------------------------------------------------- #

    def load(self) -> None:
        with self._lock:
            self._events = {}
            self.load_error = ""
            if not self.path.is_file():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                items = raw["events"] if isinstance(raw, dict) else raw
                if not isinstance(items, list):
                    raise ValueError("events 不是数组")
            except (OSError, ValueError, KeyError, TypeError) as e:
                # 不能把"读失败"当成"没有日程"然后覆盖掉——那会让用户的数据悄悄消失
                keep = self.path.with_name(f"{self.path.name}.corrupt-{self.clock():%Y%m%d-%H%M%S}")
                try:
                    self.path.replace(keep)
                    self.load_error = f"日程文件读不了（{e}），已原样改名为 {keep.name} 留着，从空白开始"
                except OSError:
                    self.load_error = f"日程文件读不了（{e}），且无法改名备份——请手动处理 {self.path}"
                return
            for d in items:
                if isinstance(d, dict) and (ev := Event.from_dict(d)) is not None:
                    self._events[ev.id] = ev

    def _save(self) -> None:
        """原子写：先写临时文件再 replace。写到一半断电，旧文件仍然完整。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": SCHEMA_VERSION, "events": [e.to_dict() for e in self._sorted()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _sorted(self) -> list[Event]:
        return sorted(self._events.values(), key=lambda e: (e.start, e.created, e.id))

    # -- 增删改 ----------------------------------------------------------- #

    def add(
        self,
        title: str,
        start: datetime,
        *,
        end: datetime | None = None,
        kind: str = "event",
        remind_before: int = 0,
        note: str = "",
    ) -> Event:
        ev = Event(
            id=new_id(), title=title.strip() or KIND_LABEL.get(kind, "日程"),
            start=start.replace(microsecond=0), end=end, kind=kind if kind in KINDS else "event",
            created=self.clock().replace(microsecond=0), remind_before=max(0, int(remind_before)),
            note=note,
        )
        with self._lock:
            self._events[ev.id] = ev
            self._save()
        return ev

    def find_duplicate(self, title: str, start: datetime) -> Event | None:
        """已经有一条同名、同时刻、还没触发的了？用来避免「说了两遍建了两条」。"""
        t = title.strip()
        with self._lock:
            for e in self._events.values():
                if e.status == "pending" and e.start == start.replace(microsecond=0) and e.title == t:
                    return e
        return None

    def remove(self, event_id: str) -> bool:
        with self._lock:
            if self._events.pop(event_id, None) is None:
                return False
            self._save()
            return True

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            return self._events.get(event_id)

    def set_status(self, event_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"未知状态 {status!r}")
        with self._lock:
            ev = self._events.get(event_id)
            if ev is None:
                return False
            ev.status = status
            if status == "pending":
                ev.fired_at = None
            self._save()
            return True

    def mark_fired(self, event_id: str, now: datetime | None = None, *, missed: bool = False) -> bool:
        with self._lock:
            ev = self._events.get(event_id)
            if ev is None:
                return False
            ev.status = "missed" if missed else "fired"
            ev.fired_at = (now or self.clock()).replace(microsecond=0)
            ev.snoozed_until = None
            self._save()
            return True

    def snooze(self, event_id: str, minutes: int, now: datetime | None = None) -> bool:
        """推迟 N 分钟再提醒：回到 pending，due_at 变成现在 + N 分钟。"""
        with self._lock:
            ev = self._events.get(event_id)
            if ev is None:
                return False
            ev.status = "pending"
            ev.snoozed_until = ((now or self.clock()) + timedelta(minutes=max(1, minutes))).replace(microsecond=0)
            self._save()
            return True

    def clear_finished(self) -> int:
        """清掉已提醒/已错过/已完成/已取消的。返回清掉几条。"""
        with self._lock:
            gone = [i for i, e in self._events.items() if e.status != "pending"]
            for i in gone:
                del self._events[i]
            if gone:
                self._save()
            return len(gone)

    # -- 查询 ------------------------------------------------------------- #

    def all(self) -> list[Event]:
        with self._lock:
            return self._sorted()

    def pending(self) -> list[Event]:
        with self._lock:
            return [e for e in self._sorted() if e.status == "pending"]

    def on_day(self, day: date, *, include_finished: bool = False) -> list[Event]:
        with self._lock:
            return [
                e for e in self._sorted()
                if e.start.date() == day and (include_finished or e.status == "pending")
            ]

    def upcoming(self, now: datetime | None = None, limit: int = 5) -> list[Event]:
        now = now or self.clock()
        with self._lock:
            return [e for e in self._sorted() if e.status == "pending" and e.start >= now][:limit]

    def due(self, now: datetime | None = None) -> list[Event]:
        now = now or self.clock()
        with self._lock:
            return [e for e in self._sorted() if e.status == "pending" and e.due_at <= now]

    def next_due(self) -> datetime | None:
        with self._lock:
            times = [e.due_at for e in self._events.values() if e.status == "pending"]
            return min(times) if times else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


# --------------------------------------------------------------------------- #
# .ics 导出（RFC 5545）
# --------------------------------------------------------------------------- #


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """按 RFC 5545 折行：每行最多 75 **字节**（不是字符），续行以一个空格开头。
    中文一个字 3 字节，按字符数折会超；而且绝不能从一个多字节字符中间切开。"""
    out: list[str] = []
    cur, size, limit = "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " " + ch, 1 + n, 75
        else:
            cur += ch
            size += n
    out.append(cur)
    return "\r\n".join(out)


def _utc(dt: datetime) -> str:
    """本地 naive 时间 → UTC 基本格式。`astimezone()` 对 naive 时间按系统时区解释，含夏令时。"""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def to_ics(events: list[Event], *, now: datetime | None = None, default_minutes: int = 60) -> str:
    stamp = (now or datetime.now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//voice-ctl//schedule//CN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:voice-ctl",
    ]
    for e in events:
        end = e.end or (e.start + timedelta(minutes=default_minutes))
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.id}@voice-ctl",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(e.start)}",
            f"DTEND:{_utc(end)}",
            f"SUMMARY:{_ics_escape(e.title)}",
        ]
        if e.note:
            lines.append(f"DESCRIPTION:{_ics_escape('语音原话：' + e.note)}")
        lines += [
            "BEGIN:VALARM",
            f"TRIGGER:-PT{e.remind_before}M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_ics_escape(e.title)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


def write_ics(path: str | Path, events: list[Event], **kw: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 二进制写：保证 CRLF 原样落盘（文本模式在 Windows 上会把 \r\n 变成 \r\r\n）
    p.write_bytes(to_ics(events, **kw).encode("utf-8"))
    return p


# --------------------------------------------------------------------------- #
# 到点提醒
# --------------------------------------------------------------------------- #


class ReminderService:
    """后台线程：到点就调 `on_fire(event, late)`。

    `late=True` 表示这条是**错过后补响**的（程序没开、电脑睡眠了）。
    轮询间隔默认 1 秒而不是"睡到下一个事件再醒"：用墙钟时间判断，系统休眠、
    手动改时间之后也能在 1 秒内恢复正确，不会因为睡了一觉就哑火。
    """

    def __init__(
        self,
        store: ScheduleStore,
        on_fire: Callable[[Event, bool], None],
        *,
        clock: Callable[[], datetime] = datetime.now,
        poll: float = 1.0,
        late_after: timedelta = LATE_AFTER,
        stale_after: timedelta = STALE_AFTER,
        on_stale: Callable[[Event], None] | None = None,
    ) -> None:
        self.store = store
        self.on_fire = on_fire
        self.on_stale = on_stale
        self.clock = clock
        self.poll = poll
        self.late_after = late_after
        self.stale_after = stale_after
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="voice-ctl-reminders", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    def wake(self) -> None:
        """新增/修改了事件，让线程立刻重新算一遍下一个到期时刻。"""
        self._wake.set()

    def tick(self) -> int:
        """处理一次所有到期的事件。返回触发了几条。循环线程和测试都调它。"""
        now = self.clock()
        fired = 0
        for ev in self.store.due(now):
            lateness = now - ev.due_at
            if lateness > self.stale_after:
                self.store.mark_fired(ev.id, now, missed=True)
                if self.on_stale:
                    self._safe(self.on_stale, ev)
                continue
            late = lateness > self.late_after
            # 先落盘再弹窗：反过来的话，响到一半崩溃，下次启动会再响一遍
            self.store.mark_fired(ev.id, now, missed=late)
            self._safe(self.on_fire, ev, late)
            fired += 1
        return fired

    @staticmethod
    def _safe(fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - 提醒回调出错不能让整个提醒线程死掉
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                pass
            nxt = self.store.next_due()
            timeout = self.poll
            if nxt is not None:
                timeout = max(0.02, min(self.poll, (nxt - self.clock()).total_seconds()))
            self._wake.wait(timeout)
            self._wake.clear()


__all__ = [
    "KIND_LABEL",
    "KINDS",
    "STATUS_LABEL",
    "Event",
    "ReminderService",
    "ScheduleStore",
    "default_store_path",
    "to_ics",
    "write_ics",
]
