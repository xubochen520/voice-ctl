"""配置回写。

最重要的是两条底线：
  1. 没改的地方一个字节都不动（注释是仓库 config.toml 的主要价值）
  2. **绝不写出加载不回来的配置**——那会让程序下次启动直接起不来
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from voice_ctl.config import load_config
from voice_ctl.confedit import ConfigEditor, new_action_template
from voice_ctl.toml_edit import TomlEditError

REAL = Path(__file__).resolve().parent.parent / "config.toml"

MINIMAL = """\
# 说明注释

[hotkey]
keys = "<ctrl>+<alt>+space"     # 按住说话
min_duration_ms = 200

[audio]
samplerate = 16000
channels = 1
min_peak = 0.01

[match]
threshold = 80

[feedback]
beep = true

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

[[action]]
id = "a.three"
handler = "sysctl"
aliases = ["三"]
target = "mute"
enabled = false
"""


@pytest.fixture()
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(MINIMAL, encoding="utf-8")
    return p


@pytest.fixture()
def real_path(tmp_path: Path) -> Path:
    if not REAL.is_file():  # pragma: no cover
        pytest.skip("没有仓库自带的 config.toml")
    p = tmp_path / "config.toml"
    shutil.copy2(REAL, p)
    return p


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #


def test_no_change_writes_nothing(cfg_path: Path):
    before = cfg_path.read_text(encoding="utf-8")
    ed = ConfigEditor(cfg_path)
    assert ed.apply_settings(load_config(cfg_path)) == []
    assert ed.dirty is False
    assert ed.commit() is None
    assert cfg_path.read_text(encoding="utf-8") == before
    assert not cfg_path.with_suffix(".toml.bak").exists(), "没改动不该产生备份"


def test_single_change_only_touches_that_line(cfg_path: Path):
    before = cfg_path.read_text(encoding="utf-8")
    cfg = load_config(cfg_path)
    cfg.match.threshold = 95
    ed = ConfigEditor(cfg_path)
    assert ed.apply_settings(cfg) == ["[match].threshold = 95"]
    ed.commit()
    after = cfg_path.read_text(encoding="utf-8")
    assert after.count("\n") == before.count("\n"), "只改一个值，行数不该变"
    assert "# 说明注释" in after
    assert load_config(cfg_path).match.threshold == 95


def test_trailing_comment_survives_value_change(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    ed.set("hotkey", "min_duration_ms", 250)
    ed.commit()
    assert "# 按住说话" in cfg_path.read_text(encoding="utf-8")


def test_none_removes_the_key(cfg_path: Path):
    """[audio].device 的 None 语义是"用系统默认"，TOML 没有 null。"""
    ed = ConfigEditor(cfg_path)
    ed.set("audio", "device", 3)
    ed.commit()
    assert tomllib.loads(cfg_path.read_text(encoding="utf-8"))["audio"]["device"] == 3

    ed = ConfigEditor(cfg_path)
    ed.set("audio", "device", None)
    ed.commit()
    assert "device" not in tomllib.loads(cfg_path.read_text(encoding="utf-8"))["audio"]


def test_backup_written_on_commit(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    ed.set("match", "threshold", 91)
    ed.commit()
    bak = cfg_path.with_suffix(".toml.bak")
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == MINIMAL


def test_unknown_key_blocks_write(cfg_path: Path):
    """未知键在 doc 层能写进去（语法合法），但 commit 的语义校验会拦下来。"""
    before = cfg_path.read_text(encoding="utf-8")
    ed = ConfigEditor(cfg_path)
    ed.set("hotkey", "nonsense_key", 1)
    assert "nonsense_key" in ed.doc.text()
    with pytest.raises(TomlEditError):
        ed.commit()
    assert cfg_path.read_text(encoding="utf-8") == before


def test_create_editor_makes_skeleton(tmp_path: Path):
    p = tmp_path / "sub" / "config.toml"
    ed = ConfigEditor(p, create=True)
    assert p.is_file()
    ed.set("match", "threshold", 70)
    ed.commit()
    assert load_config(p).match.threshold == 70


def test_missing_file_without_create_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ConfigEditor(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #


def test_toggle_action_enabled(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    assert ed.set_action(0, enabled=False) is True
    ed.commit()
    cfg = load_config(cfg_path)
    assert cfg.actions[0].enabled is False
    assert len(cfg.enabled_actions) == 1


def test_edit_aliases_and_id(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    ed.set_action(0, aliases=["一", "壹", "头一个"], id="a.first")
    ed.commit()
    cfg = load_config(cfg_path)
    assert cfg.actions[0].id == "a.first"
    assert cfg.actions[0].aliases == ["一", "壹", "头一个"]


def test_append_action(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    idx = ed.append_action({"id": "a.four", "handler": "open_url",
                            "aliases": ["四"], "target": "https://example.com"})
    assert idx == 3
    ed.commit()
    cfg = load_config(cfg_path)
    assert [a.id for a in cfg.actions] == ["a.one", "a.two", "a.three", "a.four"]
    assert cfg.actions[3].target == "https://example.com"


def test_append_action_writes_comment(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    ed.append_action({"id": "a.x", "handler": "open_app", "aliases": ["x"]}, comment="# 我加的")
    ed.commit()
    assert "# 我加的" in cfg_path.read_text(encoding="utf-8")


def test_remove_action(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    assert ed.remove_action(0) is True
    ed.commit()
    assert [a.id for a in load_config(cfg_path).actions] == ["a.two", "a.three"]


def test_remove_action_out_of_range(cfg_path: Path):
    assert ConfigEditor(cfg_path).remove_action(9) is False


def test_move_action_swaps_fields(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    assert ed.move_action(0, 1) is True
    ed.commit()
    cfg = load_config(cfg_path)
    assert [a.id for a in cfg.actions] == ["a.two", "a.one", "a.three"]
    # 属性要跟着动作走，而不是留在原来的位置上
    assert cfg.actions[0].target == "lock"
    assert cfg.actions[1].target == "notepad.exe"


def test_move_action_out_of_range(cfg_path: Path):
    ed = ConfigEditor(cfg_path)
    assert ed.move_action(0, -1) is False
    assert ed.move_action(2, 1) is False


# --------------------------------------------------------------------------- #
# 语义校验：绝不能写出加载不回来的配置
# --------------------------------------------------------------------------- #


def test_commit_refuses_semantically_invalid_config(cfg_path: Path):
    """回归：图形界面点「新建」时，新动作既没有说法也没有目标，
    validate() 会判非法。这类文件一旦写下去，程序下次启动直接起不来。"""
    before = cfg_path.read_text(encoding="utf-8")
    ed = ConfigEditor(cfg_path)
    ed.append_action({"id": "a.bad", "handler": "open_app", "aliases": [],
                      "describe": "", "target": "", "args": [], "enabled": True})
    assert ed.dirty, "append 本身是成功的"
    with pytest.raises(TomlEditError) as ei:
        ed.commit()
    assert "读" in str(ei.value) or "加载" in str(ei.value)
    assert cfg_path.read_text(encoding="utf-8") == before, "文件必须一个字节都没动"
    assert not cfg_path.with_suffix(".toml.bak").exists()


def test_commit_refuses_duplicate_action_id(cfg_path: Path):
    before = cfg_path.read_text(encoding="utf-8")
    ed = ConfigEditor(cfg_path)
    ed.append_action({"id": "a.one", "handler": "open_app", "aliases": ["重复"]})
    with pytest.raises(TomlEditError):
        ed.commit()
    assert cfg_path.read_text(encoding="utf-8") == before


def test_commit_refuses_mixed_table_forms(cfg_path: Path):
    """[action] 和 [[action]] 同时出现，TOML 判 cannot redefine table。"""
    before = cfg_path.read_text(encoding="utf-8")
    ed = ConfigEditor(cfg_path)
    ed.doc.lines.append("")
    ed.doc.lines.append("[action]")
    ed.doc.lines.append('id = "broken"')
    ed.doc.reparse()
    ed._note("手写了一个不合法的段")  # noqa: SLF001
    with pytest.raises(TomlEditError):
        ed.commit()
    assert cfg_path.read_text(encoding="utf-8") == before


def test_new_action_template_is_loadable(cfg_path: Path):
    """回归：新建动作的初值必须本身就是合法的。"""
    ed = ConfigEditor(cfg_path)
    ed.append_action(new_action_template("custom.new"))
    ed.commit()  # 不该抛
    cfg = load_config(cfg_path)
    added = [a for a in cfg.actions if a.id == "custom.new"]
    assert added and added[0].aliases, "占位说法必须在，否则动作永远匹配不上"


# --------------------------------------------------------------------------- #
# 真实配置文件
# --------------------------------------------------------------------------- #


def test_real_config_settings_round_trip(real_path: Path):
    before = real_path.read_text(encoding="utf-8")
    cfg = load_config(real_path)
    ed = ConfigEditor(real_path)
    assert ed.apply_settings(cfg) == []

    cfg.model.use_itn = False
    cfg.feedback.beep_ms = 120
    cfg.audio.min_peak = 0.004
    ed = ConfigEditor(real_path)
    changes = ed.apply_settings(cfg)
    assert len(changes) == 3
    ed.commit()

    after = real_path.read_text(encoding="utf-8")
    assert "# 别名模糊匹配阈值" in after
    assert "# device 留空（不写这一行）用系统默认麦克风。" in after
    assert len(after.splitlines()) == len(before.splitlines())

    back = load_config(real_path)
    assert back.model.use_itn is False
    assert back.feedback.beep_ms == 120
    assert back.audio.min_peak == 0.004


def test_real_config_action_edits_keep_everything_else(real_path: Path):
    cfg = load_config(real_path)
    n = len(cfg.actions)
    ed = ConfigEditor(real_path)
    ed.set_action(0, enabled=False)
    ed.commit()
    after = load_config(real_path)
    assert len(after.actions) == n
    assert after.actions[0].enabled is False
    assert after.actions[1].id == cfg.actions[1].id
    text = real_path.read_text(encoding="utf-8")
    assert "例子：模拟按键" in text
    assert "handler 支持：open_app" in text
