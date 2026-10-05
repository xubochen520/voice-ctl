"""动态应用词典。

场景全部取自这台机器的真实情况：开始菜单里有 QQ、微信、Steam、Claude…，没有 QQ 音乐。
"""

from __future__ import annotations

import sys

import pytest

from voice_ctl import appfind
from voice_ctl.apps import (
    AppEntry,
    AppIndex,
    decide,
    process_hints,
)

APPS = [
    ("QQ", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Tencent\QQNT\QQ.exe"),
    ("微信", r"E:\weixin\Weixin.exe"),
    ("微信开发者工具", r"D:\WXKFZ\微信开发者工具.exe"),
    ("腾讯会议", r"C:\Program Files\Tencent\WeMeet\wemeetapp.exe"),
    ("飞书", r"C:\Users\x\AppData\Local\Feishu\Feishu.exe"),
    ("Steam", r"C:\Steam\steam.exe"),
    ("Steam Support Center", r"C:\Steam\support.exe"),
    ("Claude", "Anthropic.Claude_pzs8sxrjxfjjc!Claude"),
    ("ChatGPT", "OpenAI.ChatGPT_2p2nqsd0c76g0!App"),
    ("Visual Studio Code", r"C:\VSCode\Code.exe"),
    ("Visual Studio Installer", "Microsoft.VisualStudio.Installer_8wekyb3d8bbwe!App"),
    ("计算器", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    ("记事本", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    ("设置", "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel"),
    ("QQ", r"C:\dup\QQ.exe"),  # 同名重复项
]


@pytest.fixture()
def idx() -> AppIndex:
    return AppIndex(loader=lambda: list(APPS))


def top(idx: AppIndex, q: str):  # noqa: ANN201
    hits = idx.search(q)
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #


def test_duplicate_names_keep_the_first(idx: AppIndex):
    qq = [e for e in idx.entries() if e.name == "QQ"]
    assert len(qq) == 1 and "QQNT" in qq[0].appid


def test_system_aliases_fill_in_what_the_start_menu_lacks(idx: AppIndex):
    names = {e.name for e in idx.entries()}
    assert "画图" in names and "控制面板" in names, "开始菜单没有的系统自带应用靠中文名表兜底"
    assert [e.name for e in idx.entries()].count("计算器") == 1, "开始菜单已经有的不重复加"


def test_system_aliases_can_be_turned_off():
    i = AppIndex(loader=lambda: [("QQ", "x.exe")], system_aliases=False)
    assert [e.name for e in i.entries()] == ["QQ"]


def test_empty_start_menu_still_has_system_apps():
    i = AppIndex(loader=lambda: [])
    assert i.search("记事本")


def test_entries_are_built_once_until_invalidated():
    calls = {"n": 0}

    def loader():  # noqa: ANN202
        calls["n"] += 1
        return [("QQ", "q.exe")]

    i = AppIndex(loader=loader)
    i.search("qq")
    i.search("qq")
    assert calls["n"] == 1
    i.invalidate()
    i.search("qq")
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# 名字匹配
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("q", "want"),
    [("QQ", "QQ"), ("qq", "QQ"), ("微信", "微信"), ("steam", "Steam"), ("Claude", "Claude"),
     ("claude", "Claude"), ("chatgpt", "ChatGPT"), ("计算器", "计算器"), ("设置", "设置"),
     ("记事本", "记事本"), ("飞书", "飞书"), ("画图", "画图")],
)
def test_exact_names(idx: AppIndex, q: str, want: str):
    h = top(idx, q)
    assert h is not None and h.entry.name == want and h.score >= 0.98


def test_exact_beats_longer_names(idx: AppIndex):
    assert top(idx, "微信").entry.name == "微信"  # type: ignore[union-attr]
    assert top(idx, "steam").entry.name == "Steam"  # type: ignore[union-attr]


def test_long_query_does_not_collapse_onto_installed_short_name(idx: AppIndex):
    """没装 QQ 音乐时，说「QQ音乐」不能落到 QQ 上——这是 resolve_app 的旧缺陷。"""
    h = top(idx, "QQ音乐")
    assert h is None or h.entry.name != "QQ" or decide([h]) == "none"


def test_vs_code_is_not_confused_with_installer(idx: AppIndex):
    h = top(idx, "visual studio code")
    assert h is not None and h.entry.name == "Visual Studio Code"
    assert decide(idx.search("visual studio code")) == "auto"


def test_short_name_is_a_suggestion_not_a_certainty(idx: AppIndex):
    """「code」是 VS Code 的简称，但也可能指别的：命中，但要问一句。"""
    hits = idx.search("code")
    assert hits and hits[0].entry.name == "Visual Studio Code"
    assert decide(hits) == "ask"


def test_unknown_app_finds_nothing_useful(idx: AppIndex):
    assert decide(idx.search("完全不存在的软件xyz")) == "none"
    assert idx.search("") == [] and idx.search("   ") == []


def test_limit_and_ordering(idx: AppIndex):
    hits = idx.search("visual studio", limit=5)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert len(idx.search("visual studio", limit=1)) == 1


# --------------------------------------------------------------------------- #
# 昵称
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spoken", ["扣扣", "企鹅", "腾讯QQ", "腾讯扣扣", "腾讯 QQ"])
def test_qq_nicknames(idx: AppIndex, spoken: str):
    h = top(idx, spoken)
    assert h is not None and h.entry.name == "QQ" and h.how == "nickname" and h.score == 1.0
    assert decide(idx.search(spoken)) == "auto"


def test_nickname_only_works_if_the_app_is_installed():
    i = AppIndex(loader=lambda: [("微信", "w.exe")])
    assert decide(i.search("扣扣")) == "none", "没装 QQ，说「扣扣」不该打开别的东西"


def test_nickname_with_alternative_display_names():
    i = AppIndex(loader=lambda: [("WeChat", "w.exe")])
    h = i.search("微信")  # 开始菜单里叫 WeChat，用户说的是微信
    assert not h or h[0].entry.name != "x"
    h2 = i.search("威信")
    assert h2 and h2[0].entry.name == "WeChat" and h2[0].how == "nickname"


def test_user_nickname_overrides_and_extends():
    i = AppIndex({"小鹅": "QQ", "扣扣": "Claude"}, loader=lambda: list(APPS))
    assert i.search("小鹅")[0].entry.name == "QQ"
    assert i.search("扣扣")[0].entry.name == "Claude", "用户配置要能覆盖内置昵称"


def test_nickname_matching_ignores_case_space_and_punct():
    i = AppIndex({"my  app!": "QQ"}, loader=lambda: list(APPS))
    assert i.search("MY APP")[0].entry.name == "QQ"


# --------------------------------------------------------------------------- #
# 拼音：ASR 把「微信」听成「威信」是常态
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("heard", ["威信", "薇信", "为信"])
def test_homophones_resolve_via_pinyin(idx: AppIndex, heard: str):
    pytest.importorskip("pypinyin")
    h = top(idx, heard)
    assert h is not None and h.entry.name == "微信", f"{heard!r} → {h and h.entry.name}"
    assert h.score >= 0.9


def test_pinyin_can_be_disabled():
    pytest.importorskip("pypinyin")
    i = AppIndex(loader=lambda: list(APPS), use_pinyin=False)
    assert all(e.pinyin == "" for e in i.entries())
    h = i.search("为信")  # 不在昵称表里的同音写法
    assert not h or h[0].entry.name != "微信"


def test_pinyin_needs_exact_syllables_not_just_close_for_short_names(idx: AppIndex):
    pytest.importorskip("pypinyin")
    h = top(idx, "危险")  # wei xian ≠ wei xin
    assert h is None or h.entry.name != "微信"


def test_pinyin_is_skipped_for_latin_queries(idx: AppIndex):
    assert top(idx, "zzzz") is None or decide(idx.search("zzzz")) == "none"


# --------------------------------------------------------------------------- #
# decide
# --------------------------------------------------------------------------- #


def hit(score: float, name: str = "X"):  # noqa: ANN202
    from voice_ctl.apps import AppHit

    return AppHit(AppEntry(name), score, "name")


def test_decide_thresholds():
    assert decide([]) == "none"
    assert decide([hit(0.59)]) == "none"
    assert decide([hit(0.6)]) == "ask"
    assert decide([hit(0.89)]) == "ask"
    assert decide([hit(0.9)]) == "auto"
    assert decide([hit(1.0)]) == "auto"


def test_decide_asks_when_runner_up_is_too_close():
    """两个应用得分几乎一样：不能替用户挑一个。"""
    assert decide([hit(1.0, "A"), hit(0.98, "B")]) == "ask"
    assert decide([hit(1.0, "A"), hit(0.86, "B")]) == "auto"


def test_two_installed_apps_with_the_same_score_are_ambiguous():
    i = AppIndex(loader=lambda: [("Foo Pro", "a!x"), ("Foo Max", "b!x")], system_aliases=False)
    hits = i.search("foo")
    assert len(hits) == 2 and decide(hits) == "ask"


# --------------------------------------------------------------------------- #
# 启动与进程提示
# --------------------------------------------------------------------------- #


def test_resolved_kinds():
    assert AppEntry("微信", r"E:\weixin\Weixin.exe").resolved().kind == "exe"
    uwp = AppEntry("计算器", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App").resolved()
    assert uwp.kind == "aumid" and uwp.label == "计算器"
    guid = AppEntry("QQ", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Tencent\QQNT\QQ.exe").resolved()
    assert guid.kind == "exe", "已知文件夹 GUID 前缀的 exe 路径也按 exe 处理"
    assert AppEntry("不存在", "").resolved().ok is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专有")
def test_system_alias_entry_resolves_to_real_command():
    r = AppEntry("记事本", "", "notepad").resolved()
    assert r.ok and "notepad" in r.value.lower()


@pytest.mark.parametrize(
    ("entry", "exes", "pfn"),
    [
        (AppEntry("QQ", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Tencent\QQNT\QQ.exe"), {"qq.exe"}, ""),
        (AppEntry("微信", r"E:\weixin\Weixin.exe"), {"weixin.exe"}, ""),
        (AppEntry("计算器", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"), set(),
         "Microsoft.WindowsCalculator_8wekyb3d8bbwe"),
        (AppEntry("记事本", "", "notepad"), {"notepad.exe"}, ""),
        (AppEntry("记事本", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "notepad"), {"notepad.exe"},
         "Microsoft.WindowsNotepad_8wekyb3d8bbwe"),
        (AppEntry("x", ""), set(), ""),
    ],
)
def test_process_hints(entry: AppEntry, exes: set[str], pfn: str):
    got_exes, got_pfn = process_hints(entry)
    assert set(got_exes) == exes and got_pfn == pfn


def test_real_start_menu_if_available():
    """在开发机上拿真实的开始菜单跑一遍，确认构建/搜索不会炸（拿不到就跳过）。"""
    if sys.platform != "win32":
        pytest.skip("Windows 专有")
    apps = appfind.start_apps()
    if not apps:
        pytest.skip("开始菜单为空")
    i = AppIndex()
    assert i.entries()
    name, _ = apps[0]
    assert i.search(name)[0].score >= 0.98
