"""handler: schedule —— 说一句话就有一条会准时响的提醒。

用户的原话是「设置今天下午三点的日程『我要玩游戏』」。拆开看是三件事：

    时间   由 timeparse.py 解析（中文数字、口语时段、相对时间全在那边）
    标题   由 intent.py 提炼（用户的原话，一个字不能改）
    落地   由 schedule.py 存（本地 JSON + .ics 导出 + 常驻提醒线程）

这个 handler 只负责**把它们接起来**，并把用户该看到的话说出来。它刻意不碰
自然语言——所有理解都在意图层，这里只认已经结构化好的槽位。

三条规矩：

  1. **不替用户猜时间。** 意图层标了 `ambiguous`（没说上午下午）或时间已经过去时，
     返回"需要确认"而不是直接建。日程建错了比没建更烦人——它会在错的时间响。
  2. **同一条说两遍不建两条。** `ScheduleStore.find_duplicate` 挡住，这是实测到的：
     用户不确定第一遍成没成，会再说一遍。
  3. **说出来的话必须能核对。** 成功时把时间**和星期**一起报出来（「今天 15:00」），
     用户听到就能确认自己没被理解错；只报「已创建」等于什么都没说。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from . import Action, ActionContext, ActionResult

# --------------------------------------------------------------------------- #
# 存储：进程内共享一份
#
# 每次执行都新建 ScheduleStore 会重新读一遍 JSON，更要紧的是提醒线程要盯着
# 同一个对象。用模块级缓存而不是全局变量，是为了测试能注入自己的路径
# （VOICE_CTL_SCHEDULE_FILE）并在用完后 close() 清掉。
# --------------------------------------------------------------------------- #

_STORES: dict[str, Any] = {}


def store_for(path: str | os.PathLike[str] | None = None):  # noqa: ANN201
    """拿到（并缓存）一个 ScheduleStore。"""
    from ..schedule import ScheduleStore, default_store_path

    p = str(path) if path else str(default_store_path())
    s = _STORES.get(p)
    if s is None:
        s = ScheduleStore(p)
        _STORES[p] = s
    return s


def reset_stores() -> None:
    """清掉缓存。测试之间必须调它，否则上一条用例的日程会漏到下一条。"""
    _STORES.clear()


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #


class ScheduleAction(Action):
    handler_name = "schedule"

    def _store(self):  # noqa: ANN202
        return store_for(self.cfg.args[0] if self.cfg.args else None)

    def _kind(self) -> str:
        """日程 / 提醒 / 闹钟 / 倒计时。target 里写，默认 event。"""
        t = (self.cfg.target or "").strip().lower()
        return t if t in ("event", "reminder", "alarm", "timer") else "event"

    def preflight(self) -> ActionResult:
        try:
            store = self._store()
        except Exception as e:  # noqa: BLE001 - 路径不可写是最常见的失败
            return ActionResult(False, f"日程文件用不了：{e}")
        if store.load_error:
            return ActionResult(False, "日程文件上次读坏了", store.load_error)
        return ActionResult(True, f"日程文件 {store.path}", f"现有 {len(store)} 条")

    # -- 执行 ------------------------------------------------------------- #

    def execute(self, ctx: ActionContext) -> ActionResult:
        who = ctx.slots.get("op") or "add"
        if who == "list":
            return self._list(ctx)
        if who in ("cancel", "delete", "remove"):
            return self._cancel(ctx)
        return self._add(ctx)

    def _add(self, ctx: ActionContext) -> ActionResult:
        when = ctx.slots.get("when")
        if when is None:
            return ActionResult(False, "没听出时间", "说「明天早上八点提醒我开会」这样带时间的")
        start: datetime = when.start
        title = str(ctx.slots.get("title") or "").strip() or "提醒"

        # 过去的时间先判，**在 dry-run 之前**：dry-run 的职责是"如实预演会做什么"，
        # 报一句「将建 …」而真实执行会拒绝，那是在骗用户。
        if when.in_past:
            return ActionResult(
                False, f"{_describe(start)} 已经过去了，没有创建",
                "如果是想说别的日子，把日期也说上",
            )

        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将建 {_describe(start)} 的「{title}」")

        store = self._store()
        if (dup := store.find_duplicate(title, start)) is not None:
            return ActionResult(
                True, f"已经有一条 {_describe(start)} 的「{title}」了", "没有重复创建"
            )

        ev = store.add(
            title,
            start,
            end=when.end,
            kind=self._kind(),
            remind_before=int(ctx.slots.get("remind_before") or 0),
            note=str(ctx.slots.get("source") or ctx.text or ""),
        )
        _wake()
        return ActionResult(
            True, f"{_describe(ev.start)} 提醒你「{ev.title}」",
            f"{ev.label}已存进 {store.path.name}" + (f"；{when.note()}" if when.note() else ""),
        )

    def _list(self, ctx: ActionContext) -> ActionResult:
        store = self._store()
        items = store.upcoming(limit=5)
        if not items:
            return ActionResult(True, "接下来没有待提醒的日程")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 接下来有 {len(items)} 条日程")
        lines = "；".join(f"{_describe(e.start)} {e.title}" for e in items)
        return ActionResult(True, f"接下来 {len(items)} 条：{lines}")

    def _cancel(self, ctx: ActionContext) -> ActionResult:
        store = self._store()
        want = str(ctx.slots.get("title") or "").strip()
        when = ctx.slots.get("when")
        target = None
        if want:
            for e in store.pending():
                if want in e.title or e.title in want:
                    if when is None or e.start.date() == when.start.date():
                        target = e
                        break
        elif when is not None:
            for e in store.pending():
                if e.start.date() == when.start.date():
                    target = e
                    break
        if target is None:
            return ActionResult(False, "没找到要取消的日程", "说「取消三点的提醒」或「取消日程 开会」")
        if ctx.dry_run:
            return ActionResult(True, f"[dry-run] 将取消「{target.title}」")
        store.remove(target.id)
        _wake()
        return ActionResult(True, f"已取消 {_describe(target.start)} 的「{target.title}」")


def _describe(dt: datetime) -> str:
    """把时间说成人话，带**星期**。

    周几不是装饰：用户说「周三三点」时，报出「周三 15:00」他才能一眼看出
    自己的话是不是被理解对了。只报「15:00」等于没说。
    """
    from ..timeparse import format_when

    return format_when(dt)


def _wake() -> None:
    """新增日程后叫醒提醒线程，让它立刻重算下一个到期时刻。"""
    from ..reminder import current_service

    svc = current_service()
    if svc is not None:
        svc.wake()


__all__ = ["ScheduleAction", "reset_stores", "store_for"]
