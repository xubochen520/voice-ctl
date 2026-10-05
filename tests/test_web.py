"""网页解析：「打开百度」→ baidu.com。

守的是几条**只有真跑起来才知道**的边界：

  * 「打开百度」和「打开百度网盘」必须是两个结果——前者开站，后者开本机程序。
    本机装了百度网盘，索引对「百度」的模糊匹配是 0.72，光看分数会开错。
  * 「淘宝网」的「网」是弱尾巴（= 淘宝），而「百度网盘」的「网盘」不是
    （那是另一个站）。一刀切地剥尾巴会把网盘开成百度首页。
  * 「打开计算器」绝不能被搜素兜底吞掉。搜索兜底总能给出结果，所以它必须
    排在本机应用**后面**。
"""

from __future__ import annotations

import pytest

from voice_ctl.intent import APP, WEB, interpret
from voice_ctl.normalize import Normalizer
from voice_ctl.web import WebIndex, looks_like_domain, normalize_url, search_url

N = Normalizer()


@pytest.fixture()
def idx() -> WebIndex:
    return WebIndex()


# --------------------------------------------------------------------------- #
# 站点表
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("query", "want_url"),
    [
        ("百度", "https://www.baidu.com"),
        ("baidu", "https://www.baidu.com"),
        ("度娘", "https://www.baidu.com"),
        ("淘宝", "https://www.taobao.com"),
        ("B站", "https://www.bilibili.com"),
        ("bilibili", "https://www.bilibili.com"),
        ("哔哩哔哩", "https://www.bilibili.com"),
        ("12306", "https://www.12306.cn"),
        ("火车票", "https://www.12306.cn"),
        ("github", "https://github.com"),
        ("码云", "https://gitee.com"),
        ("github", "https://github.com"),
    ],
)
def test_known_sites(idx: WebIndex, query: str, want_url: str):
    hit = idx.find(query)
    assert hit is not None, f"{query} 没查到"
    assert hit[0].url == want_url


def test_names_are_case_insensitive_and_punctuation_free(idx: WebIndex):
    """ASR 给的大小写和标点不可控，比较前统一收拾掉。"""
    for q in ("GitHub", "github", "git hub", " GitHub "):
        hit = idx.find(q)
        assert hit is not None and hit[0].url == "https://github.com", q


def test_unknown_name_is_none_not_a_guess(idx: WebIndex):
    """查不到就返回 None，让上层去走搜索——硬猜一个站会开错东西。"""
    for q in ("计算器", "我渴了", "腾讯", "阿斯顿发斯蒂芬"):
        assert idx.find(q) is None, q


# --------------------------------------------------------------------------- #
# 弱尾巴：淘宝网 = 淘宝，但百度网盘 ≠ 百度
# --------------------------------------------------------------------------- #


def test_trailing_net_is_a_weak_suffix(idx: WebIndex):
    hit = idx.find("淘宝网")
    assert hit is not None and hit[0].url == "https://www.taobao.com"
    assert hit[2] == "suffix"


def test_netpan_is_not_a_weak_suffix(idx: WebIndex):
    """「百度网盘」是另一个站（pan.baidu.com），不是「百度」+尾巴。

    实测过的坑：把「网盘」当尾巴剥掉，「打开百度网盘」就会开成百度首页，
    而用户要的是网盘。
    """
    hit = idx.find("百度网盘")
    assert hit is not None
    assert hit[0].url == "https://pan.baidu.com"
    assert hit[2] == "exact"


def test_prefix_only_match_is_marked_contained(idx: WebIndex):
    """名字里带着站点名（多出来的那截可能是别的东西）→ contained，不算确切。

    这里要拿**归一化器不动**的名字试：「百度一下」会被归一化器收成「百度」，
    那就成了 exact，测不到这条路。
    """
    hit = idx.find("百度糯米")
    assert hit is not None and hit[2] == "contained"


# --------------------------------------------------------------------------- #
# 域名
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", ["example.com", "https://a.b.cn/x", "www.gov.cn"])
def test_domains_recognized(text: str):
    assert looks_like_domain(text)


@pytest.mark.parametrize("text", ["12306", "百度", "打开百度", "a", "v1.2"])
def test_non_domains(text: str):
    """「12306」是站名不是域名——判成域名会去开一个不存在的站。"""
    assert not looks_like_domain(text)


def test_normalize_url_adds_scheme():
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("https://x.cn") == "https://x.cn"


def test_search_url_encodes_the_query():
    u = search_url("百度 网盘")
    assert u.startswith("https://www.baidu.com/s?wd=")
    assert " " not in u and "%20" in u


# --------------------------------------------------------------------------- #
# 意图层接线
# --------------------------------------------------------------------------- #


def _intent(text: str, **kw):  # noqa: ANN201
    return interpret(text, normalizer=N, **kw)


@pytest.mark.parametrize(
    ("text", "want_url"),
    [
        ("打开百度", "https://www.baidu.com"),
        ("打开百度网页", "https://www.baidu.com"),
        ("打开百度官网", "https://www.baidu.com"),
        ("打开淘宝", "https://www.taobao.com"),
        ("打开淘宝网", "https://www.taobao.com"),
        ("打开哔哩哔哩", "https://www.bilibili.com"),
        ("打开B站", "https://www.bilibili.com"),
    ],
)
def test_open_web_intent(text: str, want_url: str):
    it = _intent(text)
    assert it is not None and it.kind == WEB, f"{text} -> {it}"
    assert it.url == want_url


def test_category_word_is_stripped_before_lookup():
    """「百度网页」要把「网页」剥掉再查，不然会拿「百度网页」当名字查不到。"""
    it = _intent("打开百度网页")
    assert it is not None and it.kind == WEB
    assert it.payload == "百度网页"  # 原文保留，便于日志解释


def test_domain_goes_to_the_web():
    it = _intent("打开 example.com")
    assert it is not None and it.kind == WEB
    assert it.url == "https://example.com"


def test_search_fallback_only_when_asked_for_a_webpage():
    """没收录的名字 + 明说要网页 → 搜索页。"""
    it = _intent("打开一个没听过的站网页")
    assert it is not None and it.kind == WEB
    assert it.via_search and "wd=" in it.url


def test_search_fallback_can_be_disabled():
    it = _intent("打开一个没听过的站网页", web_search=False)
    assert it is not None
    assert it.kind != WEB, "关掉搜索兜底后不该再凭空造一个搜索页"


def test_web_can_be_disabled_entirely():
    it = _intent("打开百度", web_enabled=False)
    assert it is None or it.kind != WEB


def test_local_app_still_wins_for_names_that_are_apps():
    """站点表里没有的名字，照旧交给本机索引——「打开计算器」不能变成搜索。"""
    it = _intent("打开计算器")
    assert it is None or it.kind != WEB, f"计算器被当成网页了：{it}"
