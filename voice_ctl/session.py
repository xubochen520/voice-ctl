"""会话控制：把「按下 → 录音 → 松开 → 识别执行」的状态机抽出来。

为什么单独成类：这段逻辑有多线程来源（pynput 监听线程、主循环的超时检查），
而并发缺陷没法靠手工点击验证。抽出来后就能用假 Recorder 精确测：
重复按下、重复松开、超时与松开同时到达、start 失败等。

放在 pipeline 之外，是因为它管的是**会话生命周期**，不是单次判断。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class RecordingLike(Protocol):
    """Recorder 的最小子集——测试用假对象实现它即可。"""

    def start(self, at: float | None = ...) -> None: ...
    def stop(self): ...  # noqa: ANN201 - 返回 Recording
    def abort(self) -> None: ...


class RecorderErrorLike(Exception):
    """占位，便于类型标注；实际捕获由调用方传进来的异常类型决定。"""


@dataclass
class SessionStats:
    pressed: int = 0
    finished: int = 0
    too_short: int = 0
    gated: int = 0
    start_failed: int = 0
    timeouts: int = 0
    processed: int = 0

    def summary(self) -> str:
        return (
            f"按下 {self.pressed}  完成 {self.finished}  太短 {self.too_short}  "
            f"静音 {self.gated}  启动失败 {self.start_failed}  "
            f"超时 {self.timeouts}  已处理 {self.processed}"
        )


@dataclass
class SessionController:
    """把热键事件翻译成录音会话。

    不变量：
      * 任何时刻最多一次录音在跑（repeat press 被忽略）
      * 一次录音只会被收尾一次（release 与 timeout 竞争时只有一个生效）
      * start 失败会把状态复位，不会卡在"以为在录"

    **press()/release() 跑在键盘钩子线程里，必须立刻返回。** Windows 给低级
    键盘钩子的预算是 LowLevelHooksTimeout（默认 300ms），超了系统会悄悄摘掉
    钩子——症状是"按几次之后热键忽然没反应了"，日志上还什么都看不到。而开流
    实测冷启 319ms，正好越线。所以开流这一步（`_begin_capture`）在
    `capture_async=True` 时丢给后台线程；钩子线程只做状态翻转。
    """

    recorder: RecordingLike
    timer: object
    """需要 arm() / disarm()。传 HotkeyTimer 或测试替身。"""

    on_recording_start: Callable[[], None] | None = None
    on_recording_end: Callable[[], None] | None = None
    on_recording_ready: Callable[[object], None] | None = None
    """拿到有效录音时调用（参数是 Recording）。"""

    on_too_short: Callable[[float], None] | None = None
    on_gated: Callable[[str], None] | None = None
    on_error: Callable[[str], None] | None = None

    min_duration_ms: int = 200
    min_peak: float = 0.01

    recorder_error: type[BaseException] = Exception
    """Recorder 抛的异常类型，用于精确捕获。"""

    capture_async: bool = False
    """True = 开流丢后台线程（生产环境用，钩子线程等不起 300ms）。
    False = 同步（测试默认走这条，行为与旧版完全一致）。"""

    stats: SessionStats = field(default_factory=SessionStats)

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _recording: bool = field(default=False, init=False, repr=False)
    _capturing: bool = field(default=False, init=False, repr=False)
    """后台线程是否正在真的录（recorder.start() 已返回）。"""

    _stopped: bool = field(default=False, init=False, repr=False)
    """松开已经发生了、但当时流还没开好。开好后要立刻收尾。"""

    _finalized: bool = field(default=False, init=False, repr=False)
    """这次会话已经交付过（或已被放弃）。防止收尾跑第二遍。"""

    _press_ts: float = field(default=0.0, init=False, repr=False)
    """按下热键的时刻。混进录音器作时长基准。"""

    # -- 状态 ------------------------------------------------------------- #

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    # -- 事件 ------------------------------------------------------------- #

    def press(self) -> None:
        with self._lock:
            if self._recording:
                return  # 重复按下 / 键盘自动重复
            self._recording = True  # 先占位，避免两个线程都通过检查
            self._stopped = False
            self._finalized = False
            self._press_ts = time.perf_counter()
            self.stats.pressed += 1
        if self.capture_async:
            self._spawn(self._press_worker)
            return
        self._press_worker()

    def _press_worker(self) -> None:
        """开流并进入录音态。同步模式下它就是 press() 本身。

        "开流中"这段是**唯一**一个既有并发又想独占的状态：松手（release）、
        超时（timeout）、开流完成（本函数）三边都可能想收尾。规矩定死：

          * 进入录音态（`_capturing=True`）与"是否已经松手"必须在**同一把锁**里
            一次读完。分两次读会丢更新——实测症状就是录音被静默丢掉：release
            先把 _recording 置 False 并把 capturing 读成 False 提前返回，本函数
            随后才把 capturing 置 True，于是没有任何一方去收尾，那段录音凭空消失。
          * 谁把 capturing 从 False 翻成 True，谁就负责那次收尾。
        """
        at = self._press_ts
        try:
            self._begin_capture(at)
        except Exception as e:  # noqa: BLE001 - 具体类型由调用方用 recorder_error 收窄
            with self._lock:
                self._recording = False
                self._stopped = False
            self.stats.start_failed += 1
            if self.on_error:
                self.on_error(f"打开录音设备失败：{e}")
            return
        with self._lock:
            self._capturing = True
            stopped_early = self._stopped
        self.timer.arm()  # type: ignore[attr-defined]
        if self.on_recording_start:
            self.on_recording_start()
        if stopped_early:
            # 用户手快：流还没开好就松开了。录音本身没丢（开头在预滚缓冲里），
            # 照常交付即可——但要在这里交付，不能等 release，那次已经过去了。
            self._finalize()

    def release(self) -> None:
        with self._lock:
            self._stopped = True
        self.finish()

    def timeout(self) -> None:
        """超时守护触发。只有在录音中才有效。"""
        if not self.recording:
            return
        self.stats.timeouts += 1
        self.finish(forced=True)

    def finish(self, *, forced: bool = False) -> None:
        """收尾。松手、超时、以及"开流太慢"三条路都走这里。

        只有真在录的那条路能收尾：开流还没跑完时（capturing=False）这里直接
        返回，由 `_press_worker` 开完流之后自己收——那个时机由它独占，别的线程
        插进来只会撞车（见 `_press_worker` 里的说明）。
        """
        with self._lock:
            if not self._recording:
                return  # 另一个路径已经收尾
            self._recording = False
            self._stopped = True
            capturing = self._capturing
            self._capturing = False
        self.timer.disarm()  # type: ignore[attr-defined]
        if not capturing:
            return  # 开流还没跑完；它跑完时会看到 _stopped 自己收尾
        self._finalize()

    def _finalize(self) -> None:
        """停采 + 交付。**一次会话只会真的跑一次**（`_finalized` 当闸门）。

        闸门不是洁癖：`_end_capture()` 第二次会抛"没有在录音，不能停止"，那条
        异常会被报成"停止录音失败"，看起来像功能坏了。
        """
        with self._lock:
            if self._finalized:
                return
            self._finalized = True

        try:
            rec = self._end_capture()
        except Exception as e:  # noqa: BLE001
            if self.on_error:
                self.on_error(f"停止录音失败：{e}")
            return

        if self.on_recording_end:
            self.on_recording_end()

        duration_ms = float(getattr(rec, "duration_s", 0.0)) * 1000
        if duration_ms < self.min_duration_ms:
            self.stats.too_short += 1
            if self.on_too_short:
                self.on_too_short(duration_ms)
            return

        gate = getattr(rec, "gate_reason", None)
        reason = gate(self.min_peak) if callable(gate) else None
        if reason:
            self.stats.gated += 1
            if self.on_gated:
                self.on_gated(reason)
            return

        self.stats.finished += 1
        # 超时收尾拿到的也是有内容的录音，与正常松手一视同仁
        if self.on_recording_ready:
            self.stats.processed += 1
            self.on_recording_ready(rec)

    def abort(self) -> None:
        """放弃当前会话（退出时用）。"""
        with self._lock:
            was = self._recording or self._capturing
            self._recording = False
            self._stopped = True
            self._capturing = False
            self._finalized = True  # 放弃的会话绝不能再被交付
        self.timer.disarm()  # type: ignore[attr-defined]
        if was:
            try:
                self._discard_capture()
            except Exception:  # noqa: BLE001
                pass

    # -- 录音器的三个动作（测试可以整组换掉，不用真起线程/真开设备） -------- #

    def _begin_capture(self, at: float) -> None:
        self.recorder.start(at)

    def _end_capture(self):  # noqa: ANN202 - 返回 Recording
        return self.recorder.stop()

    def _discard_capture(self) -> None:
        self.recorder.abort()

    @staticmethod
    def _spawn(target: Callable[[], None]) -> None:
        threading.Thread(target=target, name="voice-ctl-capture", daemon=True).start()
