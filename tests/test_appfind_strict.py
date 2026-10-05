"""应用名严格匹配 / 预热 / 限频刷新 / 解析缓存。

起点是一次实测：`resolve_app('QQ音乐')` 启动了 QQ（旧的包含关系加分给了 0.90），
`'visual studio code'` 解析成了 Visual Studio Installer（difflib 0.73 > 阈值 0.72）。
在 `launch` 之前出错的后果是**静默启动一个用户没要的程序**。
"""

from __future__ import annotations

import threading
import time

import pytest

from voice_ctl import appfind
from voice_ctl.actions import ActionContext, OpenAppAction
from voice_ctl.appfind import (
    STRICT_FLOOR,
    Resolved,
    _Loose,
    from_start_menu,
    name_tokens,
    resolve_app,
    strict_score,
)
from voice_ctl.config import ActionConfig

APPS = [
    ("QQ", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Tencent\QQNT\QQ.exe"),
    ("微信", r"E:\weixin\Weixin.exe"),
    ("微信开发者工具", r"D:\WXKFZ\微信开发者工具.exe"),
    ("Visual Studio Installer", "Microsoft.VisualStudio.Installer_8wekyb3d8bbwe!App"),
    ("Visual Studio Code", r"C:\Users\x\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    ("Steam", r"C:\Steam\steam.exe"),
    ("Steam Support Center", r"C:\Steam\support.exe"),
    ("Google Chrome", r"C:\Chrome\chrome.exe"),
    ("记事本", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
]


@pytest.fixture()
def menu(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setattr(appfind, "_START_APPS", list(APPS))
    return APPS


# --------------------------------------------------------------------------- #
# strict_score
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("query", "shown", "lo", "hi"),
    [
        ("qq", "QQ", 1.0, 1.0),
        ("QQ 音乐", "QQ音乐", 0.97, 0.99),  # 只差空格
        ("微信", "微信", 1.0, 1.0),
        ("code", "Visual Studio Code", 0.85, 0.9),  # 简称：每个词元都是名字里完整的词元
        ("chrome", "Google Chrome", 0.85, 0.9),
        ("steam", "Steam Support Center", 0.85, 0.9),
    ],
)
def test_expected_high_scores(query: str, shown: str, lo: float, hi: float):
    assert lo <= strict_score(query, shown) <= hi


@pytest.mark.parametrize(
    ("query", "shown"),
    [
        ("QQ音乐", "QQ"),  # 用户说的是另一个应用
        ("腾讯QQ", "QQ"),  # 这种说法交给昵称表，不靠包含关系碰运气
        ("visual studio code", "Visual Studio Installer"),
        ("微信开发者工具", "微信"),
        ("计算器", "网易云音乐"),
        ("a", "apple"),  # 单字符不该靠包含关系命中
        ("微", "微信"),
    ],
)
def test_must_not_pass_the_strict_floor(query: str, shown: str):
    assert strict_score(query, shown) < STRICT_FLOOR


def test_containment_direction_is_what_matters():
    """旧版两个方向都给 0.8+，所以说「QQ音乐」和说「音乐」得分一样高。"""
    longer_query = strict_score("QQ音乐", "QQ")
    shorter_query = strict_score("网易云", "网易云音乐")
    assert longer_query < shorter_query


def test_partial_name_without_whole_token_scores_below_whole_token():
    whole = strict_score("code", "Visual Studio Code")
    part = strict_score("网易云", "网易云音乐")
    assert part < whole
    assert part >= 0.6, "仍要高到足够让界面问一句「是想打开 X 吗？」"


def test_empty_inputs_score_zero():
    assert strict_score("", "x") == 0.0
    assert strict_score("x", "") == 0.0
    assert strict_score("！！", "？？") == 0.0


def test_name_tokens_split_ascii_and_cjk_runs():
    assert name_tokens("QQ音乐") == ["qq", "音乐"]
    assert name_tokens("Visual Studio Code") == ["visual", "studio", "code"]
    assert name_tokens("  ") == []


# --------------------------------------------------------------------------- #
# from_start_menu / resolve_app
# --------------------------------------------------------------------------- #


def test_resolve_does_not_launch_a_different_app_for_a_longer_name(menu):  # noqa: ANN001
    r = from_start_menu(["QQ音乐"])
    assert r is None, f"说「QQ音乐」不该落到 {r.display if r else ''}"


def test_resolve_prefers_exact_over_longer_names(menu):  # noqa: ANN001
    r = from_start_menu(["微信"])
    assert r is not None and r.display == "微信"
    r = from_start_menu(["steam"])
    assert r is not None and r.display == "Steam"


def test_resolve_does_not_confuse_vs_code_with_installer(menu):  # noqa: ANN001
    r = from_start_menu(["visual studio code"])
    assert r is not None and r.display == "Visual Studio Code"
    r2 = from_start_menu(["vs installer"])
    assert r2 is None or r2.display != "Visual Studio Code"


def test_describe_keeps_loose_matching(menu):  # noqa: ANN001
    """describe 是一整句话，应用名只是其中一部分——这条路必须保持宽松，否则
    「打开微信，用来聊天」这类配置会全部失效。"""
    assert from_start_menu(["完全不相干", _Loose("打开记事本写点东西")]) is not None
    r = from_start_menu([_Loose("打开记事本")])
    assert r is not None and r.display == "记事本"


def test_strict_names_do_not_get_the_loose_treatment(menu):  # noqa: ANN001
    """同样的句子，当成别名（严格）就不该命中——证明 _Loose 标记确实在起作用。"""
    assert from_start_menu(["打开记事本写点东西"]) is None


def test_first_name_wins_on_equal_score(menu):  # noqa: ANN001
    r = from_start_menu(["Steam", "steam"])
    assert r is not None and r.display == "Steam"


def test_resolve_app_wraps_describe_as_loose(menu, monkeypatch):  # noqa: ANN001
    seen: list[list[str]] = []
    real = appfind.from_start_menu

    def spy(names):  # noqa: ANN001, ANN202
        seen.append(list(names))
        return real(names)

    monkeypatch.setattr(appfind, "from_start_menu", spy)
    resolve_app("", ["瞎写"], "打开记事本")
    assert any(isinstance(n, _Loose) for n in seen[0])
    assert not any(isinstance(n, _Loose) for n in seen[0][:2])


# --------------------------------------------------------------------------- #
# 预热 / 并发 / 限频刷新
# --------------------------------------------------------------------------- #


def test_concurrent_loads_run_powershell_once(monkeypatch: pytest.MonkeyPatch):
    """预热线程和第一次说话几乎同时到：不该起两个 PowerShell。"""
    monkeypatch.setattr(appfind, "_START_APPS", None)
    monkeypatch.setattr(appfind.sys, "platform", "win32")
    calls = {"n": 0}

    class R:
        stdout = "甲\tA\n乙\tB\n"

    def fake_run(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        time.sleep(0.15)
        return R()

    monkeypatch.setattr(appfind.subprocess, "run", fake_run)
    results: list[object] = []
    ts = [threading.Thread(target=lambda: results.append(appfind.start_apps())) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert calls["n"] == 1, f"并发加载起了 {calls['n']} 个 PowerShell"
    assert all(r is results[0] for r in results), "所有线程应拿到同一份缓存"
    assert results[0] == [("甲", "A"), ("乙", "B")]


def test_warm_is_noop_when_already_cached(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(appfind, "_START_APPS", [("x", "y")])
    assert appfind.warm_start_apps() is None


def test_warm_loads_in_background_thread(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(appfind, "_START_APPS", None)
    monkeypatch.setattr(appfind.sys, "platform", "win32")
    loaded = threading.Event()
    main = threading.get_ident()
    where: list[int] = []

    def fake_start_apps(refresh: bool = False):  # noqa: ANN202, ARG001
        where.append(threading.get_ident())
        loaded.set()
        return []

    monkeypatch.setattr(appfind, "start_apps", fake_start_apps)
    t = appfind.warm_start_apps()
    assert t is not None
    assert loaded.wait(2)
    assert where and where[0] != main, "预热必须在后台线程里跑，不能阻塞调用方"


def test_failed_load_is_not_cached_forever(monkeypatch: pytest.MonkeyPatch):
    """PowerShell 偶尔失败一次，不能让"没有任何应用"一直缓存到重启。"""
    monkeypatch.setattr(appfind, "_START_APPS", None)
    monkeypatch.setattr(appfind, "_START_AT", 0.0)
    monkeypatch.setattr(appfind.sys, "platform", "win32")

    def boom(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
        raise OSError("powershell 不见了")

    monkeypatch.setattr(appfind.subprocess, "run", boom)
    assert appfind.start_apps() == []
    assert appfind._START_AT > 0, "失败也要记时间，限频才算得对"

    class R:
        stdout = "甲\tA\n"

    monkeypatch.setattr(appfind.subprocess, "run", lambda *_a, **_k: R())
    monkeypatch.setattr(appfind, "_START_AT", time.monotonic() - 999)  # 已过限频间隔
    assert appfind.refresh_start_apps_if_stale() is True
    assert appfind._START_APPS == [("甲", "A")]


def test_refresh_is_rate_limited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(appfind.sys, "platform", "win32")
    monkeypatch.setattr(appfind, "_START_AT", time.monotonic())
    called = []
    monkeypatch.setattr(appfind, "start_apps", lambda refresh=False: called.append(refresh) or [])
    assert appfind.refresh_start_apps_if_stale(30) is False, "刚扫过，不该再扫"
    assert called == []
    monkeypatch.setattr(appfind, "_START_AT", time.monotonic() - 31)
    assert appfind.refresh_start_apps_if_stale(30) is True
    assert called == [True]


def test_resolve_refreshes_once_on_miss_and_finds_new_app(monkeypatch: pytest.MonkeyPatch):
    """用户刚装好应用：第一次没找到 → 重扫 → 找到。以前要重启 voice-ctl 才认。"""
    monkeypatch.setattr(appfind, "_START_APPS", [("计算器", "x!App")])
    monkeypatch.setattr(appfind, "_START_AT", 0.0)
    monkeypatch.setattr(appfind.sys, "platform", "win32")

    def fake_start_apps(refresh: bool = False):  # noqa: ANN202
        if refresh:
            monkeypatch.setattr(appfind, "_START_APPS", [("计算器", "x!App"), ("新装的软件", r"C:\n\new.exe")])
            monkeypatch.setattr(appfind, "_START_AT", time.monotonic())
        return appfind._START_APPS

    monkeypatch.setattr(appfind, "start_apps", fake_start_apps)
    miss = resolve_app("新装的软件", [], "")
    assert not miss.ok, "默认不刷新：体检/预检不能每次都去起 PowerShell"
    hit = resolve_app("新装的软件", [], "", refresh_on_miss=True)
    assert hit.ok and hit.display == "新装的软件"


# --------------------------------------------------------------------------- #
# OpenAppAction：缓存与执行
# --------------------------------------------------------------------------- #


def _action(**kw) -> OpenAppAction:  # noqa: ANN003
    cfg = ActionConfig(id="t", handler="open_app", aliases=["测试应用"], target=kw.pop("target", ""), **kw)
    return OpenAppAction(cfg)


def test_open_app_caches_successful_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path):  # noqa: ANN001
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"MZ")
    calls = {"n": 0}

    def fake_resolve(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        return Resolved("exe", str(exe), "测试")

    monkeypatch.setattr("voice_ctl.actions.resolve_app", fake_resolve)
    a = _action()
    for _ in range(5):
        assert a.preflight().ok
    a.execute(ActionContext(dry_run=True))
    assert calls["n"] == 1, "同一个动作不该每次都重新解析"


def test_open_app_cache_invalidated_when_exe_disappears(monkeypatch: pytest.MonkeyPatch, tmp_path):  # noqa: ANN001
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"MZ")
    calls = {"n": 0}

    def fake_resolve(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        return Resolved("exe", str(exe), "测试")

    monkeypatch.setattr("voice_ctl.actions.resolve_app", fake_resolve)
    a = _action()
    assert a.preflight().ok
    exe.unlink()
    assert a.preflight().ok  # 重新解析（fake 仍返回同一路径，但缓存必须已作废）
    assert calls["n"] == 2


def test_open_app_does_not_cache_failures(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def fake_resolve(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        return Resolved("fail", "", "没找到")

    monkeypatch.setattr("voice_ctl.actions.resolve_app", fake_resolve)
    a = _action()
    assert not a.preflight().ok
    assert not a.preflight().ok
    assert calls["n"] == 2, "没找到时下一次要重新找（用户可能刚装好）"


def test_open_app_refresh_only_when_really_executing(monkeypatch: pytest.MonkeyPatch):
    flags: list[bool] = []

    def fake_resolve(*_a, refresh_on_miss=False, **_k):  # noqa: ANN002, ANN003, ANN202
        flags.append(refresh_on_miss)
        return Resolved("fail", "", "没找到")

    monkeypatch.setattr("voice_ctl.actions.resolve_app", fake_resolve)
    a = _action()
    a.preflight()
    a.execute(ActionContext(dry_run=True))
    a.execute(ActionContext(dry_run=False))
    assert flags == [False, False, True]


def test_open_app_launch_failure_clears_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):  # noqa: ANN001
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr("voice_ctl.actions.resolve_app", lambda *a, **k: Resolved("exe", str(exe), "测试"))

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("拒绝访问")

    monkeypatch.setattr("voice_ctl.actions.launch_resolved", boom)
    a = _action()
    res = a.execute(ActionContext())
    assert not res.ok and "启动失败" in res.message
    assert a._cached is None


# --------------------------------------------------------------------------- #
# 原文透传
# --------------------------------------------------------------------------- #


def test_action_context_receives_raw_text_not_normalized():
    """归一化会改写内容；日程标题这类自由文本必须用 ASR 原文。"""
    from pathlib import Path

    from voice_ctl.actions import Action, ActionResult, build_registry
    from voice_ctl.app import Pipeline
    from voice_ctl.config import load_config
    from voice_ctl.matcher import Matcher
    from voice_ctl.normalize import NormalizeConfig, Normalizer

    cfg = load_config(Path(__file__).resolve().parent.parent / "config.toml")
    norm = Normalizer(NormalizeConfig(
        strip_prefixes=cfg.match.strip_prefixes, strip_suffixes=cfg.match.strip_suffixes,
        inline_fillers=cfg.match.inline_fillers,
    ))
    pipe = Pipeline(
        asr=None,  # type: ignore[arg-type]
        matcher=Matcher(cfg.enabled_actions, normalizer=norm),
        registry=build_registry(cfg.enabled_actions),
        actions=cfg.enabled_actions,
        normalizer=norm,
    )
    seen: list[ActionContext] = []

    class Spy(Action):
        def execute(self, ctx: ActionContext) -> ActionResult:
            seen.append(ctx)
            return ActionResult(True, "ok")

    pipe.registry._by_id["open.notepad"] = Spy(cfg.actions[0])  # type: ignore[attr-defined]
    pipe.process_text("请帮我打开一下记事本吧", dry_run=True)
    assert seen[0].text == "请帮我打开一下记事本吧", "text 必须是 ASR 原文"
    assert seen[0].normalized == "打开记事本", "normalized 仍是归一化后的"
