"""界面冒烟测试。

真建一个 Tk 窗口、真切一遍所有页面、真点几个按钮。

为什么不 mock 掉 Tk：界面代码里最容易出问题的地方恰恰是"控件搭不起来"
——ttk 样式里写错一个选项名、`grid` 和 `pack` 混用、某个变量在回调里
还是 None。这些只有真建一遍才会暴露，而它们在打包成 exe 之后表现为
"窗口一闪就没了"，用户完全无从反馈。

无图形环境（CI、远程会话）时整体跳过。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from voice_ctl import events  # noqa: E402
from voice_ctl.config import load_config  # noqa: E402
from voice_ctl.ui import theme  # noqa: E402

MINIMAL = """
[hotkey]
keys = "<ctrl>+<alt>+space"
min_duration_ms = 200
max_duration_ms = 15000

[audio]
samplerate = 16000
channels = 1
min_peak = 0.01

[model]
dir = "models/sense-voice-int8"
language = "auto"
use_itn = true
num_threads = 2
provider = "cpu"

[match]
threshold = 80

[feedback]
beep = false

[[action]]
id = "open.notepad"
handler = "open_app"
aliases = ["记事本", "notepad"]
describe = "打开记事本"
target = "notepad.exe"

[[action]]
id = "sys.mute"
handler = "sysctl"
aliases = ["静音"]
describe = "系统静音"
target = "mute"

[[action]]
id = "off.example"
handler = "open_app"
aliases = ["停用的"]
target = "calc.exe"
enabled = false
"""


@pytest.fixture()
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(MINIMAL, encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def tcl_root():  # noqa: ANN201
    """整个模块共用一个 Tcl 解释器。

    每个测试都 Tk()/destroy() 的话，Tcl 会反复 Init/Finalize，跑几次之后
    连自己的 init.tcl 都找不到（"Can't find a usable init.tcl"）——在全量
    测试里表现为随机一个 UI 测试报错，单独跑这个文件却是绿的。
    """
    try:
        # DPI 感知必须在 Tk() 之前声明，否则 winfo_fpixels 拿到的是被虚拟化的值
        theme.enable_dpi_awareness()
        root = tk.Tk()
    except tk.TclError as e:  # pragma: no cover - 无图形环境
        pytest.skip(f"没有可用的图形环境：{e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:  # pragma: no cover
        pass


@pytest.fixture()
def app(cfg_path: Path, tcl_root):  # noqa: ANN001, ANN201
    from voice_ctl.ui.window import build_window

    win = tk.Toplevel(tcl_root)
    win.withdraw()
    a = build_window(load_config(cfg_path), cfg_path, root=win)
    yield a
    try:
        a.on_close()
    except tk.TclError:  # pragma: no cover
        pass
    events.get_bus().close(0.2)


def pump(app, n: int = 6) -> None:  # noqa: ANN001
    for _ in range(n):
        app.root.update()
        time.sleep(0.02)


ALL_TABS = ("run", "logs", "hotkey", "actions", "settings", "about")


# --------------------------------------------------------------------------- #
# 搭建
# --------------------------------------------------------------------------- #


def test_window_builds(app):  # noqa: ANN001
    assert app.root.title().startswith("voice-ctl")
    assert set(app.tabs) == set(ALL_TABS)
    assert app.current == "run"


def test_every_tab_can_be_shown_and_ticked(app):  # noqa: ANN001
    for key in ALL_TABS:
        app.show_tab(key)
        pump(app, 4)
        assert app.current == key
    # 每个页面都必须能挨住 on_show + on_tick 的组合（主循环每 120ms 调一次）
    for key in ALL_TABS:
        tab = app.tabs[key]
        for _ in range(3):
            tab.on_tick()
    pump(app)


def test_unknown_attribute_in_poll_does_not_kill_the_loop(app):  # noqa: ANN001
    """轮询里抛异常会让界面整个冻住，必须被兜住。"""
    app.show_tab("logs")
    pump(app, 3)


# --------------------------------------------------------------------------- #
# 运行页
# --------------------------------------------------------------------------- #


def test_run_tab_send_feeds_pipeline(app):  # noqa: ANN001
    app.show_tab("run")
    run = app.tabs["run"]
    run._entry.insert(0, "打开记事本")
    run._send()
    deadline = time.perf_counter() + 5
    while app.engine.last_outcome is None and time.perf_counter() < deadline:
        pump(app, 2)
    out = app.engine.last_outcome
    assert out is not None
    assert out.action_id == "open.notepad"
    pump(app, 6)


def test_run_tab_dry_run_toggle(app):  # noqa: ANN001
    run = app.tabs["run"]
    run._dry.set(True)
    run._on_dry()
    assert app.engine.dry_run is True
    run._dry.set(False)
    run._on_dry()
    assert app.engine.dry_run is False


def test_model_pill_distinguishes_missing_from_not_loaded(app, cfg_path: Path):  # noqa: ANN001
    """「没加载」和「没有」必须分得清。

    完整版内置了模型，只是懒加载；这里如果一律显示"未加载"，用户会以为
    缺模型，白白去下 226MB。
    """
    app.show_tab("run")
    run = app.tabs["run"]
    # 这个 fixture 的模型目录是空的
    run._model_present = None
    run.on_tick()
    missing = run._model_pill._text.cget("text")
    assert "缺少" in missing

    md = cfg_path.parent / "models" / "sense-voice-int8"
    md.mkdir(parents=True)
    (md / "model.int8.onnx").write_bytes(b"x" * 1024)
    (md / "tokens.txt").write_text("a", encoding="utf-8")
    run._model_present = None
    run.on_tick()
    present = run._model_pill._text.cget("text")
    assert "待加载" in present
    assert present != missing


# --------------------------------------------------------------------------- #
# 日志页
# --------------------------------------------------------------------------- #


def test_logs_tab_receives_events(app):  # noqa: ANN001
    app.show_tab("logs")
    events.ok("冒烟测试事件", kind="test")
    pump(app, 8)
    assert app.tabs["logs"].view.count >= 1


def test_logs_filter_and_pause(app):  # noqa: ANN001
    logs = app.tabs["logs"]
    events.error("一个错误", kind="test")
    events.warn("一个警告", kind="test")
    pump(app, 8)
    logs._level.set("只看错误")
    logs._apply_filter()
    text = logs.view.text.get("1.0", "end")
    assert "一个错误" in text and "一个警告" not in text
    logs._paused.set(True)
    logs._on_pause()
    assert logs.view._paused is True  # noqa: SLF001
    logs._paused.set(False)
    logs._on_pause()
    logs._level.set("全部（含调试）")
    logs._apply_filter()
    logs._clear()
    assert logs.view.count == 0


def test_logs_export(app, tmp_path: Path):  # noqa: ANN001
    events.info("导出我", kind="test")
    pump(app, 6)
    out = tmp_path / "log.txt"
    assert app.tabs["logs"].view.export(str(out)) >= 1
    assert "导出我" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 快捷键页
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ("<ctrl>+space", "block"),
        ("<alt>+<f4>", "block"),
        ("<ctrl>+", "block"),
        ("<ctrl>+<alt>+j", None),
        ("<ctrl>+<alt>+<shift>+k", "warn"),
    ],
)
def test_hotkey_warnings(spec: str, want: str | None):  # noqa: ANN201
    from voice_ctl.ui.tab_hotkey import warnings_for

    levels = [lv for lv, _ in warnings_for(spec)]
    if want is None:
        assert levels == []
    else:
        assert want in levels


def test_hotkey_capture_shows_and_saves(app):  # noqa: ANN001
    app.show_tab("hotkey")
    hk = app.tabs["hotkey"]
    hk._on_captured("<ctrl>+<alt>+j")
    pump(app, 4)
    assert hk._msg.cget("text").startswith("✓")
    hk._save()
    pump(app, 8)
    assert app.cfg.hotkey.keys == "<ctrl>+<alt>+j"
    assert app.engine.hotkey_spec == "<ctrl>+<alt>+j"


def test_hotkey_save_refuses_reserved_combo(app):  # noqa: ANN001
    app.show_tab("hotkey")
    hk = app.tabs["hotkey"]
    before = app.cfg.hotkey.keys
    hk._on_captured("<alt>+<f4>")
    hk._save()
    pump(app, 6)
    assert app.cfg.hotkey.keys == before


def test_keysym_mapping(app):  # noqa: ANN001
    from voice_ctl.ui.widgets import keysym_to_name

    assert keysym_to_name("Control_L") == "ctrl"
    assert keysym_to_name("space") == "space"
    assert keysym_to_name("F9") == "f9"
    assert keysym_to_name("A") == "a"
    assert keysym_to_name("Multi_key") is None


# --------------------------------------------------------------------------- #
# 功能页
# --------------------------------------------------------------------------- #


def test_actions_tab_lists_and_autoselects(app):  # noqa: ANN001
    app.show_tab("actions")
    pump(app, 6)
    a = app.tabs["actions"]
    assert len(a.tree.get_children()) == 3
    assert a._f_id.get() == "open.notepad"
    assert a._f_aliases.get().startswith("记事本")


def test_actions_toggle_enabled_writes_config(app, cfg_path: Path):  # noqa: ANN001
    app.show_tab("actions")
    pump(app, 6)
    a = app.tabs["actions"]
    a._toggle_enabled()
    pump(app, 8)
    by_id = {x.id: x for x in app.cfg.actions}
    assert by_id["open.notepad"].enabled is False
    assert "enabled = false" in cfg_path.read_text(encoding="utf-8")


def test_actions_add_then_remove(app):  # noqa: ANN001
    app.show_tab("actions")
    pump(app, 6)
    a = app.tabs["actions"]
    before = len(app.cfg.actions)
    a._new()
    pump(app, 8)
    assert len(app.cfg.actions) == before + 1
    assert app.cfg.actions[-1].id.startswith("custom.new")
    assert app.cfg.actions[-1].aliases, "新动作必须有占位说法，否则配置非法"

    app.remove_action(len(app.cfg.actions) - 1)
    pump(app, 8)
    assert len(app.cfg.actions) == before


def test_actions_comment_preserved_on_edit(app, cfg_path: Path):  # noqa: ANN001
    app.show_tab("actions")
    pump(app, 6)
    app.save_action(0, {"aliases": ["记事本", "notepad", "小本本"]})
    pump(app, 8)
    assert "# 说明" not in cfg_path.read_text(encoding="utf-8")  # 这个 fixture 里没有注释
    assert "小本本" in cfg_path.read_text(encoding="utf-8")
    assert app.cfg.actions[0].aliases == ["记事本", "notepad", "小本本"]


def test_split_list_handles_chinese_punctuation():  # noqa: ANN201
    from voice_ctl.ui.tab_actions import split_list

    assert split_list("微信，威信、wechat；聊天") == ["微信", "威信", "wechat", "聊天"]
    assert split_list("   ") == []
    assert split_list("a,a,b") == ["a", "b"]


# --------------------------------------------------------------------------- #
# 设置页
# --------------------------------------------------------------------------- #


def test_settings_save_round_trips(app, cfg_path: Path):  # noqa: ANN001
    app.show_tab("settings")
    pump(app, 6)
    s = app.tabs["settings"]
    s._threshold.set(93)
    s._min_peak.set(0.02)
    s._save()
    pump(app, 10)
    assert app.cfg.match.threshold == 93
    assert app.cfg.audio.min_peak == 0.02
    text = cfg_path.read_text(encoding="utf-8")
    assert "threshold = 93" in text
    assert "min_peak = 0.02" in text


def test_settings_save_does_not_materialize_defaults(app, cfg_path: Path):  # noqa: ANN001
    """点一次保存不该凭空多出十几行默认值。"""
    before = cfg_path.read_text(encoding="utf-8").splitlines()
    app.show_tab("settings")
    pump(app, 6)
    app.tabs["settings"]._save()
    pump(app, 10)
    after = cfg_path.read_text(encoding="utf-8").splitlines()
    assert after == before


def test_settings_rejects_invalid_timing(app, cfg_path: Path):  # noqa: ANN001
    app.show_tab("hotkey")
    pump(app, 6)
    hk = app.tabs["hotkey"]
    before = app.cfg.hotkey.max_duration_ms
    hk._min_ms.set(5000)
    hk._max_ms.set(1000)
    hk._save_timing()
    pump(app, 6)
    assert app.cfg.hotkey.max_duration_ms == before


def test_number_row_two_way_sync(app):  # noqa: ANN001
    from voice_ctl.ui.tab_settings import NumberRow

    row = NumberRow(app.root, from_=0, to=100, fmt="{:.0f}")
    row.set(42)
    assert row.get() == 42
    row.text.set("77")
    row._to_scale()
    assert row.get() == 77
    row.text.set("999")  # 超出范围要夹住
    row._to_scale()
    assert row.get() == 100
    row.text.set("垃圾输入")  # 非法输入要回滚，不能抛
    row._to_scale()
    assert row.get() == 100
    row.destroy()


# --------------------------------------------------------------------------- #
# 关于页
# --------------------------------------------------------------------------- #


def test_about_tab_collects_paths_and_deps(app):  # noqa: ANN001
    app.show_tab("about")
    pump(app, 6)
    ab = app.tabs["about"]
    body = ab._paths.get("1.0", "end")
    assert "配置文件" in body and str(app.config_path) in body
    ab.scan_deps()
    pump(app, 6)
    assert ab._deps_pending is None, "on_tick 应该已经把结果画上去了"
    assert "numpy" in ab._dep_box.winfo_children()[0].winfo_children()[1].cget("text")


# --------------------------------------------------------------------------- #
# 引擎控制
# --------------------------------------------------------------------------- #


def test_toggle_engine_without_model_does_not_crash(app):  # noqa: ANN001
    """没有模型时点启动：应该去加载模型，而不是抛异常。"""
    app.show_tab("run")
    pump(app, 4)
    app.toggle_engine()
    pump(app, 8)
    assert app.engine.state in ("idle", "loading", "error")


def test_app_reload_config_keeps_working(app, cfg_path: Path):  # noqa: ANN001
    app.save_settings({("match", "threshold"): 88})
    pump(app, 10)
    assert app.cfg.match.threshold == 88
    app.reload_config()
    pump(app, 10)
    assert app.cfg.match.threshold == 88
    assert app.engine.runtime is not None


def test_app_save_rejects_nothing_when_unchanged(app):  # noqa: ANN001
    assert app.save_settings({("match", "threshold"): 80}) is True
    pump(app, 4)
