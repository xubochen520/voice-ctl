"""录音：push-to-talk 采集 + 提示音。

用 sounddevice（PortAudio），Windows 上是 WASAPI/MME 后端，纯 wheel 无编译依赖。

采集规格固定 16kHz / 单声道 / float32：SenseVoice 的要求，不做隐式重采样。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000


class RecorderError(Exception):
    """录音相关错误。消息要说清是设备问题还是用法问题。"""


@dataclass
class Recording:
    samples: np.ndarray
    """float32 [-1, 1]，单声道 16kHz。"""

    duration_s: float
    peak: float
    """峰值绝对值。也是**静音判据**——安静的真语音 RMS 低但峰值明显。"""

    rms: float
    """均方根。只用于诊断，不要拿它当静音阈值（见 AudioConfig.min_peak 的说明）。"""

    clipped: bool = False
    """峰值触顶（>= 0.99），说明爆音，识别质量会下降。"""

    @property
    def is_silent(self) -> bool:
        """整段几乎没有任何信号。"""
        return self.peak <= 0.0

    def gate_reason(self, min_peak: float) -> str | None:
        """返回被过滤的原因，None 表示放行。

        判据只有峰值一条。刻意不看 RMS：实测安静环境下真实说话的
        RMS 可以低到 0.002（与底噪同级），用 RMS 会误杀。
        """
        if min_peak > 0 and self.peak < min_peak:
            return f"静音（peak {self.peak:.4f} < {min_peak}）"
        return None

    def summary(self) -> str:
        extra = "  ⚠爆音" if self.clipped else ""
        return (
            f"{self.duration_s:.2f}s  peak={self.peak:.3f}  rms={self.rms:.4f}{extra}"
        )


def list_input_devices() -> list[tuple[int, str, int, bool]]:
    """返回 [(序号, 名称, 默认采样率, 是否默认设备)]。"""
    try:
        import sounddevice as sd
    except ImportError as e:
        raise RecorderError("sounddevice 没装。执行：.venv\\Scripts\\pip install sounddevice") from e

    try:
        default_in = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_in = -1

    out: list[tuple[int, str, int, bool]] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        out.append(
            (idx, str(dev.get("name", "?")), int(dev.get("default_samplerate", 0)), idx == default_in)
        )
    return out


def play_beep(freq_hz: int, ms: int = 70, volume: float = 0.25) -> None:
    """非阻塞提示音。失败就静默跳过——提示音不该让主流程崩掉。"""
    try:
        import sounddevice as sd

        n = max(1, int(SAMPLE_RATE * ms / 1000))
        t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
        wave = (np.sin(2 * np.pi * freq_hz * t) * volume).astype(np.float32)
        # 两端 5ms 淡入淡出，避免爆音
        ramp = min(n // 4, int(SAMPLE_RATE * 0.005))
        if ramp > 1:
            env = np.ones(n, dtype=np.float32)
            env[:ramp] = np.linspace(0, 1, ramp, dtype=np.float32)
            env[-ramp:] = np.linspace(1, 0, ramp, dtype=np.float32)
            wave *= env
        sd.play(wave, SAMPLE_RATE)
    except Exception:  # noqa: BLE001 提示音失败不影响功能
        pass


class Recorder:
    """按住开始、松开停止的录音器。

    同一时刻只允许一次录音；重复 start() 会抛错而不是悄悄丢掉前一段。
    """

    def __init__(
        self,
        *,
        samplerate: int = SAMPLE_RATE,
        channels: int = 1,
        device: int | str | None = None,
        max_duration_ms: int = 15000,
    ) -> None:
        if samplerate != SAMPLE_RATE:
            raise RecorderError(f"采样率必须是 {SAMPLE_RATE}，实际 {samplerate}")
        if channels != 1:
            raise RecorderError(f"必须是单声道，实际 {channels}")
        self.samplerate = samplerate
        self.channels = channels
        self.device = device or None
        self.max_duration_ms = max_duration_ms

        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._start_ts = 0.0
        self._peak = 0.0

    # -- 状态 ------------------------------------------------------------- #

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start_ts) * 1000 if self._recording else 0.0

    @property
    def timed_out(self) -> bool:
        """录音是否已超过 max_duration_ms（调用方据此强制收尾）。"""
        return self._recording and self.elapsed_ms >= self.max_duration_ms

    # -- 生命周期 --------------------------------------------------------- #

    def start(self) -> None:
        if self._recording:
            raise RecorderError("已经在录音了，不能重复开始")
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RecorderError(
                "sounddevice 没装。执行：.venv\\Scripts\\pip install sounddevice"
            ) from e

        with self._lock:
            self._chunks = []
            self._peak = 0.0

        def callback(indata, _frames, _time_info, status) -> None:  # noqa: ANN001
            if status:  # 溢出/欠载等，只记录不中断
                pass
            with self._lock:
                self._chunks.append(indata.copy())
                p = float(np.max(np.abs(indata))) if indata.size else 0.0
                if p > self._peak:
                    self._peak = p

        try:
            self._stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="float32",
                device=self.device,
                callback=callback,
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            self._stream = None
            raise RecorderError(
                f"打不开录音设备（device={self.device!r}）：{e}。"
                "用 `voice-ctl devices` 看可用设备，在配置里指定 device 序号。"
            ) from e

        self._start_ts = time.perf_counter()
        self._recording = True

    def stop(self) -> Recording:
        if not self._recording:
            raise RecorderError("没有在录音，不能停止")

        duration = time.perf_counter() - self._start_ts
        self._recording = False

        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        with self._lock:
            chunks, self._chunks = self._chunks, []
        if chunks:
            samples = np.concatenate(chunks).reshape(-1).astype(np.float32)
        else:
            samples = np.zeros(0, dtype=np.float32)

        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        return Recording(
            samples=samples,
            duration_s=duration,
            peak=self._peak,
            rms=rms,
            clipped=self._peak >= 0.99,
        )

    def abort(self) -> None:
        """放弃当前录音，不返回数据。用于按键卡死等异常路径。"""
        if not self._recording:
            return
        self._recording = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._chunks = []
