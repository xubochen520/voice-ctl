"""pipeline：热键 → 录音 → 识别 → 归一化 → 匹配 → 执行。

把这条链单独抽出来，是为了能脱离麦克风和热键做端到端测试
（见 `voice-ctl simulate "打开微信"`）——否则每次改逻辑都得对着麦克风喊。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .actions import ActionResult, ActionContext, Registry
from .asr import Asr, AsrError, AsrResult
from .config import ActionConfig
from .matcher import Match, Matcher
from .normalize import NormalizeConfig, Normalizer

# 各阶段耗时，用来定位「怎么变慢了」
STAGES = ("asr", "normalize", "match", "decision", "execute")


@dataclass
class StageTiming:
    values: dict[str, float] = field(default_factory=dict)

    def add(self, stage: str, ms: float) -> None:
        self.values[stage] = self.values.get(stage, 0.0) + ms

    @property
    def total_ms(self) -> float:
        return sum(self.values.values())

    def summary(self) -> str:
        parts = [f"{k}={v:.0f}ms" for k, v in self.values.items() if v > 0]
        return f"总 {self.total_ms:.0f}ms  (" + ", ".join(parts) + ")" if parts else "无耗时记录"


@dataclass
class Outcome:
    """一次完整判断的结果。不管成功失败都返回它，便于诊断。"""

    text: str = ""
    normalized: str = ""
    asr: AsrResult | None = None
    match: Match | None = None
    via: str = "none"
    """命中途径：matcher / decision / none"""

    action_id: str | None = None
    result: ActionResult | None = None
    timing: StageTiming = field(default_factory=StageTiming)
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.result and self.result.ok)

    def report(self, *, verbose: bool = True) -> str:
        if not self.text:
            return "（没听到内容）"
        lines = [f'听到  : "{self.text}"']
        if verbose and self.normalized and self.normalized != self.text:
            lines.append(f"归一化: {self.normalized}")
        if self.asr:
            lines.append(f"ASR   : {self.asr.summary()}")
        if self.action_id:
            how = f"{self.via}"
            if self.match:
                how += f" (score={self.match.score:.2f} {self.match.strategy} alias={self.match.alias!r})"
            lines.append(f"命中  : {self.action_id}  via {how}")
        elif self.note:
            lines.append(f"未命中: {self.note}")
        if self.result:
            lines.append(f"执行  : {self.result.describe()}")
        if verbose:
            lines.append(f"耗时  : {self.timing.summary()}")
        return "\n".join(lines)


class Pipeline:
    """串起整条链的载体。Asr / Matcher / Registry 都只构建一次。"""

    def __init__(
        self,
        *,
        asr: Asr,
        matcher: Matcher,
        registry: Registry,
        actions: list[ActionConfig],
        normalizer: Normalizer | None = None,
        decider=None,
        min_confidence: float = 0.6,
        log=None,
    ) -> None:
        self.asr = asr
        self.matcher = matcher
        self.registry = registry
        self.actions = actions
        self.norm = normalizer or Normalizer()
        self.decider = decider
        self.min_confidence = min_confidence
        self._log = log

    # -- 各阶段 ----------------------------------------------------------- #

    def transcribe(self, samples, timing: StageTiming, sample_rate: int = 16000) -> AsrResult:
        t0 = time.perf_counter()
        res = self.asr.transcribe(samples, sample_rate=sample_rate)
        timing.add("asr", (time.perf_counter() - t0) * 1000)
        return res

    def resolve(self, text: str, timing: StageTiming) -> tuple[Match | None, str, str]:
        """决定该执行哪个动作。返回 (匹配, 途径, 说明)。"""
        t0 = time.perf_counter()
        norm = self.norm.normalize(text)
        m = self.matcher.best(norm)
        timing.add("normalize", (time.perf_counter() - t0) * 1000)

        if m is not None:
            return m, "matcher", ""

        if self.decider is None:
            return None, "none", "别名没命中，且语义层未启用"

        t1 = time.perf_counter()
        try:
            d = self.decider.decide(norm)
        except Exception as e:  # noqa: BLE001 - 语义层失败不该拖垮主流程
            timing.add("decision", (time.perf_counter() - t1) * 1000)
            return None, "none", f"语义层出错：{e}"
        timing.add("decision", (time.perf_counter() - t1) * 1000)

        if not d.action_id:
            return None, "none", "语义层没有给出动作"
        if d.low_confidence:
            return None, "none", (
                f"语义层选择 {d.action_id} 但置信度 {d.confidence:.2f} "
                f"低于阈值 {self.min_confidence}，未执行"
            )
        # 语义层的结论包装成 Match，让下游统一处理
        return (
            Match(d.action_id, d.confidence, "(语义)", "semantic", normalized_input=norm),
            "decision",
            "",
        )

    def execute(self, action_id: str, match: Match, timing: StageTiming, *, dry_run: bool = False) -> ActionResult:
        action = self.registry.get(action_id)
        if action is None:
            return ActionResult(False, f"动作 {action_id} 不在注册表里")
        ctx = ActionContext(
            text=match.normalized_input,
            normalized=match.normalized_input,
            matched_alias=match.alias,
            dry_run=dry_run,
        )
        t0 = time.perf_counter()
        try:
            res = action.execute(ctx)
        except Exception as e:  # noqa: BLE001 - 动作异常不该让助手崩掉
            res = ActionResult(False, f"动作抛出异常：{type(e).__name__}: {e}")
        timing.add("execute", (time.perf_counter() - t0) * 1000)
        return res

    # -- 端到端 ----------------------------------------------------------- #

    def process_text(self, text: str, *, dry_run: bool = False) -> Outcome:
        """只跑「文本 → 动作」，用于 simulate / 测试。"""
        timing = StageTiming()
        out = Outcome(text=text, normalized=self.norm.normalize(text), timing=timing)
        if not out.normalized:
            out.note = "输入为空"
            return out

        match, via, note = self.resolve(text, timing)
        out.match, out.via, out.note = match, via, note
        if match is None:
            return out

        if match.action_id not in self.registry:
            out.note = f"匹配到 {match.action_id}，但注册表里没有它"
            return out
        out.action_id = match.action_id
        out.result = self.execute(match.action_id, match, timing, dry_run=dry_run)
        return out

    def process_audio(self, samples, *, dry_run: bool = False, sample_rate: int = 16000) -> Outcome:
        """完整链路：波形 → 动作。"""
        timing = StageTiming()
        try:
            asr_res = self.transcribe(samples, timing, sample_rate)
        except AsrError as e:
            return Outcome(timing=timing, note=f"识别失败：{e}")

        if not asr_res.text:
            return Outcome(asr=asr_res, timing=timing, note="识别结果为空（可能没说话）")

        out = self.process_text(asr_res.text, dry_run=dry_run)
        # 把 ASR 阶段的耗时并进来，保持总耗时完整
        for k, v in timing.values.items():
            out.timing.add(k, v)
        out.asr = asr_res
        return out
