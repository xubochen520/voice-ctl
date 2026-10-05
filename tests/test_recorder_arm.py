"""录音器的预热 / 预滚 / 电平。

用假的 sounddevice：真麦克风测不了这些（要精确控制「按键之前到了哪些音频」）。
"""

from __future__ import annotations

import sys
import time
import types

import numpy as np
import pytest

from voice_ctl import recorder as R
from voice_ctl.recorder import Recorder, RecorderError

CHUNK = 1600  # 100ms @16kHz


class FakeStream:
    instances: list["FakeStream"] = []

    def __init__(self, **kw) -> None:  # noqa: ANN003
        self.kw = kw
        self.callback = kw["callback"]
        self.started = False
        self.closed = False
        FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def feed(self, value: float, frames: int = CHUNK) -> np.ndarray:
        data = np.full((frames, 1), value, dtype=np.float32)
        self.callback(data, frames, None, None)
        return data.reshape(-1)


@pytest.fixture()
def fake_sd(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    FakeStream.instances = []
    mod = types.ModuleType("sounddevice")
    mod.InputStream = FakeStream  # type: ignore[attr-defined]
    mod.default = types.SimpleNamespace(device=(1, 2))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    return mod


def live() -> FakeStream:
    return FakeStream.instances[-1]


# --------------------------------------------------------------------------- #
# 基本流程（没预热：和改动前行为一致）
# --------------------------------------------------------------------------- #


def test_cold_start_opens_stream_and_records(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.start()
    assert rec.recording and len(FakeStream.instances) == 1
    a = live().feed(0.1)
    b = live().feed(0.3)
    out = rec.stop()
    assert np.array_equal(out.samples, np.concatenate([a, b]))
    assert out.peak == pytest.approx(0.3)
    assert out.preroll_s == 0.0
    assert live().closed, "没预热时 stop 之后流必须关掉"
    assert not rec.armed


def test_cold_start_failure_is_actionable(fake_sd, monkeypatch):  # noqa: ANN001
    def boom(**_kw):  # noqa: ANN003, ANN202
        raise OSError("device busy")

    monkeypatch.setattr(fake_sd, "InputStream", boom)
    with pytest.raises(RecorderError, match="打不开录音设备"):
        Recorder().start()


def test_double_start_is_rejected(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.start()
    with pytest.raises(RecorderError, match="已经在录音"):
        rec.start()
    rec.abort()


# --------------------------------------------------------------------------- #
# 预热 + 预滚
# --------------------------------------------------------------------------- #


def test_armed_audio_before_start_becomes_the_beginning(fake_sd):  # noqa: ANN001
    """核心：按键之前到的音频（手指还在往主键挪、嘴已经开了）不能丢。"""
    rec = Recorder(preroll_ms=400)
    rec.arm()
    s = live()
    early = [s.feed(0.2), s.feed(0.4)]
    rec.start()
    assert len(FakeStream.instances) == 1, "已预热时 start 不该再开一个流"
    late = s.feed(0.6)
    out = rec.stop()
    assert np.array_equal(out.samples, np.concatenate([*early, late]))
    assert out.preroll_s == pytest.approx(0.2, abs=1e-6)
    assert out.peak == pytest.approx(0.6)
    assert out.duration_s < 0.5, "duration 是按键时长，不含预滚"


def test_preroll_peak_counts_toward_gate(fake_sd):  # noqa: ANN001
    """开头抢出来的那半个字是整段里唯一的响动时，静音门也得放行它。"""
    rec = Recorder()
    rec.arm()
    s = live()
    s.feed(0.5)
    rec.start()
    s.feed(0.0)
    out = rec.stop()
    assert out.peak == pytest.approx(0.5)
    assert out.gate_reason(0.01) is None


def test_preroll_is_bounded(fake_sd):  # noqa: ANN001
    rec = Recorder(preroll_ms=400)
    rec.arm()
    s = live()
    for i in range(10):  # 1 秒的预热期音频
        s.feed(0.1 * (i + 1) / 10)
    rec.start()
    out = rec.stop()
    assert 0.39 <= out.preroll_s <= 0.51, f"预滚应约 400ms，实际 {out.preroll_s:.3f}s"
    # 保留的是**最近**的，不是最早的
    assert out.samples[-1] == pytest.approx(0.1)
    assert out.samples[0] > 0.05


def test_zero_preroll_keeps_nothing_meaningful(fake_sd):  # noqa: ANN001
    rec = Recorder(preroll_ms=0)
    rec.arm()
    s = live()
    for _ in range(5):
        s.feed(0.5)
    rec.start()
    out = rec.stop()
    assert out.preroll_s <= CHUNK / 16000 + 1e-9, "preroll_ms=0 最多只会带出一个回调块"


def test_arm_is_idempotent(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.arm()
    rec.arm()
    rec.arm()
    assert len(FakeStream.instances) == 1


def test_stop_closes_stream_even_after_arm(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.arm()
    rec.start()
    rec.stop()
    assert live().closed
    assert not rec.armed


# --------------------------------------------------------------------------- #
# 预热的退出路径
# --------------------------------------------------------------------------- #


def test_disarm_closes_stream_when_idle(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.arm()
    rec.disarm()
    assert live().closed and not rec.armed


def test_disarm_while_recording_does_nothing(fake_sd):  # noqa: ANN001
    """录音由 stop()/abort() 收尾；disarm 误关流会把录到一半的音频截断。"""
    rec = Recorder()
    rec.arm()
    rec.start()
    rec.disarm()
    assert rec.recording and not live().closed
    rec.stop()


def test_arm_expires_if_nobody_starts(fake_sd):  # noqa: ANN001
    """修饰键按住不放时，麦克风不能一直开着（Windows 会一直亮麦克风图标）。"""
    rec = Recorder()
    rec.arm(ttl=0.05)
    deadline = time.perf_counter() + 2
    while rec.armed and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert not rec.armed and live().closed


def test_start_cancels_the_expiry_timer(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.arm(ttl=0.1)
    rec.start()
    time.sleep(0.25)
    assert rec.recording and not live().closed, "已经开始录音，到期的预热定时器不能把流关了"
    rec.stop()


def test_permanent_arm_survives_stop_and_close_kills_it(fake_sd):  # noqa: ANN001
    """低延迟模式（常开）：录完不关流，直到 close()。"""
    rec = Recorder()
    rec.arm(ttl=None)
    rec.start()
    rec.stop()
    assert rec.armed and not live().closed
    rec.arm(ttl=0.01)  # 一次带 ttl 的预热不能把常开降级
    time.sleep(0.1)
    assert rec.armed
    rec.close()
    assert not rec.armed and live().closed


def test_abort_discards_audio_and_closes(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.arm()
    s = live()
    s.feed(0.5)
    rec.start()
    s.feed(0.5)
    rec.abort()
    assert not rec.recording and live().closed
    rec.start()  # 上一次的残留不能带进下一次
    out = rec.stop()
    assert out.samples.size == 0 or out.peak == 0.0


# --------------------------------------------------------------------------- #
# 电平
# --------------------------------------------------------------------------- #


def test_level_tracks_peak_and_decays(fake_sd):  # noqa: ANN001
    rec = Recorder()
    rec.arm()
    s = live()
    s.feed(0.8)
    assert rec.level == pytest.approx(0.8)
    s.feed(0.0)
    assert 0.5 < rec.level < 0.8, "电平应当带衰减，不是瞬间归零（否则电平条一抖一抖看不清）"
    for _ in range(30):
        s.feed(0.0)
    assert rec.level < 0.01
    rec.disarm()
    assert rec.level == 0.0, "关流后电平清零，悬浮提示才不会停在半空"


# --------------------------------------------------------------------------- #
# 设备列表
# --------------------------------------------------------------------------- #


def _devices(monkeypatch: pytest.MonkeyPatch, devs: list[tuple[str, int, int]]) -> None:
    """devs: [(名称, hostapi 序号, 输入声道数)]；hostapi 0=MME 1=DirectSound 2=WASAPI 3=WDM-KS"""
    mod = types.ModuleType("sounddevice")
    mod.default = types.SimpleNamespace(device=(1, 0))  # type: ignore[attr-defined]
    mod.query_hostapis = lambda: [  # type: ignore[attr-defined]
        {"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"},
    ]
    mod.query_devices = lambda: [  # type: ignore[attr-defined]
        {"name": n, "hostapi": h, "max_input_channels": c, "default_samplerate": 44100.0}
        for n, h, c in devs
    ]
    monkeypatch.setitem(sys.modules, "sounddevice", mod)


def test_usable_devices_drops_drivers_that_cannot_open_at_16k(monkeypatch):  # noqa: ANN001
    """实测：WASAPI 报 Invalid sample rate，WDM-KS 报 Unanticipated host error。"""
    _devices(monkeypatch, [
        ("Microsoft 声音映射器 - Input", 0, 2),
        ("麦克风阵列 (Realtek)", 0, 2),
        ("麦克风阵列 (Realtek)", 1, 2),
        ("麦克风阵列 (Realtek)", 2, 2),
        ("麦克风 (Realtek HD Audio Mic input)", 3, 2),
        ("电脑扬声器", 0, 0),  # 输出设备
    ])
    got = R.usable_input_devices()
    names = [(i, n, api) for i, n, api, _ in got]
    assert names == [(0, "Microsoft 声音映射器 - Input", "MME"), (1, "麦克风阵列 (Realtek)", "MME")]


def test_usable_devices_dedupes_same_name_and_keeps_default_flag(monkeypatch):  # noqa: ANN001
    _devices(monkeypatch, [("麦克风阵列", 0, 2), ("麦克风阵列", 1, 2)])
    mod = sys.modules["sounddevice"]
    mod.default = types.SimpleNamespace(device=(1, 0))  # 默认是 DirectSound 那一份
    got = R.usable_input_devices()
    assert len(got) == 1
    assert got[0][3] is True, "默认标记要落在保留下来的那份上，否则列表里没有「默认」了"


def test_usable_devices_without_hostapi_info_keeps_everything(monkeypatch):  # noqa: ANN001
    """查不到宿主 API 信息时宁可多列，也不能把用户唯一的麦克风过滤没了。"""
    mod = types.ModuleType("sounddevice")
    mod.default = types.SimpleNamespace(device=(0, 0))  # type: ignore[attr-defined]
    mod.query_hostapis = lambda: (_ for _ in ()).throw(RuntimeError("no"))  # type: ignore[attr-defined]
    mod.query_devices = lambda: [{"name": "X", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 16000.0}]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    assert [d[1] for d in R.usable_input_devices()] == ["X"]
