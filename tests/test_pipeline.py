"""端到端 pipeline 测试：文本 → 匹配 → 执行（全程 dry_run）+ 音频路径。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from voice_ctl.actions import build_registry
from voice_ctl.app import Pipeline, Outcome, StageTiming
from voice_ctl.asr import AsrResult
from voice_ctl.config import load_config
from voice_ctl.matcher import Matcher
from voice_ctl.normalize import NormalizeConfig, Normalizer

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = ROOT / "config.toml"


class FakeAsr:
    """替身：不加载模型，直接返回预设文本，用来测 pipeline 的接线。"""

    def __init__(self, text: str = "", raises: Exception | None = None) -> None:
        self.text = text
        self.raises = raises
        self.calls = 0
        self.load_ms = 0.0

    def load(self) -> None:
        return None

    def transcribe(self, samples, sample_rate: int = 16000) -> AsrResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return AsrResult(
            text=self.text,
            language="zh",
            duration_s=len(samples) / sample_rate,
            infer_ms=1.0,
            tokens=len(self.text),
        )

    def transcribe_file(self, path):  # noqa: ANN001
        return self.transcribe(np.zeros(16000, dtype=np.float32))


@pytest.fixture(scope="module")
def cfg():
    return load_config(REAL_CONFIG)


def make_pipeline(cfg, fake_text: str = "", raises: Exception | None = None) -> Pipeline:
    norm = Normalizer(
        NormalizeConfig(
            strip_prefixes=cfg.match.strip_prefixes,
            strip_suffixes=cfg.match.strip_suffixes,
        )
    )
    return Pipeline(
        asr=FakeAsr(fake_text, raises),  # type: ignore[arg-type]
        matcher=Matcher(cfg.enabled_actions, normalizer=norm, threshold=cfg.match.threshold),
        registry=build_registry(cfg.enabled_actions),
        actions=cfg.enabled_actions,
        normalizer=norm,
    )


# --------------------------------------------------------------------------- #
# process_text
# --------------------------------------------------------------------------- #


def test_process_text_matches_and_is_dry(cfg):
    """用系统自带应用做端到端验证（微信不一定装了，notepad 一定有）。"""
    p = make_pipeline(cfg)
    out = p.process_text("打开记事本", dry_run=True)
    assert out.action_id == "open.notepad"
    assert out.via == "matcher"
    assert out.match is not None and out.match.score >= 0.8
    assert out.result is not None and out.result.ok
    assert "dry-run" in out.result.message


def test_third_party_app_match_is_reported_even_if_not_installed(cfg):
    """微信没装时：匹配必须成功（这是匹配层的职责），执行如实报失败。"""
    p = make_pipeline(cfg)
    out = p.process_text("打开微信", dry_run=False)
    assert out.action_id == "open.wechat", "匹配层不该关心程序装没装"
    if not out.result.ok:
        assert "找不到" in out.result.message
        assert out.result.detail, "找不到时必须说明尝试过哪些位置"


def test_process_text_no_match_reports_why(cfg):
    p = make_pipeline(cfg)
    out = p.process_text("今天天气不错", dry_run=True)
    assert out.action_id is None
    assert out.result is None
    assert "别名没命中" in out.note


def test_process_text_empty_input(cfg):
    p = make_pipeline(cfg)
    out = p.process_text("   ", dry_run=True)
    assert out.action_id is None
    assert "空" in out.note


def test_process_text_records_timing(cfg):
    p = make_pipeline(cfg)
    out = p.process_text("截屏", dry_run=True)
    assert out.timing.total_ms > 0
    assert "match" in out.timing.values or "normalize" in out.timing.values


def test_process_text_normalizes_fillers(cfg):
    p = make_pipeline(cfg)
    out = p.process_text("请帮我打开一下微信", dry_run=True)
    assert out.action_id == "open.wechat"
    assert out.normalized == "打开微信"


def test_outcome_report_is_readable(cfg):
    p = make_pipeline(cfg)
    out = p.process_text("打开记事本", dry_run=True)
    text = out.report()
    assert "听到" in text
    assert "open.notepad" in text
    assert "执行" in text


def test_unknown_action_id_in_match_is_reported(cfg):
    """匹配到了但注册表里没有——必须报错而不是静默。"""
    p = make_pipeline(cfg)
    out = Outcome(text="x", normalized="x", timing=StageTiming())
    from voice_ctl.matcher import Match

    p.registry = build_registry([])  # type: ignore[assignment]
    m = Match("ghost", 1.0, "x", "exact")
    res = p.execute("ghost", m, out.timing, dry_run=True)
    assert not res.ok
    assert "不在注册表" in res.message


# --------------------------------------------------------------------------- #
# process_audio
# --------------------------------------------------------------------------- #


def test_process_audio_full_chain(cfg):
    p = make_pipeline(cfg, fake_text="打开微信")
    samples = np.zeros(16000, dtype=np.float32)
    out = p.process_audio(samples, dry_run=True)
    assert out.text == "打开微信"
    assert out.asr is not None
    assert out.asr.duration_s == pytest.approx(1.0)
    assert out.action_id == "open.wechat"
    assert out.timing.values.get("asr", 0) > 0


def test_process_audio_empty_transcript(cfg):
    p = make_pipeline(cfg, fake_text="")
    out = p.process_audio(np.zeros(16000, dtype=np.float32), dry_run=True)
    assert out.action_id is None
    assert "识别结果为空" in out.note


def test_process_audio_asr_error_is_contained(cfg):
    from voice_ctl.asr import AsrError

    p = make_pipeline(cfg, raises=AsrError("模型炸了"))
    out = p.process_audio(np.zeros(16000, dtype=np.float32), dry_run=True)
    assert out.action_id is None
    assert "识别失败" in out.note
    assert "模型炸了" in out.note


def test_action_exception_does_not_escape(cfg):
    """动作内部抛异常时必须被吃住——语音助手不能因为一个动作崩掉。"""
    p = make_pipeline(cfg)

    class Boom:
        id = "open.notepad"

        def execute(self, ctx):  # noqa: ANN001
            raise RuntimeError("boom")

    p.registry._by_id["open.notepad"] = Boom()  # type: ignore[attr-defined]
    out = p.process_text("打开记事本", dry_run=False)
    assert out.result is not None
    assert not out.result.ok
    assert "boom" in out.result.message


# --------------------------------------------------------------------------- #
# StageTiming
# --------------------------------------------------------------------------- #


def test_stage_timing_accumulates():
    t = StageTiming()
    t.add("asr", 10.0)
    t.add("asr", 5.0)
    t.add("match", 1.0)
    assert t.values["asr"] == 15.0
    assert t.total_ms == 16.0
    assert "asr=15ms" in t.summary()


def test_stage_timing_empty_summary():
    assert StageTiming().summary() == "无耗时记录"
