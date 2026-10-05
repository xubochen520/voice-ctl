"""运行引擎。

CLI（run）和 UI 共用这一份，所以它的行为既要能被单测精确驱动，又要真的
守住那条并发底线：**识别不能跑在键盘钩子线程里**。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from voice_ctl import events
from voice_ctl.asr import AsrResult
from voice_ctl.runner import IDLE, RUNNING, Engine

BASE = """
[hotkey]
keys = "<f9>"
min_duration_ms = 100
max_duration_ms = 3000

[audio]
min_peak = 0.01

[match]
threshold = 80

[feedback]
beep = false

[[action]]
id = "open.notepad"
handler = "open_app"
aliases = ["记事本", "notepad", "记录本"]
target = "notepad.exe"

[[action]]
id = "open.calc"
handler = "open_app"
aliases = ["计算器", "calc"]
target = "calc.exe"
"""


class StubAsr:
    """替身识别器。可以在 transcribe 里故意变慢，用来验证线程契约。"""

    load_ms = 12.0

    def __init__(self, text: str = "打开记事本", delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.loaded = False
        self.calls = 0

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, samples, sample_rate: int = 16000) -> AsrResult:  # noqa: ANN001
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return AsrResult(text=self.text, duration_s=1.0, infer_ms=1.0)

    def transcribe_file(self, path):  # noqa: ANN001
        return self.transcribe(None)


@pytest.fixture()
def cfg(tmp_path: Path):  # noqa: ANN201
    from voice_ctl.config import load_config

    p = tmp_path / "config.toml"
    p.write_text(BASE, encoding="utf-8")
    return load_config(p)


@pytest.fixture()
def bus():  # noqa: ANN201
    return events.EventBus()


@pytest.fixture()
def engine(cfg, bus):  # noqa: ANN201, ANN001
    eng = Engine(cfg, bus=bus)
    yield eng
    eng.close()


def wait_for(pred, timeout: float = 5.0) -> bool:  # noqa: ANN001
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #


def test_prepare_builds_registry_and_emits(engine: Engine, bus: events.EventBus):
    assert engine.prepare() is True
    assert engine.state == IDLE
    assert len(engine.actions) == 2
    bus.flush()
    assert any(e.kind == "engine" and "就绪" in e.text for e in bus.snapshot())


def test_prepare_is_idempotent(engine: Engine):
    assert engine.prepare() is True
    first = engine.runtime
    assert engine.prepare() is True
    assert engine.runtime is first


def test_prepare_reports_bad_handler(cfg, bus):  # noqa: ANN001
    """handler 不认识时必须显式失败，而不是静默少几个动作。"""
    from dataclasses import replace

    bad = replace(cfg.actions[0], handler="nonexistent")
    cfg.actions = [bad]
    eng = Engine(cfg, bus=bus)
    assert eng.prepare() is False
    assert eng.state == "error"
    assert "nonexistent" in eng.error
    bus.flush()
    assert any(e.level == "error" for e in bus.snapshot())
    eng.close()


def test_load_model_failure_sets_error_state(engine: Engine, tmp_path: Path, bus: events.EventBus):
    """模型不在时给的是可操作的错误，而不是一句 FileNotFoundError。"""
    assert engine.prepare() is True
    assert engine.load_model() is False
    assert engine.state == "error"
    assert engine.model_loaded is False
    bus.flush()
    assert any(e.level == "error" for e in bus.snapshot())


# --------------------------------------------------------------------------- #
# 文本链路
# --------------------------------------------------------------------------- #


def test_test_text_returns_outcome(engine: Engine):
    out = engine.test_text("打开记录本", dry_run=True)
    assert out is not None
    assert out.action_id == "open.notepad"
    assert out.ok


def test_test_text_updates_stats_and_emits(engine: Engine, bus: events.EventBus):
    engine.test_text("打开记事本", dry_run=True)
    bus.flush()
    assert engine.stats.outcomes == 1
    assert engine.stats.ok == 1
    assert engine.stats.last_action == "open.notepad"
    assert engine.stats.last_text == "打开记事本"
    kinds = [e.kind for e in bus.snapshot()]
    assert "outcome" in kinds


def test_unmatched_text_counts_as_no_match(engine: Engine, bus: events.EventBus):
    engine.test_text("随便说点什么", dry_run=True)
    bus.flush()
    assert engine.stats.no_match == 1
    assert engine.stats.ok == 0
    assert any(e.level == "warn" and e.kind == "outcome" for e in bus.snapshot())


def test_failed_execution_counts_as_error(engine: Engine):
    """dry-run 之外真的去执行一个找不到的程序，应记为失败。"""
    cfg = engine.cfg
    cfg.actions[0].target = "definitely-not-a-real-program-xyz.exe"
    cfg.actions[0].aliases = ["不存在的东西"]
    eng = Engine(cfg, bus=events.EventBus())
    try:
        eng.prepare()
        out = eng.test_text("开一下不存在的东西", dry_run=False)
        assert out is not None
        assert out.ok is False
        assert eng.stats.failed == 1
    finally:
        eng.close()


def test_outcome_event_carries_structured_fields(engine: Engine, bus: events.EventBus):
    engine.test_text("打开计算器", dry_run=True)
    bus.flush()
    ev = [e for e in bus.snapshot() if e.kind == "outcome"][-1]
    assert ev.data["action"] == "open.calc"
    assert ev.data["ok"] is True
    assert ev.data["via"] == "matcher"
    assert isinstance(ev.data["ms"], float)


# --------------------------------------------------------------------------- #
# 并发契约
# --------------------------------------------------------------------------- #


def test_submit_text_is_async(engine: Engine, bus: events.EventBus):
    engine.prepare()
    engine.submit_text("打开记事本", dry_run=True)
    assert engine.last_outcome is None, "submit 不该同步跑完"
    assert wait_for(lambda: engine.last_outcome is not None)
    assert engine.last_outcome is not None
    assert engine.last_outcome.action_id == "open.notepad"


def test_submit_audio_returns_immediately_even_when_asr_is_slow(engine: Engine, bus: events.EventBus):
    """这是整个 runner 存在的理由：ASR 跑在键盘钩子线程里会让 Windows 摘掉钩子。

    所以 submit_* 必须是"入队即返回"，无论识别多慢。
    """
    engine.prepare()
    engine._pipe.asr = StubAsr("打开记事本", delay=0.4)  # noqa: SLF001
    t0 = time.perf_counter()
    engine.submit_audio(np.zeros(16000, dtype=np.float32), dry_run=True)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"入队花了 {elapsed * 1000:.0f}ms，说明识别是在调用线程里跑的"
    assert engine.last_outcome is None
    assert wait_for(lambda: engine.last_outcome is not None, 5.0)


def test_audio_job_uses_transcribed_text(engine: Engine, bus: events.EventBus):
    engine.prepare()
    engine._pipe.asr = StubAsr("打开计算器")  # noqa: SLF001
    engine.submit_audio(np.zeros(16000, dtype=np.float32), dry_run=True)
    assert wait_for(lambda: engine.last_outcome is not None)
    assert engine.last_outcome is not None
    assert engine.last_outcome.text == "打开计算器"
    assert engine.last_outcome.action_id == "open.calc"
    assert engine.last_outcome.asr is not None


def test_jobs_run_in_order(engine: Engine, bus: events.EventBus):
    """串行执行保证「日志顺序 = 实际发生顺序」，排查时这是刚需。"""
    engine.prepare()
    for text in ("打开记事本", "打开计算器", "打开记录本"):
        engine.submit_text(text, dry_run=True)
    assert wait_for(lambda: engine.stats.outcomes == 3)
    bus.flush()
    got = [e.data.get("text") for e in bus.snapshot() if e.kind == "outcome"]
    assert got == ["打开记事本", "打开计算器", "打开记录本"]


def test_worker_survives_a_failing_job(engine: Engine):
    engine.prepare()
    boom = StubAsr("打开记事本")

    def explode(_samples, sample_rate: int = 16000):  # noqa: ANN001, ANN202
        raise RuntimeError("识别炸了")

    boom.transcribe = explode  # type: ignore[method-assign]
    engine._pipe.asr = boom  # noqa: SLF001
    engine.submit_audio(np.zeros(16000, dtype=np.float32), dry_run=True)
    time.sleep(0.3)
    # 工作线程必须还活着，能继续接受任务
    engine._pipe.asr = StubAsr("打开记事本")  # noqa: SLF001
    engine.submit_text("打开记事本", dry_run=True)
    assert wait_for(lambda: engine.last_outcome is not None)


def test_close_stops_worker(engine: Engine):
    engine.prepare()
    engine.close()
    assert engine._worker is None  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 热键
# --------------------------------------------------------------------------- #


def test_apply_hotkey_accepts_valid_spec(engine: Engine, bus: events.EventBus):
    assert engine.apply_hotkey("<ctrl>+<alt>+j") is True
    assert engine.hotkey_spec == "<ctrl>+<alt>+j"
    assert engine.cfg.hotkey.keys == "<ctrl>+<alt>+j"
    bus.flush()
    assert any(e.kind == "hotkey" and e.level == "ok" for e in bus.snapshot())


@pytest.mark.parametrize("bad", ["", "   ", "<ctrl>+<ctrl>"])
def test_apply_hotkey_rejects_bad_spec(engine: Engine, bus: events.EventBus, bad: str):
    before = engine.hotkey_spec
    assert engine.apply_hotkey(bad) is False
    assert engine.hotkey_spec == before, "失败的改动不该留下来"
    bus.flush()
    assert any(e.level == "error" and e.kind == "hotkey" for e in bus.snapshot())


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #


def test_preflight_for_known_and_unknown_action(engine: Engine):
    engine.prepare()
    ok = engine.preflight("open.notepad")
    assert ok.ok is True
    missing = engine.preflight("no.such.action")
    assert missing.ok is False
    assert "no.such.action" in missing.message


def test_status_line_contains_state_and_counts(engine: Engine):
    engine.prepare()
    engine.test_text("打开记事本", dry_run=True)
    line = engine.status_line()
    assert "未启动" in line
    assert "识别 1" in line


def test_toggle_without_model_still_starts_listener(engine: Engine):
    """没加载模型也应该能启动监听——第一次说话时再现场加载。"""
    engine.prepare()
    assert engine.stop() is None  # 没在跑时 stop 是安全的
    assert engine.tick() is False  # 没有会话时 tick 不炸


def test_dry_run_default_propagates(engine: Engine):
    engine.dry_run = True
    engine.prepare()
    out = engine.test_text("打开记事本")
    assert out is not None
    assert out.result is not None
    assert "[dry-run]" in out.result.message


def test_state_label_is_chinese(engine: Engine):
    assert engine.state_label == "未启动"
    assert engine.running is False
    assert engine.state == IDLE
    assert RUNNING == "running"
