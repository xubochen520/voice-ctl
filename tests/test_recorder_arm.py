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
# 异步预热（钩子线程专用路径）
# --------------------------------------------------------------------------- #


def _wait(pred, timeout: float = 2.0) -> bool:  # noqa: ANN001
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_arm_async_returns_immediately_and_opens_in_background(fake_sd):  # noqa: ANN001
    """钩子线程调 arm(blocking=False) 必须立刻返回。

    这是硬要求：Windows 给低级键盘钩子 300ms 预算，超了系统会悄悄摘掉钩子，
    表现是「按几次之后热键忽然没反应」而且日志上什么都看不到。而开流实测
    首次 319ms，正好越线。
    """
    rec = Recorder()
    t = time.perf_counter()
    rec.arm(blocking=False)
    call_ms = (time.perf_counter() - t) * 1000
    assert call_ms < 20, f"钩子线程里 arm 花了 {call_ms:.1f}ms，必须几乎为零"
    assert _wait(lambda: rec.armed), "后台线程应当把流开起来"


def test_arm_async_is_idempotent(fake_sd):  # noqa: ANN001
    rec = Recorder()
    for _ in range(5):
        rec.arm(blocking=False)
    assert _wait(lambda: rec.armed)
    time.sleep(0.1)
    assert len(FakeStream.instances) == 1, "重复预热只该开一个流"


def test_arm_async_refreshes_ttl_while_opening(fake_sd):  # noqa: ANN001
    """开流期间又来的预热请求要把定时器续上，不能把流忘了关、也不能提前关。

    ARM_TTL_S 是 3 秒，比"按住 Ctrl+Alt 慢慢挪到空格"短。丢掉后来的请求就等于
    计时器不续：挪到 3.5 秒时流刚好被关掉，于是又变回冷开流——而这一整套存在
    的意义就是别冷开流。
    """
    rec = Recorder()
    rec.arm(ttl=0.2, blocking=False)
    time.sleep(0.05)
    rec.arm(ttl=0.2, blocking=False)  # 模拟用户还在按修饰键，又按了一次
    assert _wait(lambda: rec.armed)
    time.sleep(0.1)
    assert rec.armed, "刚续上的 ttl 不该立刻到期"
    assert _wait(lambda: not rec.armed), "续期之后仍然要能自动关掉"
    assert live().closed


def test_disarm_before_async_arm_finishes_does_not_reopen_mic(fake_sd):  # noqa: ANN001
    """松手比开流快时，晚到的后台预热不能把刚关掉的流又开起来。

    不然 Windows 的麦克风图标会一直亮着，用户会以为被偷听了。
    """
    rec = Recorder()
    rec.arm(blocking=False)
    rec.disarm()  # 修饰键已经松了
    time.sleep(0.2)
    assert not rec.armed, "后台预热应当被作废"
    for s in FakeStream.instances:
        assert s.closed, "开起来的流必须被关掉"


def test_start_backdates_duration_to_the_keypress(fake_sd):  # noqa: ANN001
    """异步路上 start() 比按键晚几十~几百毫秒，时长要按**按下**那一刻算。

    不拨回去的话界面上"已录 x 秒"比实际按住的时间短，排查时会被误导。
    """
    rec = Recorder()
    press_ts = time.perf_counter() - 0.30  # 假装 300ms 前就按下了（冷开流的代价）
    rec.start(at=press_ts)
    time.sleep(0.10)
    out = rec.stop()
    assert out.duration_s == pytest.approx(0.40, abs=0.08), f"实际 {out.duration_s:.3f}s"


def test_capture_elapsed_runs_from_the_keypress(fake_sd):  # noqa: ANN001
    """"已录 x 秒"要在流还没开好时就开始走，否则那 300ms 显示成 0，像没在录。"""
    rec = Recorder()
    assert rec.capture_elapsed_ms == 0.0
    rec.start(at=time.perf_counter() - 0.2)
    assert 180 <= rec.capture_elapsed_ms <= 400, f"实际 {rec.capture_elapsed_ms:.0f}ms"
    rec.stop()
    assert rec.capture_elapsed_ms == 0.0, "录音结束后要归零"


def test_cold_start_keeps_the_audio_that_arrived_while_opening(fake_sd, monkeypatch):  # noqa: ANN001
    """没预热时，开流那 300ms 里进来的音频不能丢——它就是用户的第一个字。

    用 FakeStream.feed 模拟"开流期间回调已经在送音频"。
    """
    original = FakeStream.start

    def slow_start(self) -> None:  # noqa: ANN001
        original(self)
        self.feed(0.7)  # 流一开就有音频进来，而这时 start() 还没返回
        self.feed(0.7)

    monkeypatch.setattr(FakeStream, "start", slow_start)
    rec = Recorder(preroll_ms=400)
    rec.start(at=time.perf_counter())
    out = rec.stop()
    assert out.samples.size >= 2 * CHUNK, "开流期间的音频必须被当成开头交出去"
    assert out.peak == pytest.approx(0.7)


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
