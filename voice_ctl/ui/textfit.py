"""中文换行：避头尾。

Tk 的 Label 折行只认空格——中文整段没有空格，它就在「放不下的那个字」处硬断，结果常常是
一行末尾只掉下来一个「。」或「，」。这在排版上是错的：标点不能出现在行首（避头），
开括号不能出现在行尾（避尾）。这里自己算断行，再把结果以 "\\n" 交给 Label。

纯函数、不依赖 Tk（量字宽的函数从外面传进来），所以能直接单测。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

# 不能出现在行首的：句读、闭括号、引号尾
NO_START = frozenset("，。、；：？！）】」』》〉”’…—·,.;:?!)]}%")
# 不能出现在行尾的：开括号、引号头
NO_END = frozenset("（【「『《〈“‘([{")


def _is_word_char(ch: str) -> bool:
    """属于同一个"西文单词"的字符：ASCII 里除了空白的全部。路径、标识符（open_app、%USERPROFILE%）
    都得当一个整体，不能在中间断开。NO_START 里的 ASCII 标点（. , ) …）也算——它们紧跟着单词。"""
    return ch.isascii() and not ch.isspace()


def tokens(para: str) -> Iterator[str]:
    """拆成可断行的最小单位：一个汉字（或其它非 ASCII 字符）、一个连续的 ASCII 词、一个空白。"""
    i, n = 0, len(para)
    while i < n:
        ch = para[i]
        if _is_word_char(ch):
            j = i + 1
            while j < n and _is_word_char(para[j]):
                j += 1
            yield para[i:j]
            i = j
        else:
            yield ch
            i += 1


def wrap_text(text: str, measure: Callable[[str], int], max_px: int, em: int) -> str:
    """把 text 按 max_px 折行，返回带 "\\n" 的字符串。

    `measure(s)` 返回 s 的像素宽度；`em` 是一个全角字的宽度。
    规则：
      * 行首不放 NO_START 里的字符——放不下时让它**悬挂**在上一行末尾（最多多出 1em）。
        调用方要把 max_px 设成「容器宽度 - em」，悬挂出来的那一格才不会被裁掉。
      * 行尾不放 NO_END 里的字符——有就带到下一行。
      * 西文词不在中间断开，除非它自己就比一整行还长（长路径）——那时才逐字符断。
      * 原文里的换行保留；行首的空格丢掉，行尾的空格丢掉。
    """
    out: list[str] = []
    for para in text.split("\n"):
        line: list[str] = []
        width = 0

        def flush(carry: list[str] | None = None) -> None:
            nonlocal line, width
            while line and line[-1].isspace():
                line.pop()
            out.append("".join(line))
            line = list(carry) if carry else []
            width = sum(measure(t) for t in line)

        for tok in tokens(para):
            if tok.isspace():
                if line:
                    line.append(tok)
                    width += measure(tok)
                continue
            tw = measure(tok)
            if width + tw <= max_px or not line:
                if tw > max_px and len(tok) > 1:
                    # 比一整行还长的西文词：只能逐字符断
                    for ch in tok:
                        cw = measure(ch)
                        if width + cw > max_px and line:
                            flush()
                        line.append(ch)
                        width += cw
                    continue
                line.append(tok)
                width += tw
                continue
            if tok[0] in NO_START and width + tw <= max_px + em:
                line.append(tok)  # 悬挂标点
                width += tw
                continue
            carry: list[str] = []
            while line and not line[-1].isspace() and line[-1][-1] in NO_END:
                carry.insert(0, line.pop())
            if not line:  # 整行都是开括号之类，没得带：原样放着
                line, carry = carry, []
            flush(carry)
            line.append(tok)
            width += tw
        flush()
    return "\n".join(out)
