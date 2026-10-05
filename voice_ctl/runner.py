"""运行引擎：热键 → 录音 → 识别 → 执行。

CLI（`voice-ctl run`）和 UI 共用这一份，避免出现「UI 里能用、命令行里不行」
这种分叉——那种缺陷只有用户会先发现。

三个刻意的决定：

  1. **识别不在键盘钩子线程里跑。**
     pynput 的回调就是 Windows 的低级钩子过程。整条识别+执行链路实测
     100~250ms（模型冷启动更久），一旦超过系统的 LowLevelHooksTimeout
     （默认 300ms），Windows 会**悄悄摘掉钩子**——表现是「按几次之后热键
     忽然没反应了」，重启才好，而且日志上什么都看不到。
     所以钩子线程只做状态转移 + 停止录音，剩下全丢给工作线程。

  2. **单工作线程串行执行。**
     不用线程池：用户不会连按两条指令，而串行能保证「日志顺序 = 实际发生
     顺序」——排查「我刚才明明说了」这类问题时，这一点是刚需。

  3. **只 emit 事件，不 print。**
     谁来显示由订阅方决定（见 events.py 开头的详述）。UI 和命令行因此
     拿到的信息量完全一样。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import events
from . import appfind
from .actions import ActionResult, build_registry
from .app import Outcome, Pipeline
from .asr import Asr, AsrError
from .config import ActionConfig, AppConfig
from .matcher import Matcher
from .normalize import NormalizeConfig, Normalizer
from .recorder import Recorder, RecorderError, play_beep
from .session import SessionController, SessionStats

# 状态常量用字符串而不是 Enum：UI 只拿它做展示，字符串更好打日志
IDLE = "idle"
LOADING = "loading"
RUNNING = "running"
ERROR = "error"

STATE_LABEL = {
    IDLE: "未启动",
    LOADING: "加载中",
    RUNNING: "运行中",
    ERROR: "出错",
}


# --------------------------------------------------------------------------- #
# 运行时组装（原来在 cli.py，挪过来让 UI 也能直接用，CLI 仍从这里导入）
# --------------------------------------------------------------------------- #


@dataclass
class Runtime:
    cfg: AppConfig
    normalizer: Normalizer
    matcher: Matcher
    registry: Any
    asr: Asr
    decider: object | None = None
    app_index: Any = None
    """已安装应用索引（意图层用它理解「打开QQ」）。"""
    llm: Any = None
    """可选的小模型层（voice_ctl.llm.SlotExtractor）。"""

    def pipeline(self) -> Pipeline:
        return Pipeline(
            asr=self.asr,
            matcher=self.matcher,
            registry=self.registry,
            actions=self.cfg.enabled_actions,
            normalizer=self.normalizer,
            decider=self.decider,
            min_confidence=self.cfg.decision.min_confidence,
            app_index=self.app_index,
            intent_enabled=self.cfg.intent.enabled,
            llm=self.llm,
            llm_candidates=self.cfg.llm.max_candidates,
        )


def model_dir_for(cfg: AppConfig) -> Path:
    """模型目录，含「打包内嵌模型」兜底。所有命令都该用它，而不是 cfg.model_path()。"""
    from . import bootstrap

    return bootstrap.resolve_model_dir(cfg.model_path())


def build_runtime(
    cfg: AppConfig,
    *,
    with_asr: bool = True,
    with_decision: bool | None = None,
    bus: events.EventBus | None = None,
) -> Runtime:
    norm = Normalizer(
        NormalizeConfig(
            strip_prefixes=cfg.match.strip_prefixes,
            strip_suffixes=cfg.match.strip_suffixes,
            inline_fillers=cfg.match.inline_fillers,
            substitutions=cfg.normalize.substitutions,
            use_pinyin=cfg.normalize.use_pinyin,
        )
    )
    matcher = Matcher(cfg.enabled_actions, normalizer=norm, threshold=cfg.match.threshold)
    registry = build_registry(cfg.enabled_actions)
    asr = Asr(
        model_dir_for(cfg),
        language=cfg.model.language,
        use_itn=cfg.model.use_itn,
        num_threads=cfg.model.num_threads,
        provider=cfg.model.provider,
        pad_ms=cfg.model.pad_ms,
    )

    # 已安装应用索引：意图层理解「打开QQ」靠它。构建很便宜（几百个名字+拼音），
    # 但底层的开始菜单扫描要起一次 PowerShell（冷启动实测约 0.8s）。
    # `warm_start_apps()` 在后台线程里把那份列表扫好，等用户第一次说话时
    # 索引已经是热的——冷的那 0.8 秒会正好落在"用户松开热键等结果"的那一刻。
    app_index = None
    if cfg.intent.enabled:
        from .apps import AppIndex

        appfind.warm_start_apps()
        app_index = AppIndex(use_pinyin=cfg.normalize.use_pinyin)
        # 当场把条目建出来。AppIndex 自己也是懒构建的，但那会在**用户说完第一句
        # 话**时触发——如果后台预热线程还没扫完，索引里就是空的，于是"打开QQ"
        # 报找不到。这里先把（可能是空的）快照定下来，预热完成后重建一次即可。
        app_index.entries()

    decider = None
    want = cfg.decision.enabled if with_decision is None else with_decision
    if want:
        from .decision import DecisionUnavailable, SemanticDecider

        try:
            decider = SemanticDecider(cfg.enabled_actions, cfg.decision, root=cfg.decision_path())
            decider.load()
        except DecisionUnavailable as e:
            (bus or events.get_bus()).emit(
                "warn", f"语义层启用失败，已回落到仅别名匹配：{e}", kind="decision"
            )
            decider = None

    # 小模型层（可选）。探测失败只记一条日志——没装 LM Studio/Ollama 是常态，
    # 不该在界面上刷一堆警告。
    llm = None
    if cfg.llm.enabled:
        from .llm import SlotExtractor

        llm = SlotExtractor(
            cfg.llm.endpoint, cfg.llm.model, timeout=cfg.llm.timeout,
        )
        if not llm.probe():
            (bus or events.get_bus()).emit(
                "warn", f"小模型层开启但服务连不上，本句起回落到前几层：{llm.last_error}",
                kind="llm",
            )
            llm = None

    return Runtime(cfg, norm, matcher, registry, asr, decider, app_index, llm)


class _Emitter:
    """把 events.info/ok/warn/error 这套写法绑到**某一个**总线上。

    为什么要这么一层：Engine 允许注入 bus（UI 和测试都这么用），但模块级的
    events.ok() 写的是全局总线。混着用的结果是一半事件发到全局、一半发到
    注入的那条——调用方只能看到一半日志，而且看不出少了哪一半。
    """

    __slots__ = ("_bus",)

    def __init__(self, bus: events.EventBus) -> None:
        self._bus = bus

    def emit(self, level: str, message: str, **kw: Any) -> events.Event:
        return self._bus.emit(level, message, **kw)

    def debug(self, message: str, **kw: Any) -> events.Event:
        return self._bus.emit("debug", message, **kw)

    def info(self, message: str, **kw: Any) -> events.Event:
        return self._bus.emit("info", message, **kw)

    def ok(self, message: str, **kw: Any) -> events.Event:
        return self._bus.emit("ok", message, **kw)

    def warn(self, message: str, **kw: Any) -> events.Event:
        return self._bus.emit("warn", message, **kw)

    def error(self, message: str, **kw: Any) -> events.Event:
        return self._bus.emit("error", message, **kw)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


@dataclass
class EngineStats:
    """会话统计之外的引擎级计数。UI 底栏直接显示它。"""

    outcomes: int = 0
    ok: int = 0
    failed: int = 0
    no_match: int = 0
    errors: int = 0

    last_text: str = ""
    last_action: str = ""
    last_via: str = ""
    last_message: str = ""
    last_ms: float = 0.0
    model_load_ms: float = 0.0

    def summary(self) -> str:
        return (
            f"识别 {self.outcomes}  成功 {self.ok}  失败 {self.failed}  "
            f"未命中 {self.no_match}"
        )


@dataclass
class _Job:
    kind: str
    """audio / text"""

    payload: Any
    dry_run: bool
    done: threading.Event | None = None
    outcome: Outcome | None = None
    error: str = ""


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #


class Engine:
    """把热键、录音、识别、执行、统计、事件串成一个可控开关的对象。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        dry_run: bool = False,
        bus: events.EventBus | None = None,
        hotkey: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.bus = bus or events.get_bus()
        self.ev = _Emitter(self.bus)
        self.hotkey_spec = hotkey or cfg.hotkey.keys

        self.stats = EngineStats()
        self.session_stats = SessionStats()
        self.last_outcome: Outcome | None = None

        self._state = IDLE
        self._error = ""
        self._lock = threading.RLock()

        self._rt: Runtime | None = None
        self._pipe: Pipeline | None = None
        self._recorder: Recorder | None = None
        self._controller: SessionController | None = None
        self._listener: Any = None
        self._model_loaded = False

        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None

    # -- 状态 ------------------------------------------------------------- #

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def state_label(self) -> str:
        return STATE_LABEL.get(self.state, self.state)

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    def _set_state(self, state: str, *, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error

    @property
    def runtime(self) -> Runtime | None:
        return self._rt

    @property
    def actions(self) -> list[ActionConfig]:
        return self.cfg.enabled_actions if self._rt is None else self._rt.cfg.enabled_actions

    # -- 组装（快） ------------------------------------------------------- #

    def prepare(self) -> bool:
        """构建匹配/动作/识别器对象。**不加载模型**，所以很快（毫秒级）。"""
        with self._lock:
            if self._rt is not None:
                return True
        try:
            rt = build_runtime(self.cfg, bus=self.bus)
        except Exception as e:  # noqa: BLE001 - 配置/依赖问题都要在 UI 上看得见
            self._fail(f"初始化失败：{type(e).__name__}: {e}")
            return False

        with self._lock:
            self._rt = rt
            self._pipe = rt.pipeline()
        self._start_worker()
        # 提醒线程跟着运行时一起起来：它和监听热键无关（电脑开着就该提醒，
        # 哪怕热键没启动），所以放在 prepare 而不是 start 里。
        self._start_reminders()
        self._set_state(IDLE)
        self.ev.ok(
            f"就绪：{len(rt.registry)} 个动作，热键 {self.hotkey_spec}"
            + ("，" + ("语义层已启用" if rt.decider else "仅别名匹配")),
            kind="engine",
        )
        return True

    def _fail(self, message: str) -> None:
        self._error = message
        self._set_state(ERROR, error=message)
        self.stats.errors += 1
        self.ev.error(message, kind="engine")

    def _start_reminders(self) -> None:
        """把到点提醒接上。日程关掉时什么都不做。"""
        if not self.cfg.schedule.enabled:
            return
        from . import reminder

        try:
            reminder.start(path=self.cfg.schedule_path())
        except Exception as e:  # noqa: BLE001 - 提醒起不来不该让整个程序不可用
            self.ev.warn(f"提醒服务启动失败（日程仍可创建）：{type(e).__name__}: {e}", kind="schedule")
            return
        self.ev.info(reminder.status(), kind="schedule")

    # -- 加载模型（慢） --------------------------------------------------- #

    def load_model(self) -> bool:
        """加载识别模型。实测 1~3 秒，**绝不能在 UI 线程里调**。"""
        rt = self._rt
        if rt is None and not self.prepare():
            return False
        rt = self._rt
        assert rt is not None
        if self._model_loaded:
            return True

        self._set_state(LOADING)
        md = getattr(rt.asr, "model_dir", "?")
        self.ev.info(f"正在加载识别模型：{md}", kind="engine")
        try:
            rt.asr.load()
        except AsrError as e:
            self._fail(str(e))
            return False
        except Exception as e:  # noqa: BLE001
            self._fail(f"加载模型失败：{type(e).__name__}: {e}")
            return False

        self._model_loaded = True
        self.stats.model_load_ms = float(getattr(rt.asr, "load_ms", 0.0))
        self.ev.ok(f"识别模型就绪（{self.stats.model_load_ms:.0f}ms，常驻内存）", kind="engine")
        self._set_state(IDLE)
        return True

    # -- 工作线程 --------------------------------------------------------- #

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._work_loop, name="voice-ctl-worker", daemon=True
            )
            self._worker.start()

    def _work_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job.outcome = self._run_job(job)
            except Exception as e:  # noqa: BLE001 - 工作线程绝不能死
                job.error = f"{type(e).__name__}: {e}"
                self.ev.error(f"处理失败：{job.error}", kind="engine")
            finally:
                # 事件先于 done.set()：等 done 的一方醒来时日志已经在总线上，
                # 否则 CLI/测试里会出现「结果拿到了但日志还没到」的顺序错乱
                if job.done is not None:
                    job.done.set()

    def _run_job(self, job: _Job) -> Outcome:
        pipe = self._pipe
        if pipe is None:
            raise RuntimeError("运行时还没准备好")
        if job.kind == "audio":
            out = pipe.process_audio(job.payload, dry_run=job.dry_run)
        else:
            out = pipe.process_text(str(job.payload), dry_run=job.dry_run)
        self._report(out)
        return out

    def _report(self, out: Outcome) -> None:
        self.stats.outcomes += 1
        self.stats.last_text = out.text
        self.stats.last_action = out.action_id or ""
        self.stats.last_via = out.via
        self.stats.last_ms = out.timing.total_ms
        self.last_outcome = out

        if out.result and not out.result.ok:
            level = "error"
            self.stats.failed += 1
            self.stats.last_message = out.result.message
        elif out.ok:
            level = "ok"
            self.stats.ok += 1
            self.stats.last_message = out.result.message if out.result else ""
        elif out.action_id:
            level = "error"
            self.stats.failed += 1
            self.stats.last_message = out.note or "执行未成功"
        elif out.text:
            level = "warn"
            self.stats.no_match += 1
            self.stats.last_message = out.note or "没匹配到动作"
        else:
            level = "info"
            self.stats.last_message = out.note or "没听到内容"

        self.bus.emit(
            level,
            out.report(verbose=True),
            kind="outcome",
            text=out.text,
            action=out.action_id,
            via=out.via,
            ms=round(out.timing.total_ms, 1),
            ok=bool(out.ok),
        )

    # -- 生命周期 --------------------------------------------------------- #

    def start(self) -> bool:
        """开始监听热键。prepared 之前调用会先 prepare。"""
        if self.running:
            return True
        if self._rt is None and not self.prepare():
            return False
        rt = self._rt
        assert rt is not None

        from .hotkey import HotkeyError, HotkeyListener, HotkeyTimer

        cfg = self.cfg
        self._recorder = Recorder(
            device=cfg.audio.device or None, max_duration_ms=cfg.hotkey.max_duration_ms
        )
        beep = cfg.feedback.beep

        def on_rec_start() -> None:
            if beep:
                play_beep(cfg.feedback.beep_start_hz, cfg.feedback.beep_ms)
            self.ev.info("● 录音中 …", kind="session")

        def on_rec_end() -> None:
            if beep:
                play_beep(cfg.feedback.beep_end_hz, cfg.feedback.beep_ms)
            self.ev.debug("松开，正在识别", kind="session")

        def on_ready(rec) -> None:  # noqa: ANN001
            # 关键：这里只入队。识别跑在键盘钩子线程里会让 Windows 摘钩子
            self.ev.debug(
                f"录音 {rec.duration_s * 1000:.0f}ms 峰值 {rec.peak:.3f}，排队识别",
                kind="session",
            )
            self._jobs.put(_Job("audio", rec.samples, self.dry_run))

        controller = SessionController(
            recorder=self._recorder,
            timer=HotkeyTimer(cfg.hotkey.max_duration_ms, lambda: None),
            on_recording_start=on_rec_start,
            on_recording_end=on_rec_end,
            on_recording_ready=on_ready,
            on_too_short=lambda ms: self.ev.warn(f"太短（{ms:.0f}ms），忽略", kind="session"),
            on_gated=lambda why: self.ev.warn(
                f"{why}，忽略（若确实说话了，把 [audio].min_peak 调低）", kind="session"
            ),
            on_error=lambda msg: self.ev.error(msg, kind="session"),
            min_duration_ms=cfg.hotkey.min_duration_ms,
            min_peak=cfg.audio.min_peak,
            recorder_error=RecorderError,
            stats=self.session_stats,
        )
        controller.timer = HotkeyTimer(cfg.hotkey.max_duration_ms, controller.timeout)

        def on_press() -> None:
            controller.press()

        def on_release() -> None:
            controller.release()

        try:
            listener = HotkeyListener(self.hotkey_spec, on_press=on_press, on_release=on_release)
            listener.start()
        except HotkeyError as e:
            self._fail(str(e))
            return False

        with self._lock:
            self._controller = controller
            self._listener = listener
        self._set_state(RUNNING)
        if not self._model_loaded:
            self.ev.warn(
                "识别模型尚未加载，第一次说话会现加载（会卡几秒）", kind="engine"
            )
        self.ev.ok(
            f"已启动。按住 {self.hotkey_spec} 说话，松开执行。"
            + ("（dry-run：只报告不执行）" if self.dry_run else ""),
            kind="engine",
        )
        return True

    def stop(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
            controller, self._controller = self._controller, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                pass
        if controller is not None:
            controller.abort()
        if self._state == RUNNING:
            self._set_state(IDLE)
            self.ev.info("已停止监听", kind="engine")

    def toggle(self) -> bool:
        if self.running:
            self.stop()
            return False
        return self.start()

    def tick(self) -> bool:
        """超时守护。UI 用 after()、CLI 用主循环调它。"""
        c = self._controller
        if c is None:
            return False
        return bool(c.timer.tick())  # type: ignore[attr-defined]

    def close(self) -> None:
        self.stop()
        self._jobs.put(None)
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=1.0)
        with self._lock:
            self._worker = None
        if self.cfg.schedule.enabled:
            from . import reminder

            reminder.stop()

    # -- 热键 ------------------------------------------------------------- #

    def apply_hotkey(self, spec: str) -> bool:
        """换一个热键。正在跑的话当场重挂监听，不用重启程序。"""
        from .hotkey import HotkeyError, parse_hotkey

        try:
            parsed = parse_hotkey(spec)
        except HotkeyError as e:
            self.ev.error(f"热键不可用：{e}", kind="hotkey")
            return False

        was_running = self.running
        if was_running:
            self.stop()
        self.hotkey_spec = spec
        self.cfg.hotkey.keys = spec
        if was_running:
            ok = self.start()
            if not ok:
                return False
        self.ev.ok(f"热键已切换为 {parsed.display}（{spec}）", kind="hotkey")
        return True

    # -- 手动触发 --------------------------------------------------------- #

    def submit_text(self, text: str, *, dry_run: bool | None = None) -> None:
        """把一句话丢进链路，结果通过事件出来。UI 用这个（不阻塞）。"""
        if self._rt is None and not self.prepare():
            return
        self._jobs.put(_Job("text", text, self.dry_run if dry_run is None else dry_run))

    def submit_audio(self, samples: Any, *, dry_run: bool | None = None) -> None:
        """把一段波形丢进链路（不经过麦克风）。

        存在的理由有两个：单测里没法对着麦克风喊；界面上"录一段试试"这类
        功能也需要它。行为与热键路径完全一致——都是**排队**，不在这里做识别。
        """
        if self._rt is None and not self.prepare():
            return
        self._jobs.put(_Job("audio", samples, self.dry_run if dry_run is None else dry_run))

    def test_text(
        self, text: str, *, dry_run: bool | None = None, timeout: float = 30.0
    ) -> Outcome | None:
        """同步版本，给 CLI 和测试用。"""
        if self._rt is None and not self.prepare():
            return None
        job = _Job("text", text, self.dry_run if dry_run is None else dry_run, threading.Event())
        self._jobs.put(job)
        if not job.done.wait(timeout):  # type: ignore[union-attr]
            self.ev.error(f"「{text}」处理超时（{timeout:.0f}s）", kind="engine")
            return None
        if job.error:
            self.ev.error(f"「{text}」处理失败：{job.error}", kind="engine")
            return None
        return job.outcome

    # -- 预检 ------------------------------------------------------------- #

    def action_configs(self) -> list[ActionConfig]:
        return list(self.cfg.actions)

    def preflight(self, action_id: str) -> ActionResult:
        """某个动作现在能不能跑通（找不找得到目标）。"""
        if self._rt is None and not self.prepare():
            return ActionResult(False, "运行时未就绪")
        assert self._rt is not None
        act = self._rt.registry.get(action_id)  # type: ignore[attr-defined]
        if act is None:
            return ActionResult(False, f"{action_id} 不在注册表里（可能被禁用了）")
        try:
            return act.preflight()
        except Exception as e:  # noqa: BLE001
            return ActionResult(False, f"预检异常：{type(e).__name__}: {e}")

    def status_line(self) -> str:
        s = self.session_stats
        return (
            f"{self.state_label} · 按下 {s.pressed} · 完成 {s.finished} · "
            f"{self.stats.summary()}"
        )


def make_engine(cfg: AppConfig, **kw: Any) -> Engine:
    return Engine(cfg, **kw)


__all__ = [
    "ERROR",
    "IDLE",
    "LOADING",
    "RUNNING",
    "STATE_LABEL",
    "Engine",
    "EngineStats",
    "Runtime",
    "build_runtime",
    "make_engine",
    "model_dir_for",
]
