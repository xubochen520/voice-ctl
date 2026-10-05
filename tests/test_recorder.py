"""录音静音门限测试。

这批测试来自一个实测发现的坑：最初用 RMS 当静音判据，阈值 0.006。实际测量
发现安静环境下**真实说话**的 RMS 可以低到 0.002（与底噪同级），于是用户的
小声指令会被静默丢弃——表现为"有时候喊了没反应"，极难排查。

正确判据是峰值：安静的真语音 RMS 低但峰值明显，纯静音峰值也接近 0。
"""

from __future__ import annotations

import numpy as np
import pytest

from voice_ctl.recorder import SAMPLE_RATE, Recording, Recorder, RecorderError


def mk(samples: np.ndarray, duration_s: float = 1.0) -> Recording:
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    return Recording(
        samples=samples, duration_s=duration_s, peak=peak, rms=rms, clipped=peak >= 0.99
    )


def silence(n: int = 16000) -> np.ndarray:
    """纯数字静音：全零。"""
    return np.zeros(n, dtype=np.float32)


def flat_noise(n: int = 16000, level: float = 0.002) -> np.ndarray:
    """平坦底噪：峰值和 RMS 同量级（peak/rms ≈ 1.4）。"""
    rng = np.random.default_rng(0)
    return (rng.standard_normal(n) * level).astype(np.float32)


def quiet_speech(n: int = 16000, rms: float = 0.002, peak: float = 0.12) -> np.ndarray:
    """安静环境下的真实说话：RMS 很低，但峰值明显。

    这是关键用例——旧的 RMS 判据会把它当静音丢掉。
    """
    rng = np.random.default_rng(1)
    base = rng.standard_normal(n) * rms
    # 在中间放几个明显的尖峰，模拟语音的能量集中
    idx = np.arange(0, n, n // 8)
    base[idx] = peak
    return base.astype(np.float32)


# --------------------------------------------------------------------------- #
# 峰值判据
# --------------------------------------------------------------------------- #


def test_digital_silence_is_gated():
    r = mk(silence())
    assert r.peak == 0.0
    assert r.is_silent
    assert r.gate_reason(0.01) is not None


def test_flat_noise_is_gated():
    """纯底噪应当被挡住——否则每次按热键都会白跑一次识别。"""
    r = mk(flat_noise())
    assert r.peak < 0.01, f"底噪峰值意外地高：{r.peak}"
    assert r.gate_reason(0.01) is not None


def test_quiet_speech_passes_gate():
    """安静的真语音必须放行——这正是改用峰值判据的原因。

    注意这条用例的 rms 与 flat_noise 相同量级，只有峰值不同：
    如果实现回退到 RMS 判据，这条测试就会失败。
    """
    r = mk(quiet_speech())
    assert r.rms < 0.01, "构造的用例应当 RMS 很低，否则测不出区别"
    assert r.peak > 0.05
    assert r.gate_reason(0.01) is None, "安静的真语音被误判成静音了"


def test_rms_gate_would_have_failed_but_peak_gate_passes():
    """把两种信号的 RMS 摆在一起，证明 RMS 判据分不开它们。"""
    quiet = mk(quiet_speech())
    noise = mk(flat_noise())
    # RMS 同量级 —— 用 RMS 做阈值无法区分
    assert abs(quiet.rms - noise.rms) < 0.002, (
        f"两者 RMS 应当接近：真语音 {quiet.rms:.4f} vs 底噪 {noise.rms:.4f}"
    )
    # 但峰值差一个量级 —— 峰值判据能干净区分
    assert quiet.peak > noise.peak * 5, (
        f"峰值应当差很多：真语音 {quiet.peak:.4f} vs 底噪 {noise.peak:.4f}"
    )


def test_gate_disabled_when_min_peak_zero():
    """min_peak = 0 表示关闭过滤，纯静音也放行。"""
    r = mk(silence())
    assert r.gate_reason(0.0) is None


def test_gate_reason_is_actionable():
    r = mk(silence())
    reason = r.gate_reason(0.01)
    assert reason is not None
    assert "peak" in reason and "0.01" in reason, "原因里要带上实测值和阈值，便于用户调参"


# --------------------------------------------------------------------------- #
# 爆音
# --------------------------------------------------------------------------- #


def test_clipping_detected():
    loud = np.ones(1600, dtype=np.float32)
    r = mk(loud)
    assert r.clipped
    assert "爆音" in r.summary()


def test_normal_level_not_flagged_as_clipped():
    r = mk(quiet_speech())
    assert not r.clipped
    assert "爆音" not in r.summary()


def test_summary_contains_both_metrics():
    r = mk(quiet_speech())
    s = r.summary()
    assert "peak=" in s and "rms=" in s


# --------------------------------------------------------------------------- #
# Recorder 参数校验
# --------------------------------------------------------------------------- #


def test_recorder_rejects_wrong_samplerate():
    with pytest.raises(RecorderError, match="16000"):
        Recorder(samplerate=44100)


def test_recorder_rejects_stereo():
    with pytest.raises(RecorderError, match="单声道"):
        Recorder(channels=2)


def test_recorder_stop_without_start():
    rec = Recorder()
    with pytest.raises(RecorderError, match="没有在录音"):
        rec.stop()


def test_recorder_abort_is_safe_when_idle():
    Recorder().abort()  # 不该抛异常


def test_recorder_initial_state():
    rec = Recorder()
    assert not rec.recording
    assert not rec.timed_out
    assert rec.elapsed_ms == 0.0


def test_sample_rate_constant():
    assert SAMPLE_RATE == 16000
