"""事件总线：把「运行中发生了什么」变成可订阅的结构化事件流。

为什么不能让 UI 去解析 stdout：

  * stdout 一旦被重定向（打包 exe、启动器接管），UI 就什么都收不到
  * 常驻进程的 print 是块缓冲的，行迟迟到不了 UI
  * 「识别出什么、命中哪个动作、耗时多少」如果只剩一句拼好的字符串，
    UI 想按「只看失败的」筛选就无从下手

所以运行期只做一件事：emit 结构化事件。谁来消费由订阅方决定：

    命令行  → 控制台 sink（带颜色，级别过滤）
    UI      → 轮询 since(seq)，自己按级别/关键字过滤、着色、导出
    两者    → 文件 sink（滚动，事后还能查）

三条硬约束，都是踩过才知道的：

  1. **emit 必须极快**。热键回调跑在 pynput 的键盘钩子线程里，而 Windows 的
     低级钩子有超时（LowLevelHooksTimeout，默认 300ms），回调里做磁盘 I/O
     会让钩子被系统悄悄摘掉——表现就是「按了几次之后热键忽然失灵」。
     所以 emit 只做内存追加 + 入队，真正的写盘/写控制台交给唯一的后台线程。
  2. **订阅回调不能在持锁时调用**，否则回调里再 emit 就死锁。
  3. **环形缓冲必须有上限**。这是个常驻几周的进程，无界列表迟早吃光内存；
     挤掉的条数要记下来，否则「日志怎么少了一截」没法解释。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# --------------------------------------------------------------------------- #
# 级别
# --------------------------------------------------------------------------- #

LEVELS = ("debug", "info", "ok", "warn", "error")
RANK: dict[str, int] = {name: i for i, name in enumerate(LEVELS)}

GLYPH: dict[str, str] = {
    "debug": "·",
    "info": "·",
    "ok": "✓",
    "warn": "⚠",
    "error": "✗",
}

_ANSI: dict[str, str] = {
    "debug": "\x1b[90m",
    "info": "\x1b[37m",
    "ok": "\x1b[32m",
    "warn": "\x1b[33m",
    "error": "\x1b[31m",
}
_RESET = "\x1b[0m"

DEFAULT_CAPACITY = 3000

# 这些 kind 是「原样转发到控制台」的裸输出，控制台 sink 必须跳过，
# 否则同一条内容会被打印两次（tee 转发一次、sink 再打一次）。
_ECHOED_KINDS = frozenset({"stdout", "stderr"})


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    level: str
    kind: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def rank(self) -> int:
        return RANK.get(self.level, 1)

    def clock(self, *, millis: bool = False) -> str:
        lt = time.localtime(self.ts)
        base = time.strftime("%H:%M:%S", lt)
        if not millis:
            return base
        return f"{base}.{int((self.ts % 1) * 1000):03d}"

    def stamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))

    def line(self, *, millis: bool = False) -> str:
        return f"{self.clock(millis=millis)} {GLYPH.get(self.level, '·')} {self.text}"

    def detail(self) -> str:
        """把 data 压成一行，给文件日志用。截断是为了不让单条日志爆掉。"""
        if not self.data:
            return ""
        parts = []
        for k, v in self.data.items():
            s = repr(v)
            if len(s) > 120:
                s = s[:117] + "..."
            parts.append(f"{k}={s}")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# 总线
# --------------------------------------------------------------------------- #


class EventBus:
    """线程安全的事件发布/订阅。

    sink 由**单独一个后台线程**串行调用：既能保证 sink 内部不需要加锁，
    也把磁盘 I/O 挪出了热键钩子线程。`flush()` 可以把队列排空，便于测试
    和退出前收尾。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._lock = threading.Lock()
        self._events: deque[Event] = deque(maxlen=max(1, int(capacity)))
        self._sinks: list[Callable[[Event], None]] = []
        self._seq = 0
        self._dropped = 0
        self._closed = False

        self._queue: queue.Queue[Event | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # -- 发布 ------------------------------------------------------------- #

    def emit(self, level: str, message: str, *, kind: str = "app", **data: Any) -> Event:
        """记一条事件。**必须在微秒级返回**，见模块开头的约束 1。

        第一个位置参数刻意叫 `message` 而不是 `text`：`text` 是调用方最想
        塞进 data 的键名（识别结果就叫 text），同名会直接撞成
        `TypeError: got multiple values for argument 'text'`——而且是在
        热键回调线程里抛，表现成"按了没反应"，极难查。实测踩过一次。
        """
        if level not in RANK:
            level = "info"
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._seq += 1
            ev = Event(self._seq, time.time(), level, kind, str(message), data)
            self._events.append(ev)
            has_sinks = bool(self._sinks)
        if has_sinks:
            self._queue.put(ev)
        return ev

    # -- 订阅 ------------------------------------------------------------- #

    def subscribe(self, sink: Callable[[Event], None]) -> Callable[[], None]:
        """注册 sink，返回反注册函数。sink 在后台线程被调用，每次一条、串行。"""
        with self._lock:
            self._sinks.append(sink)
        self._ensure_worker()

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._sinks.remove(sink)
                except ValueError:
                    pass

        return unsubscribe

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._closed:
                return
            t = threading.Thread(target=self._pump, name="voice-ctl-events", daemon=True)
            self._worker = t
            t.start()

    def _pump(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            with self._lock:
                sinks = tuple(self._sinks)
            for sink in sinks:
                try:
                    sink(item)
                except Exception:  # noqa: BLE001 - 日志写失败不该拖垮主流程
                    pass

    def flush(self, timeout: float = 1.0) -> bool:
        """等队列排空。返回是否排空（超时返回 False）。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self._queue.empty():
                # 队列空不代表 pump 处理完了最后一条，给它一拍
                time.sleep(0.005)
                if self._queue.empty():
                    return True
            time.sleep(0.005)
        return self._queue.empty()

    def close(self, timeout: float = 1.0) -> None:
        if self._closed:
            return
        self.flush(timeout)
        self._closed = True
        with self._lock:
            self._sinks.clear()
        self._queue.put(None)

    # -- 读取 ------------------------------------------------------------- #

    def since(self, seq: int) -> list[Event]:
        """取 seq 之后的事件。UI 定时轮询用这个，不依赖后台线程。"""
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    def snapshot(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        """清空缓冲但**不动 seq**——否则 UI 的增量游标会倒流，丢事件。"""
        with self._lock:
            self._events.clear()


# --------------------------------------------------------------------------- #
# 全局总线 + 快捷函数
# --------------------------------------------------------------------------- #

_bus = EventBus()


def get_bus() -> EventBus:
    return _bus


def set_bus(bus: EventBus) -> EventBus:
    """替换全局总线（测试用），返回被换下来的那个。"""
    global _bus
    old, _bus = _bus, bus
    return old


def emit(level: str, message: str, *, kind: str = "app", **data: Any) -> Event:
    return _bus.emit(level, message, kind=kind, **data)


def debug(message: str, **kw: Any) -> Event:
    return _bus.emit("debug", message, **kw)


def info(message: str, **kw: Any) -> Event:
    return _bus.emit("info", message, **kw)


def ok(message: str, **kw: Any) -> Event:
    return _bus.emit("ok", message, **kw)


def warn(message: str, **kw: Any) -> Event:
    return _bus.emit("warn", message, **kw)


def error(message: str, **kw: Any) -> Event:
    return _bus.emit("error", message, **kw)


# --------------------------------------------------------------------------- #
# sink：控制台
# --------------------------------------------------------------------------- #


def enable_ansi(stream: TextIO | None = None) -> bool:
    """在 Windows 控制台上打开 ANSI 转义支持。返回是否可用。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


def attach_console(
    bus: EventBus | None = None,
    *,
    stream: TextIO | None = None,
    min_level: str = "info",
    color: bool | None = None,
    millis: bool = False,
) -> Callable[[], None]:
    """把事件打到控制台。

    color 默认只在真的是终端时开——重定向到文件时留着转义码是纯污染。
    """
    bus = bus or _bus
    out = stream if stream is not None else sys.stdout
    if color is None:
        color = bool(getattr(out, "isatty", lambda: False)()) and enable_ansi(out)
    floor = RANK.get(min_level, 1)

    indent = " " * 11

    def sink(ev: Event) -> None:
        # kind=stdout/stderr 的行已经由 tee 原样转发到控制台了，这里跳过，
        # 否则同一行会打两遍。
        if ev.rank < floor or ev.kind in _ECHOED_KINDS:
            return
        lines = ev.text.splitlines() or [""]
        body = "\n".join(
            [f"{ev.clock(millis=millis)} {GLYPH.get(ev.level, '·')} {lines[0]}"]
            # 多行事件（比如一次命中的完整报告）后续行缩进对齐，读起来是一块
            + [f"{indent} {ln}" for ln in lines[1:]]
        )
        if color:
            body = f"{_ANSI.get(ev.level, '')}{body}{_RESET}"
        try:
            out.write(body + "\n")
            out.flush()
        except Exception:  # noqa: BLE001 - 控制台没了不该让进程崩
            pass

    return bus.subscribe(sink)


# --------------------------------------------------------------------------- #
# sink：文件
# --------------------------------------------------------------------------- #


def attach_file(
    bus: EventBus | None = None,
    path: str | Path | None = None,
    *,
    min_level: str = "debug",
    max_bytes: int = 2_000_000,
) -> Callable[[], None]:
    """把事件写到文件，超过 max_bytes 时轮转成 .1（只留一代）。

    只留一代是刻意的：语音助手的日志是拿来「回看刚才那次为什么没反应」的，
    不是审计留档，堆一堆历史文件只会让用户找不到最新的那份。
    """
    bus = bus or _bus
    if path is None:
        from .bootstrap import log_path

        path = log_path()
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return lambda: None

    floor = RANK.get(min_level, 0)
    try:
        written = target.stat().st_size
    except OSError:
        written = 0

    def rotate() -> None:
        backup = target.with_suffix(target.suffix + ".1")
        try:
            backup.unlink(missing_ok=True)
            target.replace(backup)
        except OSError:
            pass

    def sink(ev: Event) -> None:
        nonlocal written
        if ev.rank < floor:
            return
        detail = ev.detail()
        row = f"{ev.stamp()} {ev.level.upper():5} [{ev.kind}] {ev.text}"
        if detail:
            row += f"  | {detail}"
        row = row.replace("\n", "\n    ") + "\n"
        # 按累计字节数判断，而不是每次 stat()：stat 在常驻进程里是白花的系统调用，
        # 而"每 N 条才检查一次"会让 max_bytes 名不副实（实测能涨到上限的几十倍）。
        if written + len(row.encode("utf-8")) > max_bytes:
            rotate()
            written = 0
        try:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(row)
            written += len(row.encode("utf-8"))
        except OSError:
            return

    return bus.subscribe(sink)


# --------------------------------------------------------------------------- #
# stdout/stderr 捕获
# --------------------------------------------------------------------------- #

_LEVEL_HINTS: tuple[tuple[str, str], ...] = (
    ("Traceback (most recent call last)", "error"),
    ("✗", "error"),
    ("错误", "error"),
    ("失败", "error"),
    ("⚠", "warn"),
    ("警告", "warn"),
    ("✓", "ok"),
)


def guess_level(line: str) -> str:
    """从一行裸输出猜级别。

    纯属为了让第三方库（sherpa-onnx 的 C++ 打印、traceback）混进来的行
    也能着上色。猜错不影响正确性，只影响颜色。
    """
    for needle, level in _LEVEL_HINTS:
        if needle in line:
            return level
    return "info"


class StreamTee:
    """包一层流：原样转发，同时把整行喂进事件总线。

    tee 的意义是**兜底**——我们自己 emit 的事件是结构化的一等公民，
    但 traceback、C++ 库的 stderr、别人写的 print 只能从这里捞。
    少了它，UI 上会看到「识别失败了但没有原因」。

    控制台 sink 会跳过 kind=stdout/stderr 的事件，因为这些行已经被
    原样转发到控制台了，不跳就会打两遍。
    """

    def __init__(self, stream: TextIO, bus: EventBus, kind: str) -> None:
        self._stream = stream
        self._bus = bus
        self._kind = kind
        self._level = "error" if kind == "stderr" else "info"
        self._buf = ""
        self._lock = threading.Lock()

    # -- 流协议 ----------------------------------------------------------- #

    def write(self, s: str) -> int:
        if not isinstance(s, str):  # pragma: no cover - 防御
            s = str(s)
        try:
            self._stream.write(s)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._bus.emit(
                        guess_level(line) if self._kind == "stdout" else "error",
                        line.rstrip(),
                        kind=self._kind,
                    )
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self._bus.emit(self._level, self._buf.rstrip(), kind=self._kind)
            self._buf = ""
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str) -> Any:
        # 其余流属性（encoding / fileno / isatty / buffer …）原样透传，
        # 否则某些库一访问就 AttributeError
        return getattr(self._stream, name)


_tee_state: dict[str, Any] = {"installed": False, "restore": None}


def install_stream_tee(bus: EventBus | None = None) -> Callable[[], None]:
    """把 sys.stdout / sys.stderr 换成 tee。重复调用是幂等的。"""
    bus = bus or _bus
    if _tee_state["installed"]:
        return _tee_state["restore"] or (lambda: None)

    out_raw, err_raw = sys.stdout, sys.stderr
    out_tee = StreamTee(out_raw, bus, "stdout")
    err_tee = StreamTee(err_raw, bus, "stderr")
    sys.stdout, sys.stderr = out_tee, err_tee  # type: ignore[assignment]

    def restore() -> None:
        if not _tee_state["installed"]:
            return
        try:
            out_tee.flush()
            err_tee.flush()
        except Exception:  # noqa: BLE001
            pass
        sys.stdout, sys.stderr = out_raw, err_raw  # type: ignore[assignment]
        _tee_state["installed"] = False

    _tee_state["installed"] = True
    _tee_state["restore"] = restore
    return restore


def console_stream() -> TextIO:
    """当前真正连着控制台的原始流（tee 的原件）。"""
    stream = sys.stdout
    while isinstance(stream, StreamTee):
        stream = stream._stream  # noqa: SLF001 - 就是本模块自己的类
    return stream
