"""端到端：用户配置的替换表是否真的生效。

这类「配置写了但没接线」的缺陷，单测各模块都过、跑起来却没效果，最容易漏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl.actions import build_registry
from voice_ctl.app import Pipeline
from voice_ctl.asr import AsrResult
from voice_ctl.cli import build_runtime
from voice_ctl.config import load_config
from voice_ctl.matcher import Matcher
from voice_ctl.normalize import NormalizeConfig, Normalizer

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = ROOT / "config.toml"

BASE = """
[hotkey]
keys = "<f9>"

[audio]
min_peak = 0.01

[[action]]
id = "open.wechat"
handler = "open_app"
aliases = ["微信", "wechat"]
target = "notepad.exe"

[[action]]
id = "open.notepad"
handler = "open_app"
aliases = ["记事本", "notepad"]
target = "notepad.exe"
"""


class StubAsr:
    load_ms = 0.0

    def __init__(self, text: str) -> None:
        self.text = text

    def load(self) -> None:
        return None

    def transcribe(self, samples, sample_rate: int = 16000) -> AsrResult:  # noqa: ANN001
        return AsrResult(text=self.text, duration_s=1.0, infer_ms=1.0)

    def transcribe_file(self, path):  # noqa: ANN001
        return self.transcribe(None)


def write(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(BASE + extra, encoding="utf-8")
    return p


def test_user_substitution_from_config_is_wired(tmp_path: Path):
    """配置里写的替换必须真的进入 Normalizer，而不是只被解析出来放着。"""
    cfg = load_config(write(tmp_path, '\n[normalize]\nsubstitutions = { "围信" = "微信" }\n'))
    rt = build_runtime(cfg, with_decision=False)
    assert rt.normalizer.normalize("打开围信") == "打开微信", (
        f"配置的替换没生效，归一化结果是 {rt.normalizer.normalize('打开围信')!r}"
    )
    pipe = rt.pipeline()
    out = pipe.process_text("打开围信", dry_run=True)
    assert out.action_id == "open.wechat"


def test_user_substitution_overrides_builtin(tmp_path: Path):
    """用户配置应能覆盖内置表（内置把「威信」映射到微信）。"""
    cfg = load_config(write(tmp_path, '\n[normalize]\nsubstitutions = { "威信" = "记事本" }\n'))
    rt = build_runtime(cfg, with_decision=False)
    assert rt.normalizer.normalize("打开威信") == "打开记事本"
    out = rt.pipeline().process_text("打开威信", dry_run=True)
    assert out.action_id == "open.notepad"


def test_builtin_substitution_still_works_without_config(tmp_path: Path):
    """不配 [normalize] 时，内置表必须照常生效。"""
    cfg = load_config(write(tmp_path))
    rt = build_runtime(cfg, with_decision=False)
    assert rt.normalizer.normalize("打开威信") == "打开微信"


def test_min_peak_from_config_reaches_audio_cfg(tmp_path: Path):
    cfg = load_config(write(tmp_path))
    assert cfg.audio.min_peak == 0.01

    p = tmp_path / "c2.toml"
    p.write_text(BASE.replace("min_peak = 0.01", "min_peak = 0.5"), encoding="utf-8")
    assert load_config(p).audio.min_peak == 0.5


def test_threshold_from_config_reaches_matcher(tmp_path: Path):
    p = tmp_path / "c3.toml"
    p.write_text(BASE + "\n[match]\nthreshold = 95\n", encoding="utf-8")
    cfg = load_config(p)
    rt = build_runtime(cfg, with_decision=False)
    assert rt.matcher.threshold == pytest.approx(0.95)


def test_alias_added_in_config_becomes_matchable(tmp_path: Path):
    p = tmp_path / "c4.toml"
    p.write_text(
        BASE.replace('aliases = ["记事本", "notepad"]', 'aliases = ["记事本", "notepad", "记录本"]'),
        encoding="utf-8",
    )
    cfg = load_config(p)
    out = build_runtime(cfg, with_decision=False).pipeline().process_text("打开记录本", dry_run=True)
    assert out.action_id == "open.notepad"


def test_disabled_action_in_config_is_not_matchable(tmp_path: Path):
    p = tmp_path / "c5.toml"
    p.write_text(
        BASE.replace(
            'aliases = ["记事本", "notepad"]',
            'aliases = ["记事本", "notepad"]\nenabled = false',
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    rt = build_runtime(cfg, with_decision=False)
    assert "open.notepad" not in rt.matcher.action_ids
    assert rt.pipeline().process_text("打开记事本", dry_run=True).action_id is None


def test_real_config_pipeline_works():
    """仓库自带的 config.toml 必须能一路构建出可用的 pipeline。"""
    cfg = load_config(REAL_CONFIG)
    rt = build_runtime(cfg, with_decision=False)
    pipe = rt.pipeline()
    out = pipe.process_text("打开记事本", dry_run=True)
    assert out.action_id == "open.notepad"
    assert out.result is not None and out.result.ok


def test_pipeline_audio_path_with_stub(tmp_path: Path):
    """音频路径也要能用替身跑通——否则 CI 无麦克风时测不到这条链路。"""
    cfg = load_config(write(tmp_path))
    norm = Normalizer(NormalizeConfig(inline_fillers=["一下"]))
    pipe = Pipeline(
        asr=StubAsr("打开一下记事本"),
        matcher=Matcher(cfg.enabled_actions, normalizer=norm),
        registry=build_registry(cfg.enabled_actions),
        actions=cfg.enabled_actions,
        normalizer=norm,
    )
    import numpy as np

    out = pipe.process_audio(np.zeros(16000, dtype=np.float32), dry_run=True)
    assert out.text == "打开一下记事本"
    assert out.normalized == "打开记事本"
    assert out.action_id == "open.notepad"
