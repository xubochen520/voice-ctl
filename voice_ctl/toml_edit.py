"""保留注释的 TOML 定点改写。

为什么不能直接 `dump(config)` 整个文件：

    仓库自带的 config.toml 里，注释比代码多——每一条都写着「为什么是这个值」
    （min_peak 为什么用峰值不用 RMS、为什么中文必须用 multilingual……）。
    用户第一次在 UI 里点保存就把这些全冲掉，等于把文档删了。
    Python 3.12 的标准库只有 tomllib（只读），也没有 tomli_w。

所以做**定点改写**：解析出每个「键 = 值」在文件里的行范围，只替换那几行，
其余字节原样保留。同一行尾部的注释也一并保住。

三个刻意的设计：

  * **幂等**：写入前先比较当前值，一样就一个字都不动。UI 上反复点保存
    不应该让文件产生 diff。
  * **每次改动后重新解析**。文件才两百行，重解析比维护行号偏移便宜得多，
    而且不会因为一次插入把后续所有行号算错——那类 bug 极难查。
  * **值仍用格式化的多行数组**写回。单行 300 字符的 aliases 数组在编辑器里
    没法读也没法 diff。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["TomlDoc", "TomlEditError", "literal", "format_action_block"]


class TomlEditError(Exception):
    """改写失败。消息里必须说清是哪个键、为什么改不了。"""


_HEADER_RE = re.compile(r"^\s*\[\[?\s*(.+?)\s*\]\]?\s*(#.*)?$")
_KEY_RE = re.compile(r"""^\s*([A-Za-z0-9_\-]+|"[^"]*"|'[^']*')\s*=\s*(.*)$""")

# 内联数组/表写超过这个宽度就拆成多行
INLINE_WIDTH = 88


# --------------------------------------------------------------------------- #
# 词法小工具
# --------------------------------------------------------------------------- #


def _strip_comment(text: str) -> tuple[str, str]:
    """把一行拆成 (代码, 注释)。**字符串里的 # 不算注释**。

    这是必须自己扫描而不能用 split('#') 的原因：
    `target = "http://a#b"` 里那个 # 是 URL 的一部分。
    """
    out: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            return "".join(out), text[i:]
        out.append(ch)
        i += 1
    return "".join(out), ""


def _balance(code: str) -> int:
    """数括号深度（忽略字符串）。用来判断值跨了几行。"""
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(code):
        ch = code[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


# --------------------------------------------------------------------------- #
# 字面量格式化
# --------------------------------------------------------------------------- #


def _quote(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def literal(value: Any) -> str:
    """把 Python 值写成 TOML 字面量（单行形式）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            raise TomlEditError(f"TOML 没有 NaN/inf 字面量，无法写入 {value!r}")
        return repr(round(value, 6)) if value != int(value) else f"{value:.1f}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = ", ".join(f"{_quote(str(k))} = {literal(v)}" for k, v in value.items())
        return "{ " + body + " }"
    raise TomlEditError(f"不支持写回的类型 {type(value).__name__}：{value!r}")


def format_array(values: list[Any], *, indent: str = "    ") -> str:
    """数组：短就单行，长就拆成多行。

    拆行不是为了好看，是为了 diff——一行 40 个别名的数组，改一个字
    在 git 里就是整行重写。
    """
    if not values:
        return "[]"
    inline = literal(values)
    if "\n" not in inline and len(inline) <= INLINE_WIDTH:
        return inline
    items = ",\n".join(f"{indent}{literal(v)}" for v in values)
    return "[\n" + items + ",\n]"


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    key: str
    start: int
    """键所在的第一行（0 基）。"""
    end: int
    """值的最后一行（含）。多行数组时 > start。"""
    comment: str = ""
    """行尾注释，原样保留。"""
    value_text: str = ""
    """值的原始 TOML 文本（可能跨行）。解析一次存下来，省得反复切字符串
    ——按 '=' 切会在键名里含 '=' 时切错位置。"""


@dataclass
class Table:
    name: str
    is_array: bool
    index: int
    """同名表里的第几个（[[action]] 用；普通表恒为 0）。"""
    header_line: int
    body_start: int
    body_end: int
    """body 区间是 [body_start, body_end)，不含下一个表头。"""
    header_text: str = ""
    nodes: dict[str, Node] = field(default_factory=dict)


class TomlDoc:
    """一份可定点改写的 TOML 文本。"""

    def __init__(self, text: str, *, source: Path | None = None) -> None:
        self.source = source
        self.newline = "\r\n" if "\r\n" in text else "\n"
        raw = text.replace("\r\n", "\n").replace("\r", "\n")
        self._trailing_nl = raw.endswith("\n")
        self.lines: list[str] = raw.split("\n")
        if self._trailing_nl:
            self.lines.pop()
        self._parse()

    # -- 解析 ------------------------------------------------------------- #

    def _parse(self) -> None:
        self.tables: list[Table] = []
        current: Table | None = None
        counters: dict[str, int] = {}
        i = 0
        n = len(self.lines)
        while i < n:
            line = self.lines[i]
            m = _HEADER_RE.match(line) if not line.lstrip().startswith("#") else None
            if m:
                header = m.group(1)
                is_array = line.strip().startswith("[[")
                if current is not None:
                    current.body_end = i
                idx = counters.get(header, 0)
                counters[header] = idx + 1
                current = Table(
                    name=header,
                    is_array=is_array,
                    index=idx,
                    header_line=i,
                    body_start=i + 1,
                    body_end=n,
                    header_text=line,
                )
                self.tables.append(current)
                i += 1
                continue

            if not line.lstrip().startswith("#"):
                km = _KEY_RE.match(line)
                if km and current is not None:
                    key = km.group(1).strip("\"'")
                    code, comment = _strip_comment(km.group(2))
                    end = i
                    depth = _balance(code)
                    while depth > 0 and end + 1 < n:
                        end += 1
                        more, _ = _strip_comment(self.lines[end])
                        depth += _balance(more)
                    value_parts = [code.strip()]
                    value_parts += self.lines[i + 1 : end + 1]
                    current.nodes[key] = Node(
                        key, i, end, comment.strip(), "\n".join(value_parts).strip()
                    )
                    i = end + 1
                    continue
            i += 1

    # -- 查询 ------------------------------------------------------------- #

    def find(self, name: str, index: int = 0) -> Table | None:
        for t in self.tables:
            if t.name == name and t.index == index:
                return t
        return None

    def find_all(self, name: str) -> list[Table]:
        return [t for t in self.tables if t.name == name]

    def raw(self, section: str, key: str, index: int = 0) -> str | None:
        t = self.find(section, index)
        if t is None or key not in t.nodes:
            return None
        return t.nodes[key].value_text

    def value(self, section: str, key: str, index: int = 0, default: Any = None) -> Any:
        """取**解析后**的值，用来判断「要不要写」。"""
        raw = self.raw(section, key, index)
        if raw is None:
            return default
        try:
            return tomllib.loads(f"v = {raw}")["v"]
        except tomllib.TOMLDecodeError:
            return default

    def has_section(self, name: str) -> bool:
        return any(t.name == name for t in self.tables)

    def has_key(self, section: str, key: str, index: int = 0) -> bool:
        t = self.find(section, index)
        return t is not None and key in t.nodes

    # -- 改写 ------------------------------------------------------------- #

    def _render(self) -> str:
        out = self.newline.join(self.lines)
        if self._trailing_nl or not out.endswith("\n"):
            out += self.newline
        return out

    def text(self) -> str:
        return self._render()

    def reparse(self) -> None:
        """外部直接改过 self.lines 之后，重建行号表。"""
        self._parse()

    def set(self, section: str, key: str, value: Any, *, index: int = 0,
            force: bool = False, array: bool | None = None) -> bool:
        """写入一个键。返回是否真的改了。

        值没变就**一个字节都不动**（除非 force）：UI 上反复保存不应该
        把文件改出 diff，更不该把用户手写的等价格式（`0.01` vs `.01`）
        规范化掉。
        """
        if not force and self.value(section, key, index, _MISSING) == value:
            return False
        body = literal(value) if not isinstance(value, list) else format_array(list(value))
        t = self.find(section, index)
        if t is None:
            t = self.append_table(section, array=self._wants_array(section, index, array))
        assert t is not None

        node = t.nodes.get(key)
        if node is not None:
            m = re.match(r"^(\s*)", self.lines[node.start])
            indent = m.group(1) if m else ""
        else:
            indent = self._indent_of(t)
        suffix = f"  {node.comment}" if node is not None and node.comment else ""
        new = [f"{indent}{key} = {body}{suffix}"]

        if node is None:
            # 插到表体末尾，但**跳过尾部空行**——否则新键会贴在下一个
            # [section] 头上，看起来像属于下一段
            at = t.body_end
            while at - 1 >= t.body_start and not self.lines[at - 1].strip():
                at -= 1
            self.lines[at:at] = new
        else:
            self.lines[node.start : node.end + 1] = new
        self._parse()
        return True

    def _indent_of(self, table: Table) -> str:
        """新键该用几个空格缩进。

        跟着**同一个表里已有的键**走，而不是硬编码 0 或 4——否则在一个
        不缩进的 [audio] 里插一行 4 空格缩进的 device = 3，看起来就像
        它属于别的段，用户改完会怀疑自己是不是改坏了。
        """
        counts: dict[str, int] = {}
        for node in table.nodes.values():
            m = re.match(r"^(\s*)", self.lines[node.start])
            pad = m.group(1) if m else ""
            if pad.strip():
                pad = ""  # 混合了 tab 之类，别学
            counts[pad] = counts.get(pad, 0) + 1
        if not counts:
            return ""
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def remove(self, section: str, key: str, *, index: int = 0) -> bool:
        t = self.find(section, index)
        if t is None or key not in t.nodes:
            return False
        node = t.nodes[key]
        del self.lines[node.start : node.end + 1]
        self._parse()
        return True

    def append_table(self, name: str, *, array: bool = True, header_comment: str = "") -> Table:
        """在文件末尾追加一个表（默认 [[name]]）。"""
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        if header_comment:
            self.lines.append(header_comment)
        self.lines.append(f"[[{name}]]" if array else f"[{name}]")
        self._parse()
        t = self.find(name, len(self.find_all(name)) - 1)
        assert t is not None
        return t

    def _wants_array(self, section: str, index: int, hint: bool | None) -> bool:
        """新建的表该用 [x] 还是 [[x]]。

        猜错的后果不是格式难看，而是**文件直接读不回来**：`[action]` 和
        `[[action]]` 同时出现，TOML 会判 "cannot redefine table"。
        所以除了显式指定，还要看一眼这个段名在文件里已经是什么形态。
        """
        if hint is not None:
            return hint
        if any(t.name == section and t.is_array for t in self.tables):
            return True
        return index > 0

    def remove_table(self, name: str, index: int) -> bool:
        """删掉一个表及其全部内容。

        连带删掉紧贴在表头上方的注释和空行——那是**这个块自己的**说明
        （比如 `# 微信：装在哪里由 appfind 自己找`）。留着的话它会粘到
        下一个块头上，读起来变成张冠李戴。

        只吃紧邻的注释（中间不能隔空行）：隔了空行的那段通常是整个
        `[[action]]` 段的通用说明，删掉就丢了文档。
        """
        t = self.find(name, index)
        if t is None:
            return False
        start = t.header_line
        while start - 1 >= 0 and self.lines[start - 1].lstrip().startswith("#"):
            start -= 1
        while start - 1 >= 0 and not self.lines[start - 1].strip():
            start -= 1
        del self.lines[start : t.body_end]
        self._parse()
        return True

    def table_comment_above(self, name: str, index: int) -> list[str]:
        """取表头上方紧邻的注释行，删除动作块时可以一起带走。"""
        t = self.find(name, index)
        if t is None:
            return []
        out: list[str] = []
        i = t.header_line - 1
        while i >= 0 and self.lines[i].lstrip().startswith("#"):
            out.insert(0, self.lines[i])
            i -= 1
        return out


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"

    def __eq__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return 0


_MISSING = _Missing()


# --------------------------------------------------------------------------- #
# 动作块
# --------------------------------------------------------------------------- #

_ACTION_ORDER = ("id", "handler", "aliases", "describe", "target", "args", "enabled")


def format_action_block(action: dict[str, Any], *, comment: str = "") -> list[str]:
    """把一个动作写成 [[action]] 块的行。

    只写非默认字段（enabled 为 true、args/aliases/describe/target 为空时不写），
    让文件短、读起来是「这个动作有什么特别」而不是一堆默认值。
    id 和 handler 例外——它们是配置合法性的下限，永远写。
    """
    lines: list[str] = []
    if comment:
        lines.append(comment)
    lines.append("[[action]]")
    for key in _ACTION_ORDER:
        if key not in action:
            continue
        val = action[key]
        if key not in ("id", "handler") and val in ("", [], {}, True, None):
            continue
        if key in ("aliases", "args"):
            lines.append(f"{key} = {format_array(list(val))}")
        else:
            lines.append(f"{key} = {literal(val)}")
    return lines
