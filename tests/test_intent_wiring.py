"""意图层的执行端：日程 handler、开关应用的动态 handler、以及三层的接线顺序。

这里守的不是"功能存在"，而是几个**只有跑起来才看得见**的坑：

  * 日程的槽位从 `ctx.slots` 读，开关应用的从 `ctx.extra` 读。接错袋子时动作
    照样"执行成功"，只是参数是空的——日志上看不出任何异常。
  * `open_target` 的目标是运行时从应用索引拿的，拿不到时会 `NameError`
    （引用了没导入的 `AppEntry`），被 Pipeline 的异常兜底吞成一句
    「动作抛出异常」。
  * 关闭进程必须**先看有没有窗口**：有窗口的走优雅关闭，不然 Office 里
    没保存的文档会跟着一起没。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from voice_ctl.actions import ActionContext, build_registry, close_plan_for, normalize_exe
from voice_ctl.actions.schedule_action import ScheduleAction, reset_stores, store_for
from voice_ctl.app import Pipeline
from voice_ctl.apps import AppIndex
from voice_ctl.config import ActionConfig, AppConfig
from voice_ctl.matcher import Matcher
from voice_ctl.normalize import Normalizer
from voice_ctl.schedule import ScheduleStore
from voice_ctl.timeparse import parse_when

NOW = datetime(2026, 10, 5, 10, 0)

APPS = [("QQ", r"C:\QQ\QQ.exe"), ("微信", r"E:\weixin\Weixin.exe")]


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每条用例一个空日程文件。

    日程 handler 的 store 是**进程内缓存**的（提醒线程要盯着同一个对象），
    不隔离的话上一条用例建的日程会漏到下一条里。
    """
    p = tmp_path / "schedule.json"
    monkeypatch.setenv("VOICE_CTL_SCHEDULE_FILE", str(p))
    reset_stores()
    yield
    reset_stores()


def _cfg() -> AppConfig:
    return AppConfig(
        actions=[
            ActionConfig(id="open.wechat", handler="open_app", aliases=["微信"],
                         target=r"E:\weixin\Weixin.exe", describe="打开微信"),
            ActionConfig(id="open.target", handler="open_target", aliases=["打开应用"]),
            ActionConfig(id="schedule", handler="schedule", aliases=["日程", "提醒"], target="event"),
            ActionConfig(id="sys.close_app", handler="close_app", aliases=["关闭应用"]),
        ]
    )


def _pipe(cfg: AppConfig | None = None, *, apps: bool = True) -> Pipeline:
    cfg = cfg or _cfg()
    n = Normalizer()
    idx = AppIndex(loader=lambda: list(APPS)) if apps else None
    return Pipeline(
        asr=None,  # type: ignore[arg-type]
        matcher=Matcher(cfg.enabled_actions, normalizer=n, threshold=cfg.match.threshold),
        registry=build_registry(cfg.enabled_actions),
        actions=cfg.enabled_actions,
        normalizer=n,
        app_index=idx,
        intent_enabled=True,
        clock=lambda: NOW,
    )


# --------------------------------------------------------------------------- #
# 日程动作：纯函数与槽位
# --------------------------------------------------------------------------- #


def _schedule_ctx(text: str, **slots) -> ActionContext:
    return ActionContext(text=text, normalized=text, dry_run=True, slots=slots)


def test_add_reports_the_time_and_weekday():
    """成功时必须把时间和**星期**一起报出来。

    只报「已创建」等于没说：用户没法核对"下午三点"是不是被理解成了凌晨三点。
    周几是关键信息——用户说"周三"时，只有报出周三他才能一眼确认。
    """
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    when = parse_when("今天下午三点", NOW)
    assert when is not None
    r = act.execute(_schedule_ctx("设置今天下午三点的日程", when=when, title="我要玩游戏"))
    assert r.ok
    assert "15:00" in r.message and "周一" in r.message and "我要玩游戏" in r.message


def test_dry_run_creates_nothing():
    store = store_for()
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    when = parse_when("今天下午三点", NOW)
    assert when is not None
    r = act.execute(_schedule_ctx("x", when=when, title="t"))
    assert r.ok and "dry-run" in r.message
    assert len(store) == 0, "dry-run 绝不能写盘"


def test_no_time_is_rejected_not_guessed():
    """没说时间就不要建。猜一个时间会得到一条在错的时间响的提醒。"""
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    r = act.execute(_schedule_ctx("设置日程", title="开会"))
    assert not r.ok and "没听出时间" in r.message
    assert len(store_for()) == 0


def test_past_time_is_rejected():
    """过去的时间不建，并说清是哪一刻——用户多半是说错了日子。"""
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    when = parse_when("昨天下午三点", NOW)
    assert when is not None and when.in_past
    r = act.execute(_schedule_ctx("x", when=when, title="开会"))
    assert not r.ok and "已经过去" in r.message
    assert len(store_for()) == 0


def test_saying_it_twice_does_not_create_two():
    """用户不确定第一遍成没成，会再说一遍。这是实测到的真实行为。"""
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    when = parse_when("今天下午三点", NOW)
    assert when is not None

    class Real(ScheduleAction):
        pass

    # 第一次真的建（绕开 dry-run），第二次同样的内容应当被识别成重复
    ctx1 = ActionContext(text="x", normalized="x", dry_run=False,
                         slots={"when": when, "title": "开会"})
    r1 = act.execute(ctx1)
    assert r1.ok
    ctx2 = ActionContext(text="x", normalized="x", dry_run=False,
                         slots={"when": when, "title": "开会"})
    r2 = act.execute(ctx2)
    assert r2.ok and "已经有一条" in r2.message
    assert len(store_for()) == 1


def test_list_reports_what_is_still_ahead():
    """列表读的是 store 的"接下来"。store 用真实时钟，所以这里固定时钟再建。

    不固定的话这条用例在真实日期走过 2026-10-05 之后就再也看不到那条日程了
    ——测试会因为"今天是几号"而变红，这是最烦人的一类不稳定。
    """
    store = ScheduleStore(store_for().path, clock=lambda: NOW)
    store.add("开会", NOW + timedelta(hours=2))
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    act._store = lambda: store  # type: ignore[method-assign] - 只在这一条用例里换成固定时钟的
    r = act.execute(ActionContext(text="接下来有什么日程", normalized="x", dry_run=False, slots={"op": "list"}))
    assert r.ok and "开会" in r.message


def test_cancel_finds_by_title():
    store = store_for()
    ev = store.add("开会", NOW + timedelta(hours=2))
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    r = act.execute(ActionContext(text="x", normalized="x", dry_run=False,
                                  slots={"op": "cancel", "title": "开会"}))
    assert r.ok and "已取消" in r.message
    assert store.get(ev.id) is None


def test_cancel_without_a_match_says_so():
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    r = act.execute(ActionContext(text="x", normalized="x", dry_run=False,
                                  slots={"op": "cancel", "title": "不存在的会"}))
    assert not r.ok and "没找到" in r.message


def test_note_keeps_the_users_own_words():
    """日程里存一份原话。标题可能被提炼过，原话是核对"我当时到底说了什么"的唯一依据。"""
    act = ScheduleAction(ActionConfig(id="schedule", handler="schedule", aliases=["日程"]))
    when = parse_when("今天下午三点", NOW)
    assert when is not None
    act.execute(ActionContext(
        text="设置今天下午三点的日程我要玩游戏", normalized="x", dry_run=False,
        slots={"when": when, "title": "我要玩游戏", "source": "设置今天下午三点的日程我要玩游戏"},
    ))
    ev = store_for().pending()[0]
    assert ev.note == "设置今天下午三点的日程我要玩游戏"
    assert ev.title == "我要玩游戏"


# --------------------------------------------------------------------------- #
# 关闭进程：只测纯函数，绝不真的杀进程
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "want"),
    [("微信", "微信.exe"), ("Weixin.exe", "weixin.exe"), ("QQ", "qq.exe"),
     (r"C:\Program Files\x\Weixin.exe", "weixin.exe"), ('"notepad.exe"', "notepad.exe"),
     ("", ""), ("   ", "")],
)
def test_normalize_exe(raw: str, want: str):
    """`taskkill /IM` 要的是**纯文件名**，给路径它不认；大小写也不认。"""
    assert normalize_exe(raw) == want


def test_explorer_gets_a_window_close_not_a_kill():
    """explorer.exe 就是桌面本身。杀它会把任务栏和图标一起带走。"""
    plan = close_plan_for(["explorer.exe"], has_window=lambda e: True)
    assert plan == [("explorer.exe", "explorer")]


def test_a_windowed_process_is_closed_gracefully_first():
    """有窗口的进程不带 /F：那是"请关闭"，它来得及让用户存盘。

    /F 是立刻终止，Word 里没保存的文档会直接没——这是不可接受的数据损失。
    """
    plan = close_plan_for(["winword.exe"], has_window=lambda e: True)
    assert plan == [("winword.exe", "taskkill")]


def test_a_headless_process_can_only_be_forced():
    """没有窗口的进程，`taskkill` 不带 /F 会直接失败，只能强制。"""
    plan = close_plan_for(["weixin.exe"], has_window=lambda e: False)
    assert plan == [("weixin.exe", "taskkill-f")]


def test_force_flag_overrides_graceful():
    plan = close_plan_for(["微信"], has_window=lambda e: True, force=True)
    assert plan == [("微信.exe", "taskkill-f")]


def test_close_dry_run_never_kills():
    """dry-run 下 close_app 只报告。走真实执行路径的话，跑一次测试就会关掉用户的程序。"""
    cfg = AppConfig(actions=[ActionConfig(id="sys.close_app", handler="close_app", aliases=["关闭"])])
    reg = build_registry(cfg.enabled_actions)
    act = reg.get("sys.close_app")
    assert act is not None
    r = act.execute(ActionContext(text="关闭微信", dry_run=True, extra={"exe_names": ["weixin.exe"]}))
    assert r.ok and "dry-run" in r.message


# --------------------------------------------------------------------------- #
# 接线：三层顺序 + 槽位进对袋子
# --------------------------------------------------------------------------- #


def test_schedule_intent_reaches_the_action_with_slots():
    """端到端：一句话 → 日程动作，时间和标题都要真的到位。"""
    out = _pipe().process_text("明天早上八点提醒我开会", dry_run=True)
    assert out.via == "intent" and out.action_id == "schedule"
    assert out.result is not None and out.result.ok
    assert "08:00" in out.result.message and "开会" in out.result.message


def test_schedule_slots_go_into_slots_not_extra():
    """日程 handler 读 `ctx.slots`。填进 `extra` 的话它照样"成功"，只是没有时间。"""
    pipe = _pipe()
    out = pipe.process_text("明天早上八点提醒我开会", dry_run=True)
    assert out.intent is not None and out.intent.when is not None
    kv = pipe.slots_for(out.intent, out.match)  # type: ignore[arg-type]
    assert "when" in kv and "title" in kv


def test_dynamic_open_uses_the_index_and_goes_through_open_target():
    out = _pipe().process_text("打开QQ", dry_run=True)
    assert out.via == "intent" and out.action_id == "open.target"
    assert out.result is not None and out.result.ok
    assert "QQ" in out.result.message


def test_dynamic_open_does_not_raise_when_the_target_is_dynamic():
    """回归：`open_target.execute` 曾经引用了一个没导入的 `AppEntry`。

    表现是 `NameError` 被 Pipeline 的异常兜底吞成「动作抛出异常」——功能看着
    "接好了"，实际上一个应用都打不开。
    """
    out = _pipe().process_text("打开QQ", dry_run=False)
    assert out.result is not None
    assert "抛出异常" not in out.result.message
    assert "NameError" not in out.result.message


def test_close_intent_picks_the_close_handler_not_the_open_one():
    """头号缺陷的端到端形态：`关闭微信` 不许落到 `open.wechat` 上。"""
    out = _pipe().process_text("关闭微信", dry_run=True)
    assert out.action_id == "sys.close_app"
    assert out.action_id != "open.wechat"
    assert out.intent is not None and out.intent.polarity == "close"


def test_negation_never_reaches_any_action():
    out = _pipe().process_text("不要打开记事本", dry_run=False)
    assert out.action_id is None and out.result is None
    assert out.intent is not None and out.intent.polarity == "negate"


def test_intent_can_be_turned_off_and_the_old_path_still_works():
    """意图层必须可以整个关掉——真觉得它误判了，行为要能退回 0.2.0。"""
    pipe = _pipe()
    pipe.intent_enabled = False
    out = pipe.process_text("打开微信", dry_run=True)
    assert out.via == "matcher" and out.action_id == "open.wechat"


def test_a_configured_action_still_wins_over_the_index():
    """用户亲手配的 target/args 必须算数，不能被"索引里也认得这个名字"顶掉。"""
    out = _pipe().process_text("打开微信", dry_run=True)
    assert out.action_id == "open.wechat"
    assert out.intent is not None and out.intent.app is not None
    assert out.intent.app.action_id == "open.wechat"


def test_weak_alias_match_is_not_executed_when_the_intent_says_not_found():
    """「打开QQ音乐」而没装：不许勉强命中别的动作。

    实测 `open.browser` 会以 0.633 冒出来并**真的打开浏览器**——用户说的是
    QQ 音乐，得到的却是一个浏览器窗口。这种"勉强命中"比不执行更糟。
    """
    out = _pipe().process_text("打开QQ音乐", dry_run=False)
    assert out.action_id is None, f"不该执行 {out.action_id}"
    assert "没找到" in out.note and "QQ音乐" in out.note


def test_unknown_app_still_runs_when_the_user_bound_it_explicitly():
    """但用户**明确**写过一条动作绑定它时，照旧执行——那是用户自己的配置。"""
    cfg = AppConfig(actions=[
        ActionConfig(id="open.qqmusic", handler="open_app", aliases=["QQ音乐"],
                     target=r"C:\QQMusic\QQMusic.exe", describe="打开QQ音乐"),
    ])
    out = _pipe(cfg, apps=False).process_text("打开QQ音乐", dry_run=True)
    assert out.action_id == "open.qqmusic" and out.result is not None and out.result.ok
