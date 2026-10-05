"""录音：push-to-talk 采集 + 提示音。

用 sounddevice（PortAudio），Windows 上是 WASAPI/MME 后端，纯 wheel 无编译依赖。

采集规格固定 16kHz / 单声道 / float32：SenseVoice 的要求，不做隐式重采样。

**开流很慢，所以要能提前开**：实测（MME，44.1kHz 原生设备）创建并启动一个输入流
要 160ms（首次 280ms），拿到第一帧音频要 196ms（首次 312ms）。如果等到热键按下
才开，开口早一点的人开头那 200ms 就没了——「明天」变成「天」。所以录音器分三态：

    空闲      流没开
    预热      流开着，音频只进一个 400ms 的环形缓冲（pre-roll），不算录音
    录音      start() 时把环形缓冲里的内容当作开头，继续往后录

`start()` 在已预热时是瞬时的（只挪个指针），未预热时退回同步开流，行为和以前一致。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000

DEFAULT_PREROLL_MS = 400
"""预滚缓冲时长。够盖住"手指还在往主键挪、嘴已经开了"的那一小段。"""

ARM_TTL_S = 3.0
"""预热后多久没人真正开始录音就自动关流。

修饰键按住不放（比如 Ctrl+Alt 用在别的组合里）时不能让麦克风一直开着——
Windows 会一直亮着"麦克风正在使用"的图标，用户会以为被偷听了。"""


class RecorderError(Exception):
    """录音相关错误。消息要说清是设备问题还是用法问题。"""


@dataclass
class Recording:
    samples: np.ndarray
    """float32 [-1, 1]，单声道 16kHz。"""

    duration_s: float
    """从按下到松开的**墙钟时长**（不含预滚）。「太短」的误触判断看的是它。"""

    peak: float
    """峰值绝对值。也是**静音判据**——安静的真语音 RMS 低但峰值明显。"""

    rms: float
    """均方根。只用于诊断，不要拿它当静音阈值（见 AudioConfig.min_peak 的说明）。"""

    clipped: bool = False
    """峰值触顶（>= 0.99），说明爆音，识别质量会下降。"""

    preroll_s: float = 0.0
    """开头有多少秒是按键之前的预滚内容（0 = 没预热，开头就是按下的那一刻）。"""

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
    """返回 [(序号, 名称, 默认采样率, 是否默认设备)]。

    列的是**全部**宿主 API 的设备，设置页要用 `usable_input_devices()` 过滤。
    """
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


# 这两个宿主 API 在 Windows 上能按 16kHz 单声道打开任意输入设备（系统会做重采样）。
# 实测同一块 Realtek 麦克风阵列在设备列表里出现 4 次：MME / DirectSound 能开，
# WASAPI 报 "Invalid sample rate"（共享模式不重采样），WDM-KS 报 "Unanticipated host error"。
# 把后两类塞进下拉框，用户选中了要等到第一次按热键才发现录不了音。
USABLE_HOST_APIS = ("MME", "Windows DirectSound")


def usable_input_devices() -> list[tuple[int, str, str, bool]]:
    """设置页用的设备列表：[(序号, 名称, 驱动类型, 是否默认)]。

    只留能按本项目参数打开的宿主 API；同名设备（MME 和 DirectSound 各一份）
    只保留 MME 那份——用户分不清它们，也没必要分。
    """
    try:
        import sounddevice as sd
    except ImportError as e:
        raise RecorderError("sounddevice 没装。执行：.venv\\Scripts\\pip install sounddevice") from e

    try:
        default_in = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_in = -1
    try:
        apis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        apis = []

    seen: dict[str, int] = {}
    out: list[tuple[int, str, str, bool]] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        try:
            api = str(apis[dev["hostapi"]]["name"])
        except Exception:  # noqa: BLE001
            api = "?"
        if apis and api not in USABLE_HOST_APIS:
            continue
        name = str(dev.get("name", "?"))
        key = name.strip().lower()
        if key in seen:
            # 已经有同名的了：如果当前这份是默认设备，让默认标记落在保留的那份上
            if idx == default_in:
                j = seen[key]
                out[j] = (out[j][0], out[j][1], out[j][2], True)
            continue
        seen[key] = len(out)
        short = "MME" if api == "MME" else "DirectSound"
        out.append((idx, name, short, idx == default_in))
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

    **开流不能等按下才做。** 冷开流实测 319ms（见 `_open_stream` 的说明），
    这段 speech 就没了：「明天八点」听成「天八点」。所以有两条路一起走：

      * `arm()` 预热（修饰键按下时调），主键按下时 `start()` 就只剩挪指针；
      * `start()` 仍然保留预滚缓冲——哪怕没预热，开流这 300ms 里进来的音频
        也在环形缓冲里，会被当成开头交出去，一个字都不丢。
    """

    def __init__(
        self,
        *,
        samplerate: int = SAMPLE_RATE,
        channels: int = 1,
        device: int | str | None = None,
        max_duration_ms: int = 15000,
        preroll_ms: int = DEFAULT_PREROLL_MS,
    ) -> None:
        if samplerate != SAMPLE_RATE:
            raise RecorderError(f"采样率必须是 {SAMPLE_RATE}，实际 {samplerate}")
        if channels != 1:
            raise RecorderError(f"必须是单声道，实际 {channels}")
        self.samplerate = samplerate
        self.channels = channels
        self.device = device or None
        self.max_duration_ms = max_duration_ms
        self.preroll_ms = max(0, int(preroll_ms))

        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._ring: deque[np.ndarray] = deque()
        self._ring_frames = 0
        self._preroll_frames = self.samplerate * self.preroll_ms // 1000
        self._taken_frames = 0
        """本次录音开头取自预滚缓冲的帧数。"""

        self._lock = threading.Lock()
        """保护 _chunks / _ring / _peak / _recording（音频回调线程也会碰它们）。"""
        self._open_lock = threading.RLock()
        """保护流的开/关。预热在另一条线程里开流，start() 得等它开完。"""

        self._recording = False
        self._armed = False
        self._arming = False
        """后台预热正在进行中（还没把流开起来）。disarm 靠它把晚到的预热作废。"""
        self._permanent = False
        self._arm_timer: threading.Timer | None = None
        self._arm_thread: threading.Thread | None = None
        """异步预热的线程。开流要几百毫秒，钩子线程等不起。"""
        self._arm_ttl: float | None = ARM_TTL_S
        """最近一次预热请求的 ttl。开流期间又来的请求会刷新它。"""
        self._start_ts = 0.0
        self._press_ts = 0.0
        """按下热键的墙钟时刻（start(at=...) 传进来）。显示与时长的基准。"""
        self._peak = 0.0
        self.level = 0.0
        """最近一小段音频的电平（0-1，带衰减），悬浮提示画电平条用。无锁读写：
        一个 float 的赋值是原子的，读到旧值也无所谓。"""

    # -- 状态 ------------------------------------------------------------- #

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def armed(self) -> bool:
        """流是否已经开着（预热中或录音中）。"""
        return self._stream is not None

    @property
    def elapsed_ms(self) -> float:
        """流开好之后过了多久。界面上要显示"这次按住多久"请用 capture_elapsed_ms。"""
        return (time.perf_counter() - self._start_ts) * 1000 if self._recording else 0.0

    @property
    def capture_elapsed_ms(self) -> float:
        """这次按住已经过去多久（哪怕流还没开好也照样在走）。

        界面上的"已录 0.8 秒"用它，不用 elapsed_ms：后者要等 start() 真的
        跑完才有值，冷开流那 300ms 会显示成 0，看起来像没在录。
        """
        if not self._recording or not self._press_ts:
            return 0.0
        return (time.perf_counter() - self._press_ts) * 1000

    @property
    def timed_out(self) -> bool:
        """录音是否已超过 max_duration_ms（调用方据此强制收尾）。"""
        return self._recording and self.elapsed_ms >= self.max_duration_ms

    # -- 流的开关 --------------------------------------------------------- #

    def _callback(self, indata, _frames, _time_info, status) -> None:  # noqa: ANN001
        if status:  # 溢出/欠载等，只记录不中断
            pass
        chunk = indata.copy().reshape(-1)
        p = float(np.max(np.abs(chunk))) if chunk.size else 0.0
        self.level = max(p, self.level * 0.85)
        with self._lock:
            if self._recording:
                self._chunks.append(chunk)
                if p > self._peak:
                    self._peak = p
                return
            # 没在录：只留最近 preroll 这么长的内容
            self._ring.append(chunk)
            self._ring_frames += chunk.size
            while len(self._ring) > 1 and self._ring_frames - self._ring[0].size >= self._preroll_frames:
                self._ring_frames -= self._ring.popleft().size

    def _open_stream(self) -> None:
        """开流。调用方持有 _open_lock。已经开着就什么都不做。"""
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RecorderError(
                "sounddevice 没装。执行：.venv\\Scripts\\pip install sounddevice"
            ) from e
        # 先清环形缓冲再开流：反过来的话，流一启动就进来的头几帧会被清掉
        with self._lock:
            self._ring.clear()
            self._ring_frames = 0
        try:
            stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            stream.start()
        except Exception as e:  # noqa: BLE001
            raise RecorderError(
                f"打不开录音设备（device={self.device!r}）：{e}。"
                "用 `voice-ctl devices` 看可用设备，在配置里指定 device 序号。"
            ) from e
        self._stream = stream

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self.level = 0.0

    def _cancel_arm_timer(self) -> None:
        timer, self._arm_timer = self._arm_timer, None
        if timer is not None:
            timer.cancel()

    def _arm_async(self, ttl: float | None) -> None:
        """异步预热：立刻返回，开流在后台线程里做。

        为什么必须异步：`on_arm` 是 pynput 的**钩子回调**，Windows 给低级键盘
        钩子的预算是 LowLevelHooksTimeout（默认 300ms），超了系统会悄悄摘钩子
        ——症状是「按几次之后热键忽然没反应」，日志上什么都看不到。而开流实测
        首次 319ms，正好越线。放后台线程后钩子回调只剩几微秒。

        开流期间又来的预热请求不丢弃，只把 ttl 记下来：多键热键每次按下修饰键
        都会来一次，而 ARM_TTL_S（3 秒）比"按住 Ctrl+Alt 慢慢挪到空格"短——
        丢掉后来的请求就等于**计时器不续**，用户挪到 3.5 秒时流刚好被关掉，
        又变回冷开流。
        """
        self._arm_ttl = ttl
        if self._arm_thread is not None and self._arm_thread.is_alive():
            return
        self._arming = True
        thread = threading.Thread(
            target=self._arm_worker, name="voice-ctl-preheat", daemon=True
        )
        self._arm_thread = thread
        thread.start()

    def _arm_worker(self) -> None:
        try:
            self._arm_gated()
        except Exception:  # noqa: BLE001 - 预热失败只是没预热上，录音时还会再开一次
            pass

    def _arm_gated(self) -> None:
        """开完流再确认这次预热还没作废，把定时器按最新的 ttl 续上。

        后台预热天生会晚到：用户可能在开流的 300ms 里已经松手了（disarm 先跑），
        或者已经按下了主键并录完（stop 把流关了）。没有这个校验的话，后台线程
        会把刚刚关掉的流又开起来——麦克风图标一直亮着，用户会以为被偷听。
        """
        with self._open_lock:
            if not self._arming:
                return
            self._open_stream()
            if not self._arming:
                self._close_stream()
                return
            self._armed = True
            self._cancel_arm_timer()
            ttl = self._arm_ttl
            if ttl is None:
                self._permanent = True
                return
            if self._permanent:
                return  # 已经是常开了，别被一次带 ttl 的预热降级
            timer = threading.Timer(ttl, self._expire_arm)
            timer.daemon = True
            self._arm_timer = timer
            timer.start()

    # -- 预热 ------------------------------------------------------------- #

    def arm(self, ttl: float | None = ARM_TTL_S, *, blocking: bool = True) -> None:
        """提前把流开好。幂等。

        `ttl` 秒内没人 start() 就自动关流；`ttl=None` 表示常开（"低延迟模式"，
        代价是 Windows 一直显示麦克风图标）。

        `blocking=False` 时开流丢给后台线程（钩子线程必须用这个）。
        """
        if not blocking:
            self._arm_async(ttl)
            return
        with self._open_lock:
            self._arming = True
            self._open_stream()
            self._armed = True
            self._cancel_arm_timer()
            if ttl is None:
                self._permanent = True
                return
            if self._permanent:
                return  # 已经是常开了，别被一次带 ttl 的预热降级
            timer = threading.Timer(ttl, self._expire_arm)
            timer.daemon = True
            self._arm_timer = timer
            timer.start()

    def disarm(self) -> None:
        """取消预热并关流。正在录音时什么都不做（录音由 stop()/abort() 收尾）。"""
        with self._open_lock:
            self._arming = False
            if self._recording:
                return
            self._cancel_arm_timer()
            self._armed = False
            self._permanent = False
            self._close_stream()

    def _expire_arm(self) -> None:
        if not self._recording:
            self.disarm()

    def close(self) -> None:
        """彻底关掉：包括常开模式。引擎停止监听时用。"""
        with self._open_lock:
            self._recording = False
            self._arming = False
            self._cancel_arm_timer()
            self._armed = False
            self._permanent = False
            self._close_stream()
            with self._lock:
                self._chunks = []
                self._ring.clear()
                self._ring_frames = 0

    # -- 生命周期 --------------------------------------------------------- #

    def start(self, at: float | None = None) -> None:
        """开始录音。

        `at` 是**按下热键的墙钟时刻**（time.perf_counter()）。异步路上 start()
        会比按键晚几十到几百毫秒才跑到，不把基准拨回去的话：
          * `elapsed_ms`（界面上的"已录 x.x 秒"）会少一截；
          * 用户按住的时间和报出来的录音时长对不上，排查时会被误导。

        刻意的行为：**不清空预滚缓冲**，把开流这段时间进来的音频当成开头交出去。
        冷开流要 319ms，用户开口比按键早一点（等得急了）或者开流慢，这段就是
        用户的第一个字。预滚上限 400ms，最多混进 0.4 秒按键之前的环境音，
        对识别无影响。
        """
        if self._recording:
            raise RecorderError("已经在录音了，不能重复开始")
        press_ts = at if at is not None else time.perf_counter()
        with self._open_lock:
            self._cancel_arm_timer()
            self._arming = False
            self._open_stream()  # 已预热就是空操作；没预热就在这里同步开
            with self._lock:
                pre = list(self._ring)
                self._ring.clear()
                self._ring_frames = 0
                self._chunks = pre
                self._taken_frames = sum(c.size for c in pre)
                self._peak = max((float(np.max(np.abs(c))) for c in pre if c.size), default=0.0)
                self._start_ts = time.perf_counter()
                self._press_ts = press_ts
                self._recording = True

    def stop(self) -> Recording:
        if not self._recording:
            raise RecorderError("没有在录音，不能停止")

        # 时长按**按下热键**那一刻算，不按流开好的那一刻——否则异步开流/冷开流
        # 的那几百毫秒会被算丢，界面显示的"已录 x 秒"比实际按住的时间短。
        now = time.perf_counter()
        duration = now - (self._press_ts or self._start_ts)
        with self._lock:
            self._recording = False

        with self._open_lock:
            if not self._permanent:
                self._armed = False
                self._close_stream()

        with self._lock:
            chunks, self._chunks = self._chunks, []
            peak = self._peak
            taken = self._taken_frames
            self._taken_frames = 0
        if chunks:
            samples = np.concatenate(chunks).reshape(-1).astype(np.float32)
        else:
            samples = np.zeros(0, dtype=np.float32)

        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        return Recording(
            samples=samples,
            duration_s=duration,
            peak=peak,
            rms=rms,
            clipped=peak >= 0.99,
            preroll_s=taken / self.samplerate,
        )

    def abort(self) -> None:
        """放弃当前录音，不返回数据。用于按键卡死等异常路径。"""
        if not self._recording:
            return
        with self._lock:
            self._recording = False
        with self._open_lock:
            self._arming = False  # 顺带作废可能还在路上的后台预热
            if not self._permanent:
                self._armed = False
                self._close_stream()
        with self._lock:
            self._chunks = []
            self._taken_frames = 0
