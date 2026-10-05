"""到点提醒的接线：把 schedule.py 的提醒线程和用户能感知的通知接起来。

schedule.py 只管"到点了该响了"，它不知道通知长什么样。这一层负责三件事：

  1. **进程内只有一条提醒线程**。日程 handler 建完新日程要 `wake()` 它；
     CLI 的 run/serve、界面的引擎启动都要保证它活着。做成单例而不是让每个
     调用方各起一条，是因为两条线程盯同一个 store 会把同一条提醒响两遍。
  2. **通知要能被替换**。命令行弹 MessageBox、界面弹自己的卡片、测试传个
     callback 收着看——`on_fire` 由调用方注入，这里不写死任何一种。
  3. **响过的证据留在日志里**。错过（电脑睡眠/程序没开）要标出来，否则用户
     会以为提醒没生效。

默认通知走两路：控制台事件（总能在日志页看到）+ Windows 的提示音。刻意**不**
弹模态对话框——用户可能正在全屏游戏或演示，弹窗会打断他；真要显眼的通知，
界面自己订阅事件去弹。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from . import events
from .schedule import Event, ReminderService, ScheduleStore, default_store_path

_lock = threading.Lock()
_service: ReminderService | None = None
_store: ScheduleStore | None = None


def current_service() -> ReminderService | None:
    """当前活着的提醒服务；没启动时返回 None（handler 用它决定要不要 wake）。"""
    return _service


def current_store() -> ScheduleStore | None:
    return _store


def _default_on_fire(event: Event, late: bool) -> None:
    when = event.start.strftime("%H:%M")
    head = "错过的提醒" if late else "提醒"
    body = f"{head}：{event.title}（{when}）"
    if late:
        body += "  —— 电脑刚才在睡眠或程序没开，现在补上"
    events.get_bus().emit("warn" if late else "ok", body, kind="schedule", event_id=event.id)
    _chime()


def _chime() -> None:
    """响一声。用系统提示音而不是自己合成波形——它尊重用户的音量方案，
    静音时本来就该没声音。"""
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:  # noqa: BLE001 - 非 Windows 或没有声音设备
        pass


def _default_on_stale(event: Event) -> None:
    events.get_bus().emit(
        "info",
        f"很久以前的提醒已过期，不再弹：{event.title}"
        f"（{event.start:%m-%d %H:%M}）",
        kind="schedule",
        event_id=event.id,
    )


def start(
    *,
    store: ScheduleStore | None = None,
    on_fire: Callable[[Event, bool], None] | None = None,
    on_stale: Callable[[Event], None] | None = None,
    path: Any = None,
) -> ReminderService:
    """启动（或返回已经启动的）提醒服务。

    重复调用是安全的：第二次直接把第一条线程返回给你，不会多响一遍。
    """
    global _service, _store
    with _lock:
        if _service is not None and _service.running:
            return _service
        _store = store or ScheduleStore(path or default_store_path())
        _service = ReminderService(
            _store,
            on_fire or _default_on_fire,
            on_stale=on_stale or _default_on_stale,
        )
        _service.start()
        if _store.load_error:
            events.get_bus().emit("warn", _store.load_error, kind="schedule")
        return _service


def stop() -> None:
    global _service, _store
    with _lock:
        if _service is not None:
            _service.stop()
        _service = None
        _store = None


def status() -> str:
    """给 doctor / 关于页用的一句话。"""
    svc, store = _service, _store
    if svc is None or store is None or not svc.running:
        return "提醒服务未启动"
    pending = store.pending()
    nxt = store.next_due()
    tail = f"，最近一条 {nxt:%m-%d %H:%M}" if nxt else ""
    return f"提醒服务运行中，待提醒 {len(pending)} 条{tail}"


__all__ = [
    "current_service",
    "current_store",
    "start",
    "status",
    "stop",
]
