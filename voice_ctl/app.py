"""pipeline：热键 → 录音 → 识别 → 归一化 → 匹配 → 执行。

把这条链单独抽出来，是为了能脱离麦克风和热键做端到端测试
（见 `voice-ctl simulate "打开微信"`）——否则每次改逻辑都得对着麦克风喊。

判定顺序是**意图层在前，别名匹配在后**：

    意图层能读懂  → 用它（「关闭微信」要关，不是开；「打开QQ」用已安装应用索引）
    意图层读不懂  → 落到别名匹配（行为与 0.2.0 一致，一个动作都没少）
    都没结果      → 语义层（可选，Laya）

顺序不能反。别名匹配对动词和否定完全无感，让它先跑的话「关闭微信」会在意图层
看到之前就被判成"打开微信"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .actions import ActionResult, ActionContext, Registry
from .apps import AppEntry, AppIndex, process_hints
from .asr import Asr, AsrError, AsrResult
from .config import ActionConfig
from .intent import APP, SCHEDULE, Intent, interpret
from .matcher import Match, Matcher
from .normalize import NormalizeConfig, Normalizer

# 各阶段耗时，用来定位「怎么变慢了」
STAGES = ("asr", "normalize", "intent", "match", "decision", "llm", "execute")

CLOSE_ACTION_ID = "sys.close_window"
"""兜底的关闭动作：用户说要关的东西不在已安装应用里（「关闭记事本」而记事本
没出现在开始菜单）。它按名字去关窗口，比"什么都不做"有用。"""

WEAK_MATCH = 0.85
"""意图层已经读懂、但解析不出对象时，还认不认别名匹配的结果。

不认它是为了避免"勉强命中"：实测说「打开QQ音乐」（没装）时，`open.browser`
会以 0.633 冒出来并**真的打开浏览器**。而达到 0.85 以上的匹配（比如
`alias-hit` 完整命中别名）仍然是可信的，照旧执行。
"""


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
    """命中途径：intent / matcher / decision / none"""

    action_id: str | None = None
    result: ActionResult | None = None
    timing: StageTiming = field(default_factory=StageTiming)
    note: str = ""
    intent: Intent | None = None
    """意图层的判断结果。有它就能解释「为什么是关闭而不是打开」。"""

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
        if self.intent is not None and self.intent.why:
            lines.append(f"意图  : {self.intent.kind}/{self.intent.polarity} —— {self.intent.why}")
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
        app_index: AppIndex | None = None,
        intent_enabled: bool = True,
        llm=None,
        llm_candidates: int = 12,
        clock=None,
    ) -> None:
        self.asr = asr
        self.matcher = matcher
        self.registry = registry
        self.actions = actions
        self.norm = normalizer or Normalizer()
        self.decider = decider
        self.min_confidence = min_confidence
        self._log = log
        self.app_index = app_index
        self.intent_enabled = intent_enabled
        self.llm = llm
        """可选的小模型层（voice_ctl.llm.SlotExtractor）。None = 没开。"""
        self.llm_candidates = llm_candidates
        self._clock = clock
        """可注入的"现在"。测试用它固定时间，避免依赖系统时钟。"""

    @property
    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now()

    def close(self) -> None:
        """收掉小模型层起的子进程。

        内置 llama.cpp 会在第一次推理时起一个 llama-server。**不显式收掉的话，
        它会活到用户重启**——一个占着几百 MB 内存、在 127.0.0.1 上监听的孤儿
        进程，比这个功能不存在更糟。
        """
        if self.llm is not None:
            close = getattr(self.llm, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 收尾失败不该影响退出
                    pass

    # -- 各阶段 ----------------------------------------------------------- #

    def transcribe(self, samples, timing: StageTiming, sample_rate: int = 16000) -> AsrResult:
        t0 = time.perf_counter()
        res = self.asr.transcribe(samples, sample_rate=sample_rate)
        timing.add("asr", (time.perf_counter() - t0) * 1000)
        return res

    def understand(self, text: str, timing: StageTiming) -> tuple[Intent | None, Match | None, str, str]:
        """意图层 → 别名匹配 → 语义层。返回 (意图, 匹配, 途径, 说明)。

        语义层只在前两层都没结果时才**会被调用**——它加载 10 秒、推理 40-100ms，
        而且要额外占 873MB 内存，不该为一句「打开微信」付这个代价。
        """
        t0 = time.perf_counter()
        norm = self.norm.normalize(text)
        intent: Intent | None = None
        if self.intent_enabled:
            try:
                intent = interpret(
                    text, index=self.app_index, actions=self.actions,
                    normalizer=self.norm, now=self.now,
                )
            except Exception as e:  # noqa: BLE001 - 意图层出错不该让助手失能
                intent = None
                if self._log:
                    self._log.warning("意图层出错，回落到别名匹配：%s", e)
        timing.add("normalize", (time.perf_counter() - t0) * 1000)

        if intent is not None:
            if intent.kind == SCHEDULE and "schedule" in self.registry:
                return intent, _match_from_intent(intent, norm, text, "schedule"), "intent", ""
            if intent.kind == APP and intent.app is not None:
                aid = self.action_for(intent)
                if aid is not None:
                    return intent, _match_from_intent(intent, norm, text, aid), "intent", ""
            # 意图层读懂了，但注册表里没有能执行它的动作（用户把 open.target /
            # schedule 删了）。**不能就此返回空**——那等于因为少一条 [[action]]
            # 把整句话丢掉。往下跌一层，让别名匹配按老规矩试一试。
            if self._log:
                self._log.debug("意图层读懂了但没有对应动作，回落到别名匹配：%s", intent.kind)

        t1 = time.perf_counter()
        m = self.matcher.best(norm)
        timing.add("match", (time.perf_counter() - t1) * 1000)
        if m is not None:
            m.raw_input = text
            return intent, m, "matcher", ""

        if self.decider is None and self.llm is None:
            return intent, None, "none", self._miss_note(intent)

        if self.decider is not None:
            t2 = time.perf_counter()
            try:
                d = self.decider.decide(norm)
            except Exception as e:  # noqa: BLE001 - 语义层失败不该拖垮主流程
                timing.add("decision", (time.perf_counter() - t2) * 1000)
                d = None
                note = f"语义层出错：{e}"
            else:
                timing.add("decision", (time.perf_counter() - t2) * 1000)
                note = ""
            if d is not None:
                if not d.action_id:
                    return intent, None, "none", "语义层没有给出动作"
                if d.low_confidence:
                    return intent, None, "none", (
                        f"语义层选择 {d.action_id} 但置信度 {d.confidence:.2f} "
                        f"低于阈值 {self.min_confidence}，未执行"
                    )
                # 语义层的结论包装成 Match，让下游统一处理
                return (
                    intent,
                    Match(d.action_id, d.confidence, "(语义)", "semantic",
                          normalized_input=norm, raw_input=text),
                    "decision",
                    "",
                )
        else:
            note = ""

        # 最后一层：本机小模型。只在前面几层都没结果时被调用——它要等一次
        # HTTP 往返，不该为「打开微信」付这个代价。
        if self.llm is not None:
            t3 = time.perf_counter()
            sug = None
            try:
                from .llm import candidate_actions

                sug = self.llm.suggest(text, candidate_actions(self.actions, self.llm_candidates))
            except Exception:  # noqa: BLE001 - 这一层是锦上添花，挂了也不能影响执行
                sug = None
            timing.add("llm", (time.perf_counter() - t3) * 1000)
            if sug is not None and sug.action_id and sug.action_id in self.registry:
                return (
                    intent,
                    Match(sug.action_id, 1.0, "(小模型)", "llm",
                          normalized_input=norm, raw_input=text),
                    "llm",
                    "",
                )
            if sug is not None and sug.action_id:
                # 模型编了一个不存在的 id。说出来，别静默丢弃——这说明它的
                # 判断力不够，用户该知道。
                note = f"小模型建议了不存在的动作 {sug.action_id!r}，已忽略"
        return intent, None, "none", note or self._miss_note(intent)

    @staticmethod
    def _miss_note(intent: Intent | None) -> str:
        """都没命中时给用户的那句话。意图层的解释比"别名没命中"具体得多。"""
        if intent is not None and intent.why:
            return intent.why
        if intent is not None and intent.polarity == "open":
            return "解析不出要打开什么"
        return "别名没命中，且语义层未启用"

    def action_for(self, intent: Intent) -> str | None:
        """一个应用意图该落到哪个动作 id。

        配置里写过这条动作就用它——用户亲手配的 target/args 必须算数，
        不能因为应用索引也认得这个名字就改走别的路。
        """
        app = intent.app
        if app is None:
            return None
        if app.action_id:
            return app.action_id
        if intent.polarity in ("close", "force"):
            if "sys.close_app" in self.registry:
                return "sys.close_app"
            return CLOSE_ACTION_ID if CLOSE_ACTION_ID in self.registry else None
        if "open.target" in self.registry:
            return "open.target"
        return "open.app" if "open.app" in self.registry else None

    def slots_for(self, intent: Intent, match: Match) -> dict[str, Any]:
        """把意图翻译成动作槽位。

        日程走 `when`/`title`，开关应用走 `app`/`exe_names`——两种动作读的键不一样，
        所以按意图类型分开填，而不是硬塞进同一套名字里。
        """
        if intent.kind == SCHEDULE:
            out: dict[str, Any] = {"title": intent.title, "source": intent.source}
            if intent.when is not None:
                out["when"] = intent.when
            return out
        if intent.kind == APP and intent.app is not None:
            return {
                "app": intent.app,
                "app_name": intent.app.name,
                "exe_names": _exe_names(intent.app),
                "force": intent.polarity == "force",
            }
        return {}

    def _intent_keys(self, intent: Intent) -> tuple[str, str]:
        """意图槽位该进哪个袋子。

        日程 handler 读 `ctx.slots`（它本来就是"从模式里抓到的槽位"那个语义），
        开关应用的 handler 读 `ctx.extra`。两个袋子都填上，是为了让同一份槽位
        不管动作从哪边读都能拿到——少填一边的表现是"动作跑起来了但参数是空的"，
        这种缺陷很难从日志上看出来。
        """
        return ("slots", "extra") if intent.kind == SCHEDULE else ("extra", "slots")

    def execute(
        self, action_id: str, match: Match, timing: StageTiming, *,
        dry_run: bool = False, intent: Intent | None = None,
    ) -> ActionResult:
        action = self.registry.get(action_id)
        if action is None:
            return ActionResult(False, f"动作 {action_id} 不在注册表里")
        ctx = ActionContext(
            text=match.raw_input or match.normalized_input,
            normalized=match.normalized_input,
            matched_alias=match.alias,
            dry_run=dry_run,
        )
        if intent is not None:
            kv = self.slots_for(intent, match)
            primary, secondary = self._intent_keys(intent)
            getattr(ctx, primary).update(kv)
            getattr(ctx, secondary).update(kv)
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

        intent, match, via, note = self.understand(text, timing)
        out.intent, out.match, out.via, out.note = intent, match, via, note

        if intent is not None and intent.kind == "none":
            out.via = "intent"
            out.note = intent.why or "这句话不是在要求执行什么"
            if intent.polarity == "negate":
                return out
            # 解析不出对象（「打开QQ音乐」而没装）：别名匹配也过一遍。
            # 用户可能写过一条 [[action]] 用别的名字绑定了它。
            #
            # 只有**像样的**匹配才认。旧写法是"只要过了阈值就用"，实测说
            # 「打开QQ音乐」时 `open.browser` 会以 0.633 冒出来，而它其实
            # 什么都打不开（目标里没有 QQ 音乐）。这种时候用户要的是
            # 「没找到叫QQ音乐的应用」，不是"已打开浏览器"。
            m = self.matcher.best(out.normalized)
            if m is None or not m.action_id or m.action_id not in self.registry:
                return out
            if m.score < WEAK_MATCH:
                out.note += f"（别名匹配最接近的是 {m.action_id}，{m.score:.2f}，太弱没执行）"
                return out
            match = m
            match.raw_input = text
            out.match = match

        if match is None:
            return out
        if not match.action_id:
            out.note = "意图层读懂了这句话，但没有能执行它的动作"
            return out

        if match.action_id not in self.registry:
            out.note = f"匹配到 {match.action_id}，但注册表里没有它"
            return out
        out.action_id = match.action_id
        out.result = self.execute(
            match.action_id, match, timing, dry_run=dry_run, intent=intent
        )
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


def _match_from_intent(intent: Intent, norm: str, text: str, action_id: str) -> Match:
    """把意图包装成 Match，让下游（日志、执行、统计）走同一条路。

    score 写 1.0、strategy 写 `intent:<polarity>`：报告里一眼能看出这句是
    意图层判的、还是别名匹配判的。
    """
    alias = intent.app.name if intent.app is not None else (intent.title or "(日程)")
    return Match(
        action_id, 1.0, alias, f"intent:{intent.polarity}",
        normalized_input=norm, raw_input=text,
    )


def _exe_names(app) -> list[str]:  # noqa: ANN001 - intent.ResolvedApp
    """从解析结果推出"它跑起来是哪个进程名"。

    复用 apps.process_hints：exe 路径取文件名、UWP 的 `包名!应用` 取包族名、
    系统自带应用（notepad）用命令名。三个来源都要，否则关闭时找不到进程。
    """
    entry = AppEntry(name=app.name, appid=app.appid, system=app.system)
    exes, pfn = process_hints(entry)
    out = sorted(exes)
    if not out and pfn:
        # UWP：只能按包族名找进程，进程名不一定是应用名（计算器是 CalculatorApp.exe）
        out.append(f"pfn:{pfn}")
    return out

