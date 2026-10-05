"""把 UI 上的改动写回 config.toml。

分两层：

    TomlDoc     —— 只认文本，负责「在哪一行、怎么改」（toml_edit.py）
    ConfigEditor —— 认得 AppConfig，负责「这次改动和文件里差在哪」

分开的好处是：**只写差异**这件事有地方落脚。UI 上点一次保存，实际写盘的
通常只有一两个键，其余字节一个不动——包括用户手写的注释、空行、以及
他自己偏好的写法（`0.010` 不会被改写成 `0.01`）。

写盘一律先备份成 config.toml.bak：用户手改坏了配置想回退时，这个文件
往往是他唯一的救命稻草。
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ActionConfig, AppConfig
from .toml_edit import TomlDoc, TomlEditError, format_action_block

# AppConfig 里哪些字段该写回文件。顺序也是写新文件时的段顺序。
SETTINGS_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hotkey", ("keys", "min_duration_ms", "max_duration_ms")),
    ("audio", ("samplerate", "channels", "device", "min_peak")),
    ("model", ("dir", "language", "use_itn", "num_threads", "provider", "pad_ms")),
    ("normalize", ("substitutions", "use_pinyin")),
    ("match", ("threshold", "strip_prefixes", "strip_suffixes")),
    ("decision", ("enabled", "model", "min_confidence", "onnx_dir")),
    ("intent", ("enabled", "confirm_timeout")),
    ("schedule", ("enabled", "data_file", "remind_before")),
    ("web", ("enabled", "search_fallback", "search_url")),
    ("llm", ("enabled", "endpoint", "model", "timeout", "max_candidates")),
    ("feedback", ("beep", "beep_start_hz", "beep_end_hz", "beep_ms", "print_result")),
)

_ACTION_FIELDS = ("id", "handler", "aliases", "describe", "target", "args", "enabled")


class ConfigEditor:
    """对一份 config.toml 的差异式改写。"""

    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            if not create:
                raise FileNotFoundError(f"配置文件不存在：{self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(_SKELETON, encoding="utf-8")
        self.doc = TomlDoc(self.path.read_text(encoding="utf-8"), source=self.path)
        self.changes: list[str] = []

    # -- 设置 ------------------------------------------------------------- #

    def set(self, section: str, key: str, value: Any, *, quiet: bool = False) -> bool:
        if value is None:
            # 例如 [audio].device：None 表示"用系统默认设备"，
            # TOML 没有 null，正确表达是「这一行不写」
            if self.doc.remove(section, key):
                self._note(f"[{section}].{key} 删除（用默认值）", quiet)
                return True
            return False
        try:
            if self.doc.set(section, key, value):
                self._note(f"[{section}].{key} = {value!r}", quiet)
                return True
        except TomlEditError as e:
            raise TomlEditError(f"写 {section}.{key} 失败：{e}") from e
        return False

    def apply_settings(self, cfg: AppConfig) -> list[str]:
        """把整份 AppConfig 里**和文件不同**的字段写进去。

        一条克制的规则：**文件里本来没有、值又等于代码默认值的键，不写**。
        否则用户打开设置页、什么都没改、点一下保存，文件里会凭空多出十几行
        （[model]、[normalize]、[decision] 整段加上一堆默认值）——他会认为
        程序擅自改了他的配置。这跟"只写差异"的承诺是矛盾的。

        真的改了值（和默认值不同）才补出来。缺的键留在缺失状态是安全的：
        加载时本来就会回落到同样的默认值。
        """
        base = AppConfig()
        for section, keys in SETTINGS_FIELDS:
            data = asdict(getattr(cfg, section))
            defaults = asdict(getattr(base, section))
            for key in keys:
                if key not in data:
                    continue
                if not self.doc.has_key(section, key) and data[key] == defaults.get(key):
                    continue
                self.set(section, key, data[key])
        return list(self.changes)

    # -- 动作 ------------------------------------------------------------- #

    def set_action(self, index: int, **fields: Any) -> bool:
        any_change = False
        for key, value in fields.items():
            if key not in _ACTION_FIELDS:
                raise TomlEditError(f"动作没有 {key!r} 这个字段")
            # 空列表 / 空字符串没有写回的价值：省略更短，也和默认值等价
            if key == "args" and not value:
                if self.doc.remove("action", "args", index=index):
                    self._note(f"动作[{index}].args 删除")
                continue
            if self.doc.set("action", key, value, index=index):
                self._note(f"动作[{index}].{key} = {value!r}")
                any_change = True
        return any_change

    def append_action(self, action: dict[str, Any], *, comment: str = "") -> int:
        """在文件末尾追加一个动作块，返回它的下标。

        直接拼行而不是逐个 doc.set()：块是全新的，没有"保留原格式"的诉求，
        而拼行能让「空字段不写」这条规则只存在于 format_action_block 一处。
        """
        if self.doc.lines and self.doc.lines[-1].strip():
            self.doc.lines.append("")
        self.doc.lines.extend(format_action_block(action, comment=comment))
        self.doc.reparse()
        index = len(self.doc.find_all("action")) - 1
        self._note(f"新增动作 {action.get('id', '?')!r}")
        return index

    def append_action_cfg(self, cfg: ActionConfig, *, comment: str = "") -> int:
        return self.append_action(asdict(cfg), comment=comment)

    def remove_action(self, index: int) -> bool:
        tables = self.doc.find_all("action")
        if not (0 <= index < len(tables)):
            return False
        aid = self.doc.value("action", "id", index, "?")
        if self.doc.remove_table("action", index):
            self._note(f"删除动作 {aid!r}")
            return True
        return False

    def move_action(self, index: int, delta: int) -> bool:
        """把第 index 个动作和相邻的第 index+delta 个**互换字段**。

        为什么不搬行：搬行要同时处理块上方的注释、空行、以及删除后所有
        后续块的行号偏移——错一个就会出现"注释跟着别的动作走了"。
        动作块的字段是可枚举的，互换字段等价于互换位置，而且绝不破坏
        文件里其它任何字节。
        """
        n = len(self.doc.find_all("action"))
        other = index + delta
        if not (0 <= index < n) or not (0 <= other < n):
            return False
        a = {k: self.doc.value("action", k, index, None) for k in _ACTION_FIELDS}
        b = {k: self.doc.value("action", k, other, None) for k in _ACTION_FIELDS}
        for key in _ACTION_FIELDS:
            va, vb = a.get(key), b.get(key)
            if va is None and vb is None:
                continue
            # b 的值落到 index，a 的值落到 other —— 真·互换
            self._put(index, key, vb)
            self._put(other, key, va)
        self._note(f"交换动作 {a.get('id', '?')!r} 与 {b.get('id', '?')!r} 的位置")
        return True

    def _put(self, index: int, key: str, value: Any) -> None:
        """force=True：交换时两边值可能恰好相等，但仍然必须落到正确的块上。"""
        if value is None:
            self.doc.remove("action", key, index=index)
        else:
            self.doc.set("action", key, value, index=index, force=True)

    # -- 落盘 ------------------------------------------------------------- #

    def _note(self, what: str, quiet: bool = False) -> None:
        if not quiet:
            self.changes.append(what)

    @property
    def dirty(self) -> bool:
        return bool(self.changes)

    def commit(self, *, backup: bool = True) -> Path | None:
        """写盘前先确认「改完的配置真的能读回来」。没有任何改动时不碰文件。

        两道自检，缺一不可：

          1. `tomllib` 语法检查——挡住写坏 TOML 的情况
          2. **真的用 load_config 加载一遍**——挡住的才是要命的那类：
             语法没问题、但语义非法（比如新建的动作既没有说法也没有目标）。
             这类文件写下去之后，程序下次启动直接起不来，而用户完全不知道
             是自己刚点的那一下"新建"造成的。

        这是实测踩出来的：图形界面上点「新建动作」，新动作只有 id 和 handler，
        validate() 判定"既没有 aliases 也没有 target"，于是写进磁盘的配置
        从此加载失败——而且因为加载失败，界面会保留旧配置，看起来像"新建没反应"，
        真正的故障要等下次重启才爆发。

        第 2 步用同目录的临时文件，保证相对路径（模型目录等）解析结果一致。
        """
        if not self.changes:
            return None
        text = self.doc.text()
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise TomlEditError(f"改写后的配置不是合法 TOML，已放弃写入：{e}") from e

        self._validate_semantics(text)

        if backup and self.path.is_file():
            try:
                shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
            except OSError:
                pass
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def _validate_semantics(self, text: str) -> None:
        import tempfile

        from .config import ConfigError, load_config

        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".toml", delete=False, dir=str(self.path.parent), encoding="utf-8"
            ) as fh:
                fh.write(text)
                tmp = Path(fh.name)
            load_config(tmp)
        except ConfigError as e:
            raise TomlEditError(
                f"改完的配置加载不了，已放弃写入（你的文件一个字节都没动）：{e}"
            ) from e
        except OSError:
            # 目录不可写之类：写盘那一步自然会报，这里不抢着拦
            pass
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


_SKELETON_HEAD = """# voice-ctl 配置（由图形界面生成）
#
# 按住热键说话 → 本地离线识别 → 匹配动作 → 执行。
# 在 [[action]] 段加一条就能多一个能力，不用改代码。
# handler 支持：open_app / open_path / open_url / sysctl / keys / shell

[hotkey]
keys = "<ctrl>+<alt>+space"
min_duration_ms = 200
max_duration_ms = 15000

[audio]
samplerate = 16000
channels = 1
min_peak = 0.01

[model]
dir = "models/sense-voice-int8"
language = "auto"
use_itn = true
num_threads = 2
provider = "cpu"

[normalize]
substitutions = {}
use_pinyin = true

[match]
threshold = 80

[decision]
enabled = false
model = "multilingual"
min_confidence = 0.6
onnx_dir = "models/laya-onnx/multilingual"

[web]
enabled = true
search_fallback = true
search_url = "https://www.baidu.com/s?wd={q}"

[feedback]
beep = true
beep_start_hz = 880
beep_end_hz = 1320
beep_ms = 70
print_result = true

# --------------------------------------------------------------------------- #
# 动作表
# --------------------------------------------------------------------------- #
"""


def skeleton_text() -> str:
    """没有配置文件时生成的那一份。

    动作表**从 config._builtin_actions() 生成**，而不是手抄一遍：手抄的那份
    迟早和代码里的兜底动作集分叉，而分叉的表现是"UI 生成的配置里少了几个
    明明能用的动作"——极难注意到。

    另一个必须这么做的理由：一份没有 [[action]] 的配置是**非法的**
    （validate() 会拒绝），所以骨架里必须有动作，否则「保存」永远失败。
    """
    from .config import _builtin_actions  # 同包内复用兜底动作表，避免抄两份
    from .toml_edit import format_action_block

    lines = [_SKELETON_HEAD.rstrip("\n"), ""]
    for a in _builtin_actions():
        lines.extend(format_action_block(asdict(a)))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_SKELETON = skeleton_text()


def new_action_template(aid: str = "custom.new") -> dict[str, Any]:
    """新建动作对话框的初值。

    `aliases` 必须给一条占位说法——不是偷懒，是**校验要求**：动作不能既没有
    说法也没有目标，否则它永远匹配不上，配置也会被判非法。留空会让「新建」
    直接写出一份加载不了的配置。
    `target` 留空是安全的：open_app 允许空 target（运行时靠 appfind 按名字找），
    找不到也只是预检显示「找不到」，不会让配置失效。
    """
    return {
        "id": aid,
        "handler": "open_app",
        "aliases": ["新动作"],
        "describe": "新动作：改好「说法」和「目标」再保存",
        "target": "",
        "args": [],
        "enabled": True,
    }


__all__ = [
    "SETTINGS_FIELDS",
    "ConfigEditor",
    "format_action_block",
    "new_action_template",
]
