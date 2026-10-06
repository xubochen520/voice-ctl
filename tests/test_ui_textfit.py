"""textfit.py：中文换行的避头尾。

Tk 的 wraplength 只认空格，中文整段没有空格，会在放不下的那个字处硬断，常常单独掉下一个「。」。
这些是纯函数，量字宽的函数从外面传进来，所以可以用「每个汉字 2 格、每个 ASCII 字符 1 格」的假字体
精确地证明规则，不依赖 Tk。
"""

from __future__ import annotations

import pytest

from voice_ctl.ui.textfit import NO_END, NO_START, tokens, wrap_text


def meas(s: str) -> int:
    """假字体：ASCII 1 格，其它 2 格（全角）。"""
    return sum(1 if ch.isascii() else 2 for ch in s)


EM = 2


def wrap(text: str, cols: int) -> list[str]:
    return wrap_text(text, meas, cols, EM).split("\n")


def test_short_text_is_untouched():
    assert wrap("你好世界", 20) == ["你好世界"]


def test_breaks_between_characters_when_too_long():
    lines = wrap("一二三四五六七八", 8)  # 8 格 = 4 个汉字
    assert lines == ["一二三四", "五六七八"]


def test_punctuation_never_starts_a_line():
    """这是整个模块存在的理由：行末放不下的「。」要么悬挂在上一行，要么把前一个字一起带下去。"""
    for cols in range(6, 30):
        for line in wrap("匹配方式是别名匹配的依据，写得越像你平时的说法，越不需要靠语义层。", cols)[1:]:
            assert line[0] not in NO_START, f"cols={cols}: 行首出现了标点 {line[0]!r}：{line!r}"


def test_opening_brackets_never_end_a_line():
    for cols in range(6, 30):
        for line in wrap("这是（一段话）里面「有引号」和《书名》都要处理好。", cols)[:-1]:
            assert line[-1] not in NO_END, f"cols={cols}: 行尾出现了开括号 {line[-1]!r}：{line!r}"


def test_hanging_punctuation_may_overshoot_by_one_em():
    # 4 个汉字刚好一行（8 格），后面紧跟「。」：悬挂在行末，最多多出 1em（2 格）
    lines = wrap("一二三四。五六", 8)
    assert lines[0] == "一二三四。"
    assert lines[1] == "五六"


def test_ascii_words_are_not_split():
    """西文标识符不能被拆到两行。

    这里**只用**「不能出现半个词」+「整个词得在某一行的完整位置」两条判据。

    原先还有一条 `assert all("open_target" in ln or "open_target" not in
    "".join(lines) for ln in lines)`，它是恒假的：`"".join(lines)` 拼回原文，
    必然包含 open_target，于是它退化成「每一行都必须含 open_target」——
    正确的折行（打开 / open_target / 应用）反而通不过。断言写错的代价不只是
    误报，是它把注意力从真正的判据上引开。
    """
    lines = wrap("打开 open_target 应用", 12)

    # 不能出现被切断的半个词
    for ln in lines:
        assert "open_" not in ln or "open_target" in ln, f"单词被拆开了：{lines}"
    # 整个词必须完整地出现在某一行里（而不是被拆成两行拼起来）
    assert any("open_target" in ln for ln in lines), f"单词没保住：{lines}"
    # 逐字符断开是长单词的兜底，这里不该触发
    assert "".join(lines).count("open_target") == 1


def test_a_word_longer_than_a_line_is_broken_by_character():
    path = "D:\\very\\long\\path\\that\\cannot\\fit\\on\\one\\line"
    lines = wrap(path, 10)
    assert "".join(lines) == path, "逐字符断开后拼回来必须和原文一致"
    assert all(meas(ln) <= 10 + EM for ln in lines)


def test_explicit_newlines_are_preserved():
    assert wrap("第一行\n第二行", 40) == ["第一行", "第二行"]
    assert wrap("a\n\nb", 40) == ["a", "", "b"], "空行也要保留"


def test_leading_and_trailing_spaces_are_dropped_at_breaks():
    lines = wrap("aaaa bbbb cccc", 9)
    for ln in lines:
        assert ln == ln.strip(), f"折行处的空格要丢掉：{lines}"


def test_content_is_preserved_modulo_whitespace():
    text = "识别结果开头要丢掉的客气话，逗号分隔。open_app / open_target（打开你说的应用）。"
    for cols in (8, 11, 17, 23, 40):
        out = wrap_text(text, meas, cols, EM)
        assert out.replace("\n", "").replace(" ", "") == text.replace(" ", ""), f"cols={cols} 丢了/多了字"


def test_empty_and_whitespace_inputs():
    assert wrap_text("", meas, 10, EM) == ""
    assert wrap_text("   ", meas, 10, EM) == ""


def test_tokens_group_ascii_runs_but_not_cjk():
    assert list(tokens("打开QQ音乐")) == ["打", "开", "QQ", "音", "乐"]
    assert list(tokens("a b")) == ["a", " ", "b"]
    assert list(tokens("%USERPROFILE%\\x")) == ["%USERPROFILE%\\x"]


@pytest.mark.parametrize("cols", [3, 4, 5])
def test_tiny_widths_do_not_loop_forever_or_lose_text(cols: int):
    text = "超窄容器也必须能走完，不能死循环。"
    out = wrap_text(text, meas, cols, EM)
    assert out.replace("\n", "") == text
