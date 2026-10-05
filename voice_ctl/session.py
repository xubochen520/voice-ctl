"""会话控制：把「按下 → 录音 → 松开 → 识别执行」的状态机抽出来。

为什么单独成类：这段逻辑有多线程来源（pynput 监听线程、主循环的超时检查），
而并发缺陷没法靠手工点击验证。抽出来后就能用假 Recorder 精确测：
重复按下、重复松开、超时与松开同时到达、start 失败等。

放在 pipeline 之外，是因为它管的是**会话生命周期**，不是单次判断。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class RecordingLike(Protocol):
    """Recorder 的最小子集——测试用假对象实现它即可。"""

    def start(self) -> None: ...
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

    stats: SessionStats = field(default_factory=SessionStats)

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _recording: bool = field(default=False, init=False, repr=False)

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
            self.stats.pressed += 1
        try:
            self.recorder.start()
        except Exception as e:  # noqa: BLE001 - 具体类型由调用方用 recorder_error 收窄
            with self._lock:
                self._recording = False
            self.stats.start_failed += 1
            if self.on_error:
                self.on_error(f"打开录音设备失败：{e}")
            return
        self.timer.arm()  # type: ignore[attr-defined]
        if self.on_recording_start:
            self.on_recording_start()

    def release(self) -> None:
        self.finish()

    def timeout(self) -> None:
        """超时守护触发。只有在录音中才有效。"""
        if not self.recording:
            return
        self.stats.timeouts += 1
        self.finish(forced=True)

    def finish(self, *, forced: bool = False) -> None:
        with self._lock:
            if not self._recording:
                return  # 另一个路径已经收尾
            self._recording = False
        self.timer.disarm()  # type: ignore[attr-defined]

        try:
            rec = self.recorder.stop()
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
            was = self._recording
            self._recording = False
        self.timer.disarm()  # type: ignore[attr-defined]
        if was:
            try:
                self.recorder.abort()
            except Exception:  # noqa: BLE001
                pass
