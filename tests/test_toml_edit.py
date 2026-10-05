"""保留注释的 TOML 定点改写。

这一层的价值全在"没动的地方一个字节都没变"，所以测试的重点也是这个：
改一个键之后，注释、空行、手写格式、以及其它所有键都得原样还在。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from voice_ctl.toml_edit import TomlDoc, TomlEditError, format_action_block, format_array, literal

SAMPLE = """\
# 顶部说明，必须保住
# 第二行说明

[hotkey]
# 热键的注释
keys = "<ctrl>+<alt>+space"
min_duration_ms = 200      # 行尾注释

[audio]
samplerate = 16000
min_peak = 0.01

[match]
threshold = 80             # 别名模糊匹配阈值
strip_prefixes = [
    "请",
    "帮我",
]
substitutions = { "围信" = "微信" }

[[action]]
id = "a.one"
handler = "open_app"
aliases = ["一", "壹"]
target = "notepad.exe"

[[action]]
id = "a.two"
handler = "sysctl"
aliases = ["二"]
target = "lock"
enabled = false
"""


def doc(text: str = SAMPLE) -> TomlDoc:
    return TomlDoc(text)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_untouched_document_renders_byte_identical():
    """最重要的一条：不打算改就一个字节都别动。"""
    assert doc().text() == SAMPLE


def test_tables_and_array_tables_are_found():
    d = doc()
    assert d.find("hotkey") is not None
    assert d.find("match") is not None
    assert len(d.find_all("action")) == 2
    assert d.find("action", 1) is not None
    assert d.find("action", 2) is None


def test_values_are_parsed_not_string_matched():
    d = doc()
    assert d.value("hotkey", "min_duration_ms") == 200
    assert d.value("match", "threshold") == 80
    assert d.value("action", "enabled", 1) is False
    assert d.value("action", "enabled", 0) is None  # 没写就是没写


def test_missing_key_returns_default():
    assert doc().value("hotkey", "nope", default="d") == "d"


def test_hash_inside_string_is_not_a_comment():
    d = TomlDoc('[a]\nurl = "https://x/y#z"\n')
    assert d.value("a", "url") == "https://x/y#z"
    assert d.raw("a", "url") == '"https://x/y#z"'


def test_multiline_array_is_one_node():
    d = doc()
    node = d.find("match").nodes["strip_prefixes"]  # type: ignore[union-attr]
    assert node.end > node.start
    assert d.value("match", "strip_prefixes") == ["请", "帮我"]


# --------------------------------------------------------------------------- #
# 改写
# --------------------------------------------------------------------------- #


def test_set_replaces_value_and_keeps_trailing_comment():
    d = doc()
    assert d.set("match", "threshold", 90) is True
    out = d.text()
    assert "threshold = 90  # 别名模糊匹配阈值" in out
    assert "# 顶部说明，必须保住" in out
    assert tomllib.loads(out)["match"]["threshold"] == 90


def test_set_is_idempotent():
    """界面上反复点保存不该产生 diff。"""
    d = doc()
    assert d.set("match", "threshold", 90) is True
    assert d.set("match", "threshold", 90) is False
    once = d.text()
    d.set("match", "threshold", 90)
    assert d.text() == once


def test_set_force_overrides_idempotence():
    """交换动作块这类操作，两边值可能恰好相等，但必须照样落到正确的块上。"""
    d = doc()
    assert d.set("match", "threshold", 80) is False
    assert d.set("match", "threshold", 80, force=True) is True


def test_set_new_key_lands_at_end_of_its_own_section():
    d = doc()
    d.set("audio", "device", 3)
    lines = d.text().splitlines()
    i = lines.index("device = 3")
    assert lines[i - 1] == "min_peak = 0.01", "新键应接在本段内容后面"
    assert lines[i + 1] == "", "空行要留在段与段之间"
    assert lines[i + 2] == "[match]", "新键不该贴到下一段的头上"
    assert tomllib.loads(d.text())["audio"]["device"] == 3


def test_set_new_key_follows_existing_indentation():
    d = TomlDoc('[a]\n    k = 1\n')
    d.set("a", "j", 2)
    assert "    j = 2" in d.text()


def test_set_creates_missing_table_as_plain_table():
    """新建 [b] 不能写成 [[b]]——那会变成数组，读出来是 list 而不是 dict。"""
    d = TomlDoc("[a]\nx = 1\n")
    d.set("b", "y", 2)
    parsed = tomllib.loads(d.text())
    assert parsed["b"]["y"] == 2
    assert parsed["a"]["x"] == 1


def test_set_on_existing_array_section_creates_array_table():
    """段名在文件里已经是 [[x]] 时，新建的也必须是 [[x]]，否则 TOML 直接读不回来。"""
    d = TomlDoc('[[action]]\nid = "one"\nhandler = "open_app"\naliases = ["一"]\n')
    d.set("action", "id", "two", index=1)
    parsed = tomllib.loads(d.text())
    assert [a["id"] for a in parsed["action"]] == ["one", "two"]


def test_set_bool_and_float_and_list_render_correctly():
    d = doc()
    d.set("hotkey", "keys", "<f9>")
    d.set("audio", "min_peak", 0.003)
    d.set("match", "strip_suffixes", ["吧", "啊"])
    parsed = tomllib.loads(d.text())
    assert parsed["hotkey"]["keys"] == "<f9>"
    assert parsed["audio"]["min_peak"] == 0.003
    assert parsed["match"]["strip_suffixes"] == ["吧", "啊"]


def test_set_multi_line_array_replaces_whole_node():
    d = doc()
    before = len(d.text().splitlines())
    d.set("match", "strip_prefixes", ["请"])
    after = len(d.text().splitlines())
    assert after < before, "多行数组被压成单行，行数应当减少"
    assert tomllib.loads(d.text())["match"]["strip_prefixes"] == ["请"]


def test_set_inline_table_value():
    d = doc()
    d.set("match", "substitutions", {"围信": "微信"})
    assert tomllib.loads(d.text())["match"]["substitutions"] == {"围信": "微信"}


def test_remove_key():
    d = doc()
    assert d.remove("audio", "min_peak") is True
    assert "min_peak" not in tomllib.loads(d.text())["audio"]
    assert d.remove("audio", "min_peak") is False


# --------------------------------------------------------------------------- #
# 动作块
# --------------------------------------------------------------------------- #


def test_set_on_array_table_targets_the_right_element():
    d = doc()
    d.set("action", "enabled", True, index=1)
    parsed = tomllib.loads(d.text())
    assert parsed["action"][1]["enabled"] is True
    assert parsed["action"][0]["id"] == "a.one"


def test_append_and_remove_action_block():
    d = doc()
    d.append_table("action", header_comment="# 新加的")
    idx = len(d.find_all("action")) - 1
    d.set("action", "id", "a.three", index=idx)
    d.set("action", "handler", "open_app", index=idx)
    assert len(tomllib.loads(d.text())["action"]) == 3
    assert "# 新加的" in d.text()

    assert d.remove_table("action", 0) is True
    rest = tomllib.loads(d.text())["action"]
    assert [a["id"] for a in rest] == ["a.two", "a.three"]


def test_remove_table_takes_its_own_comment_but_not_the_section_doc():
    text = (
        "# 整个动作段的说明\n"
        "\n"
        "[[action]]\n"
        "id = \"x\"\n"
        "handler = \"open_app\"\n"
        "aliases = [\"x\"]\n"
    )
    d = TomlDoc(text)
    d.remove_table("action", 0)
    assert "整个动作段的说明" in d.text()
    assert "[[action]]" not in d.text()


def test_remove_table_takes_contiguous_comment_above():
    text = (
        "# 段说明\n"
        "\n"
        "# 这个是 x 的注释\n"
        "[[action]]\n"
        "id = \"x\"\n"
        "\n"
        "[[action]]\n"
        "id = \"y\"\n"
    )
    d = TomlDoc(text)
    d.remove_table("action", 0)
    out = d.text()
    assert "段说明" in out
    assert "x 的注释" not in out


def test_format_action_block_omits_defaults():
    lines = format_action_block(
        {"id": "x", "handler": "open_app", "aliases": ["一"], "describe": "", "target": "",
         "args": [], "enabled": True}
    )
    assert lines[0] == "[[action]]"
    body = "\n".join(lines)
    assert "enabled" not in body and "args" not in body and "describe" not in body
    assert 'id = "x"' in body


def test_format_action_block_keeps_disabled_flag():
    lines = format_action_block({"id": "x", "handler": "shell", "aliases": ["一"], "enabled": False})
    assert "enabled = false" in "\n".join(lines)


# --------------------------------------------------------------------------- #
# 字面量
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (0.5, "0.5"),
        ("a", '"a"'),
        (["a", "b"], '["a", "b"]'),
        ({}, "{}"),
    ],
)
def test_literal(value, expect):  # noqa: ANN001, ANN201
    assert literal(value) == expect


def test_literal_escapes_quotes_and_backslashes():
    assert literal('a"b\\c') == '"a\\"b\\\\c"'


def test_literal_rejects_unsupported_type():
    with pytest.raises(TomlEditError):
        literal(object())


def test_literal_of_rendered_value_round_trips():
    for value in (True, 12, 0.25, "中文", ["请", "帮我"], {"a": "b"}):
        assert tomllib.loads(f"v = {literal(value)}")["v"] == value


def test_format_array_breaks_long_arrays():
    out = format_array([f"item{i}" for i in range(30)])
    assert "\n" in out
    assert tomllib.loads(f"v = {out}")["v"][0] == "item0"


def test_format_array_keeps_short_arrays_inline():
    assert "\n" not in format_array(["a", "b"])


def test_format_array_empty():
    assert format_array([]) == "[]"


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #


def test_crlf_input_is_preserved():
    text = "[a]\r\nx = 1\r\n"
    d = TomlDoc(text)
    assert "\r\n" in d.text()
    assert tomllib.loads(d.text())["a"]["x"] == 1


def test_file_without_trailing_newline_gets_one():
    d = TomlDoc("[a]\nx = 1")
    assert d.text().endswith("\n")


def test_real_config_round_trips(tmp_path: Path):
    """仓库自带的 config.toml 是最复杂的真实样本。"""
    real = Path(__file__).resolve().parent.parent / "config.toml"
    if not real.is_file():  # pragma: no cover
        pytest.skip("没有 config.toml")
    text = real.read_text(encoding="utf-8")
    d = TomlDoc(text)
    assert d.text() == text
    assert len(d.find_all("action")) >= 10
    assert d.set("match", "threshold", 77) is True
    out = d.text()
    assert "# 别名模糊匹配阈值" in out, "行尾注释丢了"
    assert tomllib.loads(out)["match"]["threshold"] == 77
