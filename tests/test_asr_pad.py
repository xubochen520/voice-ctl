"""识别前补静音。

来自一次实测：TTS 合成的「明天早上八点半提醒我开会」被识别成「天早上八点半…」，
首字「明」丢了；头尾各补 ≥200ms 静音后 8/8 全对。日程场景里这是最危险的一类错误
（「明天」悄悄变成「今天」，错得很合理）。
"""

from __future__ import annotations

import numpy as np
import pytest

from voice_ctl.asr import DEFAULT_PAD_MS, SAMPLE_RATE, Asr


class FakeStream:
    def __init__(self) -> None:
        self.wave: np.ndarray | None = None
        self.result = type("R", (), {"text": "测试", "tokens": ["测", "试"], "lang": "", "emotion": "", "event": ""})()

    def accept_waveform(self, rate: int, audio: np.ndarray) -> None:
        assert rate == SAMPLE_RATE
        self.wave = audio


class FakeRecognizer:
    def __init__(self) -> None:
        self.streams: list[FakeStream] = []

    def create_stream(self) -> FakeStream:
        s = FakeStream()
        self.streams.append(s)
        return s

    def decode_stream(self, stream: FakeStream) -> None:  # noqa: ARG002
        return None


def make(pad_ms: int | None = None) -> tuple[Asr, FakeRecognizer]:
    kw = {} if pad_ms is None else {"pad_ms": pad_ms}
    asr = Asr("models/none", **kw)
    rec = FakeRecognizer()
    asr._rec = rec  # type: ignore[assignment]  # 绕过 load()：测的是喂进去的波形
    return asr, rec


def test_default_pad_is_applied_on_both_ends():
    asr, rec = make()
    x = np.full(SAMPLE_RATE, 0.5, dtype=np.float32)  # 1 秒
    asr.transcribe(x)
    fed = rec.streams[0].wave
    pad = SAMPLE_RATE * DEFAULT_PAD_MS // 1000
    assert fed is not None and len(fed) == len(x) + 2 * pad
    assert not fed[:pad].any() and not fed[-pad:].any(), "头尾必须是纯静音"
    assert np.array_equal(fed[pad:-pad], x), "中间必须是原始波形，一个样本都不能动"


def test_pad_zero_disables_it():
    asr, rec = make(pad_ms=0)
    x = np.ones(1000, dtype=np.float32)
    asr.transcribe(x)
    assert len(rec.streams[0].wave) == 1000


def test_pad_does_not_distort_reported_duration():
    """RTF = 推理时间 / 音频时长。时长必须是原始音频的，否则补的静音会把 RTF 稀释得好看。"""
    asr, _ = make(pad_ms=500)
    res = asr.transcribe(np.zeros(SAMPLE_RATE * 2, dtype=np.float32))
    assert res.duration_s == pytest.approx(2.0)


def test_empty_audio_is_not_padded_into_something():
    asr, rec = make()
    asr.transcribe(np.zeros(0, dtype=np.float32))
    assert len(rec.streams[0].wave) == 0


def test_negative_pad_is_clamped():
    assert Asr("x", pad_ms=-5).pad_ms == 0


def test_same_model_as_ignores_pad_but_not_model_params():
    a = Asr("models/a", pad_ms=100)
    assert a.same_model_as(Asr("models/a", pad_ms=900)), "pad_ms 只影响波形，不该触发重新加载模型"
    assert not a.same_model_as(Asr("models/b"))
    assert not a.same_model_as(Asr("models/a", num_threads=4))
    assert not a.same_model_as(Asr("models/a", use_itn=False))
    assert not a.same_model_as(Asr("models/a", language="zh"))
    assert not a.same_model_as(Asr("models/a", provider="cuda"))


def test_config_pad_ms_validation(tmp_path):
    from voice_ctl.config import ConfigError, load_config

    base = '[[action]]\nid="a"\nhandler="open_url"\ntarget="https://x"\naliases=["甲"]\n'
    ok = tmp_path / "ok.toml"
    ok.write_text("[model]\npad_ms = 250\n" + base, encoding="utf-8")
    assert load_config(ok).model.pad_ms == 250

    bad = tmp_path / "bad.toml"
    bad.write_text("[model]\npad_ms = 99999\n" + base, encoding="utf-8")
    with pytest.raises(ConfigError, match="pad_ms"):
        load_config(bad)
