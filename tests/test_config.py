"""配置加载、校验、类型检查测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl.config import AppConfig, ConfigError, HotkeyConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = ROOT / "config.toml"

# 最小可用骨架。[hotkey] 段必须在最前，因为 TOML 里同一张表不能定义两次——
# 后面各测试要加自己的段时，一律用 + 追加到 ACTION_ONLY 之后。
ACTION_ONLY = """
[[action]]
id = "a1"
handler = "open_app"
aliases = ["记事本"]
target = "notepad.exe"
"""

HOTKEY = '[hotkey]\nkeys = "<f9>"\n'

MINIMAL = HOTKEY + ACTION_ONLY


def write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_real_config_loads():
    """仓库里的 config.toml 必须始终可加载——它是新用户的第一个入口。"""
    cfg = load_config(REAL_CONFIG)
    assert cfg.hotkey.keys == "<ctrl>+<alt>+space"
    assert len(cfg.enabled_actions) >= 10
    ids = [a.id for a in cfg.actions]
    assert len(ids) == len(set(ids)), "动作 id 有重复"


def test_minimal_config(tmp_path: Path):
    cfg = load_config(write_cfg(tmp_path, MINIMAL))
    assert cfg.hotkey.keys == "<f9>"
    assert len(cfg.actions) == 1
    assert cfg.actions[0].id == "a1"


def test_unknown_top_level_section(tmp_path: Path):
    with pytest.raises(ConfigError, match="顶层有无法识别的段"):
        load_config(write_cfg(tmp_path, MINIMAL + "\n[nonsense]\nx = 1\n"))


def test_normalize_section_substitutions(tmp_path: Path):
    """[normalize].substitutions 让用户不改代码就能补同音纠错。"""
    body = MINIMAL + '\n[normalize]\nsubstitutions = { "围信" = "微信" }\n'
    cfg = load_config(write_cfg(tmp_path, body))
    assert cfg.normalize.substitutions == {"围信": "微信"}


def test_normalize_section_defaults_are_permissive():
    cfg = AppConfig(actions=[])
    assert cfg.normalize.substitutions == {}
    assert cfg.normalize.use_pinyin is True


def test_normalize_substitution_type_checked(tmp_path: Path):
    body = MINIMAL + '\n[normalize]\nsubstitutions = "围信"\n'
    with pytest.raises(ConfigError, match="必须是表"):
        load_config(write_cfg(tmp_path, body))


def test_unknown_key_in_section(tmp_path: Path):
    body = '[hotkey]\nkeys = "<f9>"\ntypo_key = 1\n' + ACTION_ONLY
    with pytest.raises(ConfigError, match="无法识别的键"):
        load_config(write_cfg(tmp_path, body))


def test_bad_handler(tmp_path: Path):
    body = MINIMAL.replace('handler = "open_app"', 'handler = "teleport"')
    with pytest.raises(ConfigError, match="不认识"):
        load_config(write_cfg(tmp_path, body))


def test_duplicate_action_id(tmp_path: Path):
    body = MINIMAL + """
[[action]]
id = "a1"
handler = "open_app"
aliases = ["计算器"]
target = "calc.exe"
"""
    with pytest.raises(ConfigError, match="重复"):
        load_config(write_cfg(tmp_path, body))


def test_no_actions(tmp_path: Path):
    with pytest.raises(ConfigError, match="没有任何"):
        load_config(write_cfg(tmp_path, HOTKEY))


def test_all_actions_disabled(tmp_path: Path):
    body = MINIMAL.replace('handler = "open_app"', 'handler = "open_app"\nenabled = false')
    with pytest.raises(ConfigError, match="无事可做"):
        load_config(write_cfg(tmp_path, body))


def test_samplerate_must_be_16000(tmp_path: Path):
    with pytest.raises(ConfigError, match="16000"):
        load_config(write_cfg(tmp_path, MINIMAL + "\n[audio]\nsamplerate = 44100\n"))


def test_handler_requires_target(tmp_path: Path):
    body = """
[[action]]
id = "x"
handler = "open_url"
aliases = ["某个网站"]
"""
    with pytest.raises(ConfigError, match="必须提供 target"):
        load_config(write_cfg(tmp_path, body))


def test_max_duration_must_exceed_min(tmp_path: Path):
    body = '[hotkey]\nkeys = "<f9>"\nmin_duration_ms = 500\nmax_duration_ms = 100\n' + ACTION_ONLY
    with pytest.raises(ConfigError, match="必须大于"):
        load_config(write_cfg(tmp_path, body))


def test_toml_syntax_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="TOML 语法错误"):
        load_config(write_cfg(tmp_path, "[hotkey\nkeys = 1"))


def test_missing_file():
    with pytest.raises(ConfigError, match="不存在"):
        load_config("definitely/not/here.toml")


def test_describe_runs():
    text = load_config(REAL_CONFIG).describe()
    assert "热键" in text
    assert "动作数" in text


def test_model_path_is_absolute_and_relative_to_config():
    cfg = load_config(REAL_CONFIG)
    p = cfg.model_path()
    assert p.is_absolute()
    assert p.name == "sense-voice-int8"
    assert p.parent == REAL_CONFIG.parent / "models"


def test_defaults_are_sane():
    cfg = AppConfig(actions=[])
    assert cfg.audio.samplerate == 16000
    assert cfg.model.use_itn is True
    assert cfg.decision.enabled is False, "语义层必须默认关闭（会拉进 torch）"
    assert "一下" in cfg.match.inline_fillers


# --------------------------------------------------------------------------- #
# 类型校验 —— 这批测试来自真实踩过的坑
# --------------------------------------------------------------------------- #


def test_bool_as_string_is_rejected(tmp_path: Path):
    """`use_itn = "true"` 能通过 TOML 解析，然后被当成真值一路用下去。

    这类静默 bug 极难查，所以配置层必须挡住。
    """
    with pytest.raises(ConfigError, match="布尔"):
        load_config(write_cfg(tmp_path, MINIMAL + '\n[model]\nuse_itn = "true"\n'))


def test_int_where_str_expected_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="期望 str"):
        load_config(write_cfg(tmp_path, MINIMAL + "\n[model]\ndir = 123\n"))


def test_bool_where_int_expected_is_rejected(tmp_path: Path):
    """bool 是 int 的子类，不特判的话 num_threads = true 会被放过去。"""
    with pytest.raises(ConfigError, match="布尔"):
        load_config(write_cfg(tmp_path, MINIMAL + "\n[model]\nnum_threads = true\n"))


def test_float_where_str_expected(tmp_path: Path):
    body = '[hotkey]\nkeys = 3.5\n' + ACTION_ONLY
    with pytest.raises(ConfigError, match="期望 str"):
        load_config(write_cfg(tmp_path, body))


def test_list_type_is_checked(tmp_path: Path):
    with pytest.raises(ConfigError, match="必须是数组"):
        load_config(write_cfg(tmp_path, MINIMAL + '\n[match]\nstrip_prefixes = "请"\n'))


def test_list_element_type_is_checked(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"strip_prefixes\[0\]"):
        load_config(write_cfg(tmp_path, MINIMAL + "\n[match]\nstrip_prefixes = [1, 2]\n"))


def test_type_hints_actually_resolve():
    """本模块有 from __future__ import annotations，注解是字符串。

    如果不解析就拿来比较，校验会永远通过——防护变成摆设。这条测试守这一点。
    """
    from voice_ctl.config import _resolved_hints

    hints = _resolved_hints(HotkeyConfig)
    assert hints["min_duration_ms"] is int, f"注解没解析出来：{hints['min_duration_ms']!r}"
    assert hints["keys"] is str


def test_toml_uppercase_boolean_is_a_syntax_error(tmp_path: Path):
    """TOML 的布尔只能小写。写成 Python 风格的 True 是语法错误。

    这是实际踩过的坑：config.toml 里写了 `use_itn = True`，整个文件解析失败。
    """
    with pytest.raises(ConfigError, match="TOML 语法错误"):
        load_config(write_cfg(tmp_path, MINIMAL + "\n[model]\nuse_itn = True\n"))
