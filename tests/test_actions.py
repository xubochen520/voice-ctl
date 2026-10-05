"""动作注册表与 handler 测试（全部走 dry_run / preflight，不真的启动程序）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl.actions import (
    HANDLERS,
    ActionResult,
    ActionContext,
    Registry,
    build_registry,
)
from voice_ctl.appfind import resolve_app
from voice_ctl.config import ActionConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = ROOT / "config.toml"


def ctx(**kw) -> ActionContext:
    kw.setdefault("text", "测试")
    return ActionContext(**kw)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def test_all_config_handlers_are_implemented():
    """配置里用到的每个 handler 都必须有实现——否则用户一启动就报错。"""
    cfg = load_config(REAL_CONFIG)
    for a in cfg.actions:
        assert a.handler in HANDLERS, f"{a.id} 用了没实现的 handler {a.handler}"


def test_build_registry_from_real_config():
    cfg = load_config(REAL_CONFIG)
    reg = build_registry(cfg.enabled_actions)
    assert len(reg) == len(cfg.enabled_actions)
    for a in cfg.enabled_actions:
        assert a.id in reg


def test_registry_rejects_duplicate():
    reg = Registry()
    a = ActionConfig(id="x", handler="open_app", aliases=["记事本"], target="notepad.exe")
    reg.register(HANDLERS["open_app"](a))
    with pytest.raises(ValueError, match="重复"):
        reg.register(HANDLERS["open_app"](a))


def test_build_registry_rejects_unknown_handler():
    a = ActionConfig(id="x", handler="open_app", aliases=["a"], target="notepad.exe")
    a.handler = "nonexistent"
    with pytest.raises(ValueError, match="没有实现"):
        build_registry([a])


# --------------------------------------------------------------------------- #
# preflight（真实配置全绿是硬要求）
# --------------------------------------------------------------------------- #


# 系统自带命令：在任何 Windows 上都必须解析成功
MUST_RESOLVE = {
    "open.notepad",
    "open.calc",
    "open.explorer",
    "open.taskmgr",
    "open.settings",
    "sys.volume_up",
    "sys.volume_down",
    "sys.mute",
    "sys.screenshot",
}


def test_system_actions_preflight_all_pass():
    """系统自带动作的预检必须全绿——这些不依赖用户装了什么。"""
    cfg = load_config(REAL_CONFIG)
    reg = build_registry(cfg.enabled_actions)
    failures = []
    for a in cfg.enabled_actions:
        if a.id not in MUST_RESOLVE:
            continue
        r = reg.get(a.id).preflight()
        if not r.ok:
            failures.append(f"{a.id}: {r.message} [{r.detail}]")
    assert not failures, "以下系统动作预检失败：\n  " + "\n  ".join(failures)


def test_third_party_apps_report_clearly_when_absent():
    """第三方应用（微信等）可能没装。此时必须**清楚地报告找不到**，
    而不是抛异常或给出误导性的成功。"""
    cfg = load_config(REAL_CONFIG)
    reg = build_registry(cfg.enabled_actions)
    reported = 0
    for a in cfg.enabled_actions:
        if a.id in MUST_RESOLVE or a.handler != "open_app":
            continue
        r = reg.get(a.id).preflight()
        if not r.ok:
            reported += 1
            assert "找不到" in r.message
            assert r.detail, "找不到时必须说明尝试过哪些位置"
    # 不强求本机装没装微信，只要求「没装就说清楚」
    assert reported >= 0


def test_doctor_style_preflight_never_raises():
    """doctor 会对每个动作跑 preflight，任何一个抛异常都会让诊断崩掉。

    disabled 的动作不在注册表里，doctor 必须跳过而不是崩。
    """
    cfg = load_config(REAL_CONFIG)
    reg = build_registry(cfg.enabled_actions)
    checked = 0
    for a in cfg.actions:
        act = reg.get(a.id)
        if act is None:
            continue  # disabled，跳过
        act.preflight()
        checked += 1
    assert checked == len(cfg.enabled_actions)


def test_sysctl_rejects_unknown_op():
    a = ActionConfig(id="x", handler="sysctl", aliases=["啥"], target="explode")
    r = HANDLERS["sysctl"](a).preflight()
    assert not r.ok
    assert "不认识" in r.message


def test_keys_rejects_unknown_key():
    a = ActionConfig(id="x", handler="keys", aliases=["啥"], target="ctrl+hyperkey")
    r = HANDLERS["keys"](a).preflight()
    assert not r.ok, "hyperkey 不是合法键名，必须被拒"
    assert "不认识" in r.message


def test_keys_accepts_single_char_and_known_names():
    for spec in ("ctrl+alt+w", "f5", "ctrl+shift+escape", "cmd+d"):
        a = ActionConfig(id="x", handler="keys", aliases=["啥"], target=spec)
        assert HANDLERS["keys"](a).preflight().ok, f"{spec} 应当被接受"


def test_keys_parses_from_args():
    a = ActionConfig(id="x", handler="keys", aliases=["啥"], target="", args=["ctrl", "alt", "w"])
    act = HANDLERS["keys"](a)
    assert act._keys() == ["ctrl", "alt", "w"]


def test_open_url_flags_missing_scheme():
    a = ActionConfig(id="x", handler="open_url", aliases=["啥"], target="example.com")
    r = HANDLERS["open_url"](a).preflight()
    assert r.ok
    assert "https://" in r.detail


def test_open_path_reports_missing():
    a = ActionConfig(id="x", handler="open_path", aliases=["啥"], target=r"C:\definitely\not\here")
    r = HANDLERS["open_path"](a).preflight()
    assert not r.ok
    assert "不存在" in r.message


# --------------------------------------------------------------------------- #
# dry_run 不产生副作用
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("handler_name", ["open_app", "open_url", "sysctl", "keys", "shell"])
def test_dry_run_is_side_effect_free(handler_name: str):
    targets = {
        "open_app": "notepad.exe",
        "open_url": "https://example.com",
        "sysctl": "volume_up",
        "keys": "ctrl+alt+w",
        "shell": "echo hi",
    }
    a = ActionConfig(id="x", handler=handler_name, aliases=["啥"], target=targets[handler_name])
    r = HANDLERS[handler_name](a).execute(ctx(dry_run=True))
    assert r.ok, f"{handler_name} dry-run 失败：{r.message}"
    assert "dry-run" in r.message


def test_dry_run_open_path(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    a = ActionConfig(id="x", handler="open_path", aliases=["啥"], target=str(f))
    r = HANDLERS["open_path"](a).execute(ctx(dry_run=True))
    assert r.ok
    assert "dry-run" in r.message


def test_execute_never_raises_on_bad_target():
    """动作内部异常必须被吃成 ActionResult，不能掀翻主循环。"""
    a = ActionConfig(id="x", handler="open_path", aliases=["啥"], target="\x00invalid")
    r = HANDLERS["open_path"](a).execute(ctx())
    assert isinstance(r, ActionResult)
    assert not r.ok


# --------------------------------------------------------------------------- #
# appfind
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["notepad", "notepad.exe", "calc", "explorer", "taskmgr"])
def test_resolve_system_commands(name: str):
    r = resolve_app(name)
    assert r.ok, f"{name} 解析失败：{r.how}"


def test_resolve_nonsense_fails_with_reason():
    r = resolve_app("definitely-not-a-real-app-xyz")
    assert not r.ok
    assert "尝试过" in r.how


def test_resolve_reports_how():
    r = resolve_app("notepad")
    assert r.how, "必须说明是怎么找到的"


def test_result_describe_readable():
    assert ActionResult(True, "干完了").describe().startswith("✓")
    assert ActionResult(False, "没干成", "因为").describe().startswith("✗")
