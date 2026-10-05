"""应用定位测试。

这批测试源于一个真实缺陷：最初 _KNOWN 表里只有英文名，导致中文用户说
「打开记事本」「计算器」全都解析失败——而默认配置里一半动作都是中文别名，
等于出厂就是坏的。修法是把「开始菜单」提为第二级数据源（覆盖 UWP/商店应用），
并加中文名 → 系统命令的映射。
"""

from __future__ import annotations

import sys

import pytest

from voice_ctl import appfind
from voice_ctl.appfind import (
    Resolved,
    _similarity,
    _system_alias,
    from_start_menu,
    resolve_app,
    start_apps,
)

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows 专有")


# --------------------------------------------------------------------------- #
# 相似度
# --------------------------------------------------------------------------- #


def test_similarity_identical():
    assert _similarity("计算器", "计算器") == 1.0


def test_similarity_case_insensitive():
    assert _similarity("Notepad", "notepad") == 1.0


def test_similarity_containment_scores_high():
    """「微信」应当能匹配到系统显示名「微信」而不是被「微信开发者工具」抢走。"""
    exact = _similarity("微信", "微信")
    partial = _similarity("微信", "微信开发者工具")
    assert exact > partial


def test_similarity_unrelated_is_low():
    assert _similarity("计算器", "网易云音乐") < 0.3


def test_similarity_empty_is_zero():
    assert _similarity("", "x") == 0.0
    assert _similarity("x", "") == 0.0


# --------------------------------------------------------------------------- #
# 中文名 → 系统命令
# --------------------------------------------------------------------------- #


@win_only
@pytest.mark.parametrize(
    ("spoken", "expected_substr"),
    [
        ("记事本", "notepad"),
        ("计算器", "calc"),
        ("任务管理器", "taskmgr"),
        ("资源管理器", "explorer"),
        ("命令行", "cmd"),
        ("画图", "mspaint"),
    ],
)
def test_chinese_names_map_to_system_commands(spoken: str, expected_substr: str):
    r = _system_alias(spoken)
    assert r is not None, f"{spoken!r} 没有映射"
    assert r.ok
    assert expected_substr in r.value.lower(), f"{spoken!r} 映射到了 {r.value}"


def test_unknown_chinese_name_has_no_mapping():
    assert _system_alias("完全不存在的应用名xyz") is None


# --------------------------------------------------------------------------- #
# 开始菜单数据源
# --------------------------------------------------------------------------- #


@win_only
def test_start_apps_returns_entries():
    apps = start_apps()
    assert apps, "开始菜单应当至少返回一些条目（Windows 上不可能为空）"
    assert all(isinstance(n, str) and isinstance(a, str) for n, a in apps)


@win_only
def test_start_apps_is_cached():
    """缓存很重要：每次调用要起一次 PowerShell（200-400ms）。"""
    first = start_apps()
    second = start_apps()
    assert first is second, "结果没有被缓存"


def test_from_start_menu_uses_injected_data(monkeypatch: pytest.MonkeyPatch):
    fake = [
        ("计算器", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
        ("记事本", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
        ("微信", r"E:\weixin\Weixin.exe"),
        ("微信开发者工具", r"D:\WXKFZ\微信开发者工具.exe"),
    ]
    monkeypatch.setattr(appfind, "_START_APPS", fake)

    r = from_start_menu(["计算器"])
    assert r is not None and r.ok
    assert r.kind == "aumid", "UWP 应用必须被识别为 aumid，而不是 exe"
    assert "WindowsCalculator" in r.value

    r2 = from_start_menu(["微信"])
    assert r2 is not None
    assert r2.kind == "exe"
    assert r2.value.lower().endswith("weixin.exe")
    assert "微信开发者工具" not in r2.value, "精确匹配不该被更长的名字抢走"


def test_from_start_menu_returns_none_on_no_match(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(appfind, "_START_APPS", [("计算器", "x!App")])
    assert from_start_menu(["完全不相关的东西zzz"]) is None


def test_from_start_menu_handles_empty_data(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(appfind, "_START_APPS", [])
    assert from_start_menu(["计算器"]) is None


# --------------------------------------------------------------------------- #
# resolve_app 全流程
# --------------------------------------------------------------------------- #


@win_only
@pytest.mark.parametrize(
    "name",
    ["记事本", "计算器", "任务管理器", "资源管理器", "终端", "notepad", "calc", "explorer"],
)
def test_resolve_common_apps(name: str):
    """这些都是 Windows 自带的，任何机器上都必须解析成功。"""
    r = resolve_app(name)
    assert r.ok, f"{name!r} 解析失败：{r.how}"


@win_only
def test_resolved_kind_is_valid():
    valid = {"exe", "shortcut", "aumid", "shell", "fail"}
    for name in ("记事本", "计算器", "微信", "设置"):
        r = resolve_app(name)
        assert r.kind in valid, f"{name!r} 返回了未知 kind={r.kind}"


def test_resolve_reports_what_it_tried():
    r = resolve_app("definitely-not-a-real-app-xyz-123")
    assert not r.ok
    assert "尝试过" in r.how
    assert "definitely-not-a-real-app-xyz-123" in r.how


def test_resolve_absolute_existing_file(tmp_path):
    f = tmp_path / "my.exe"
    f.write_bytes(b"MZ")
    r = resolve_app(str(f))
    assert r.ok
    assert r.kind == "exe"
    assert r.value == str(f)


@win_only
def test_resolve_prefers_explicit_target_over_aliases():
    """配置里给了 target 就该用它，而不是被别名带偏。"""
    r = resolve_app("notepad.exe", ["微信", "计算器"])
    assert r.ok
    assert "notepad" in r.value.lower()


def test_resolve_uses_describe_for_start_menu(monkeypatch: pytest.MonkeyPatch):
    """describe 里有更接近系统显示名的说法时，应当用它匹配。"""
    monkeypatch.setattr(
        appfind, "_START_APPS", [("一些专有名字", r"C:\path\special.exe")]
    )
    called: list[list[str]] = []
    real = appfind.from_start_menu

    def spy(names):  # noqa: ANN001, ANN202
        called.append(list(names))
        return real(names)

    monkeypatch.setattr(appfind, "from_start_menu", spy)
    resolve_app("完全瞎写的", ["也瞎写"], "一些专有名字")
    assert called, "没有调用开始菜单匹配"
    assert any("一些专有名字" in " ".join(map(str, c)) for c in called), (
        f"describe 没被传进去：{called}"
    )


def test_resolved_label_prefers_display(tmp_path):
    r = Resolved("exe", r"C:\a\b\wechat.exe", "测试", "微信")
    assert r.label == "微信"
    r2 = Resolved("exe", r"C:\a\b\wechat.exe", "测试")
    assert r2.label == "wechat.exe"


def test_resolved_ok_property():
    assert Resolved("exe", "x", "y").ok
    assert Resolved("aumid", "x", "y").ok
    assert not Resolved("fail", "", "y").ok
