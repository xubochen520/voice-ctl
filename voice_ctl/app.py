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
from .intent import APP, SCHEDULE, WEB, Intent, interpret
from . import lexicon
from .matcher import Match, Matcher
from .normalize import NormalizeConfig, Normalizer

# 各阶段耗时，用来定位「怎么变慢了」
STAGES = ("asr", "normalize", "intent", "match", "decision", "llm", "execute")

CLOSE_ACTION_ID = "sys.close_window"
"""兜底的关闭动作：用户说要关的东西不在已安装应用里（「关闭记事本」而记事本
没出现在开始菜单）。它按名字去关窗口，比"什么都不做"有用。"""

WEB_ACTION_ID = "open.web"
"""打开网页的动作 id。

和 `open.url` 的区别：`open.url` 的地址写在配置里（用户自己固定几个站），
`open.web` 的地址是**运行时解析出来的**（「打开百度」→ baidu.com），走
`ctx.slots["url"]`。同一条 handler，只是来源不同——和 open.app / open.target
那一对是一个道理。
"""

WEAK_MATCH = 0.85
"""意图层已经读懂、但解析不出对象时，还认不认别名匹配的结果。

不认它是为了避免"勉强命中"：实测说「打开QQ音乐」（没装）时，`open.browser`
会以 0.633 冒出来并**真的打开浏览器**。而达到 0.85 以上的匹配（比如
`alias-hit` 完整命中别名）仍然是可信的，照旧执行。
"""

COMPOUND_SEPS = ("并且", "然后再", "然后", "接着", "还有就是", "还有", "再打开", "顺便")
"""把一句话拆成两件事的连接词。

只有这几个。**「和」「跟」「以及」刻意不在里面**：它们出现在名字里的概率太高
（「微博和腾讯视频」是两个站，但「哔哩哔哩和它的朋友们」是一个名字），
拆错的代价是把一条正常指令劈成两条看不懂的片段。

实测来源：日志里那句「打开浏览器并且打开百度页面」原来只执行前半句，后半句
**没有任何提示地消失**——用户以为程序没听见，其实是听懂了但丢掉了。
"""

_JOIN_HINT = "和"
"""并回上一截时用的连接词。选「和」是因为它不在 COMPOUND_SEPS 里，不会再次被拆。"""


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

    steps: list[tuple[str, str, str]] = field(default_factory=list)
    """一句话里听出多件事时，每件事的 (说的什么, 动作, 结果)。

    单件事时是空的——那时 `report` 的常规几行已经说清了。"""

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

        # 一句话里有多件事时，逐条列出来，**不再重复报第一条的命中/执行**——
        # 上面逐条已经说清了，再报一遍会让日志长一倍，读起来还以为是两回事。
        if self.steps:
            lines.append(f"拆分  : 听出 {len(self.steps)} 件事 —— {self.note}")
            for i, (piece, action, detail) in enumerate(self.steps, 1):
                lines.append(f"  {i}. 「{piece}」")
                lines.append(f"     {action or '（没命中）'} —— {detail}" if action
                             else f"     {detail}")
            if verbose:
                lines.append(f"耗时  : {self.timing.summary()}")
            return "\n".join(lines)

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
        web_index=None,
        web_enabled: bool = True,
        web_search: bool = True,
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
        self.web_index = web_index
        """站点表（voice_ctl.web.WebIndex）。None = 用内置那份。"""
        self.web_enabled = web_enabled
        self.web_search = web_search
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

    def understand(
        self, text: str, timing: StageTiming, *, intent_enabled: bool | None = None
    ) -> tuple[Intent | None, Match | None, str, str]:
        """意图层 → 别名匹配 → 语义层。返回 (意图, 匹配, 途径, 说明)。

        语义层只在前两层都没结果时才**会被调用**——它加载 10 秒、推理 40-100ms，
        而且要额外占 873MB 内存，不该为一句「打开微信」付这个代价。
        """
        t0 = time.perf_counter()
        norm = self.norm.normalize(text)
        intent: Intent | None = None
        enabled = self.intent_enabled if intent_enabled is None else intent_enabled
        if enabled:
            try:
                intent = interpret(
                    text, index=self.app_index, actions=self.actions,
                    normalizer=self.norm, now=self.now,
                    web_index=self.web_index, web_enabled=self.web_enabled,
                    web_search=self.web_search,
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
            if intent.kind == WEB:
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
        """一个意图该落到哪个动作 id。

        配置里写过这条动作就用它——用户亲手配的 target/args 必须算数，
        不能因为应用索引也认得这个名字就改走别的路。
        """
        if intent.kind == WEB:
            # 网页只认 open.url。注册表里没有它就退回别名匹配——
            # 用户把这条动作删了是他的选择，不该被"帮"着执行别的动作。
            return WEB_ACTION_ID if WEB_ACTION_ID in self.registry else None
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

        日程走 `when`/`title`，开关应用走 `app`/`exe_names`，网页走 `url`——
        三种动作读的键不一样，所以按意图类型分开填，而不是硬塞进同一套名字里。
        """
        if intent.kind == SCHEDULE:
            out: dict[str, Any] = {"title": intent.title, "source": intent.source}
            if intent.when is not None:
                out["when"] = intent.when
            return out
        if intent.kind == WEB:
            return {
                "url": intent.url,
                "url_name": intent.url_name,
                "via_search": intent.via_search,
            }
        if intent.kind == APP and intent.app is not None:
            return {
                "app": intent.app,
                "app_name": intent.app.name,
                "exe_names": _exe_names(intent.app),
                # 完整路径要一起带上：只靠 exe 文件名关进程是危险的，见 _exe_paths
                "exe_paths": _exe_paths(intent.app),
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
        """只跑「文本 → 动作」，用于 simulate / 测试。

        一句里说了两件事时会拆成两条分别执行（`split_commands`）——实测
        「打开浏览器并且打开百度页面」原来只执行前半句，后半句**没有任何提示地
        消失**，用户以为程序没听见。
        """
        timing = StageTiming()
        out = Outcome(text=text, normalized=self.norm.normalize(text), timing=timing)
        if not out.normalized:
            out.note = "输入为空"
            return out

        parts = split_commands(out.normalized)
        if len(parts) > 1:
            self._run_each(parts, out, dry_run=dry_run)
            return out

        self._run_one(text, out, dry_run=dry_run, intent_enabled=self.intent_enabled)
        return out

    def _run_each(self, parts: list[str], out: Outcome, *, dry_run: bool) -> None:
        """把拆出来的几条依次执行，结果合并到同一个 Outcome 里。

        每条都在 intent/匹配之前**再验一遍**能不能读懂，读不懂就并回上一条
        （见 `split_commands` 的说明）——判定要用和真正执行时完全一样的那套
        逻辑，否则会出现"拆是拆开了，但那条根本执行不了"。
        """
        done: list[Outcome] = []
        pending = list(parts)
        while pending:
            piece = pending.pop(0)
            probe = Outcome(text=piece, normalized=piece, timing=StageTiming())
            self._run_one(piece, probe, dry_run=dry_run, intent_enabled=self.intent_enabled)
            if probe.action_id is None and probe.result is None and pending:
                # 这一截自己不是一条命令，多半是上一句还没说完（「打开记事本
                # 和计算器」里的「计算器」）。并回去重试。
                pending[0] = f"{piece}{_JOIN_HINT}{pending[0]}"
                continue
            done.append(probe)

        # 意图与命中取第一条（报告顶部那几行要有东西），执行结果列全
        first = done[0]
        out.intent, out.match, out.via = first.intent, first.match, first.via
        ok = [o for o in done if o.ok]
        bad = [o for o in done if not o.ok]
        if ok:
            out.action_id = ok[0].action_id
        elif bad:
            out.action_id = bad[0].action_id

        # 每一条的结果都留在报告里。只报第一条是不行的——用户说了两件事，
        # 报告里却只有一件，第二件是成是败他看不出来。`steps` 就是给报告看的。
        out.steps = [
            (o.text, o.action_id or "", o.result.describe() if o.result else (o.note or "没读懂"))
            for o in done
        ]
        out.note = f"这句话里听出 {len(done)} 件事" + (
            f"，{len(bad)} 件没做成" if bad else "，都做完了"
        )
        # 合成一个总结果：任何一步失败，这次就算部分失败——不能让用户以为全成了
        if bad and ok:
            out.result = ActionResult(
                False,
                f"{len(ok)} 件成功，{len(bad)} 件没做成",
                "；".join(f"{t}：{d}" for t, _, d in out.steps),
            )
        elif bad:
            out.result = ActionResult(
                False, "都没做成", "；".join(f"{t}：{d}" for t, _, d in out.steps)
            )
        else:
            out.result = ActionResult(
                True,
                "、".join(o.result.message for o in done if o.result),
                "；".join(f"{t}：{d}" for t, _, d in out.steps),
            )
        for o in done:
            if o.timing.values:
                for k, v in o.timing.values.items():
                    out.timing.add(k, v)

    def _run_one(
        self, text: str, out: Outcome, *, dry_run: bool, intent_enabled: bool | None = None
    ) -> None:
        """一条命令的完整链路：意图层 → 别名匹配 → 语义层 → 执行。

        `intent_enabled` 是按**这一次调用**传的，不去改 `self.intent_enabled`：
        后者是共享状态，而拆句会连着跑好几条，中途改共享状态等于给自己埋并发坑。
        """
        timing = out.timing
        intent, match, via, note = self.understand(
            text, timing, intent_enabled=intent_enabled
        )
        out.intent, out.match, out.via, out.note = intent, match, via, note

        if intent is not None and intent.kind == "none":
            out.via = "intent"
            out.note = intent.why or "这句话不是在要求执行什么"
            if intent.polarity == "negate":
                return
            # 解析不出对象（「打开QQ音乐」而没装）：别名匹配也过一遍。
            # 用户可能写过一条 [[action]] 用别的名字绑定了它。
            #
            # 只有**像样的**匹配才认。旧写法是"只要过了阈值就用"，实测说
            # 「打开QQ音乐」时 `open.browser` 会以 0.633 冒出来，而它其实
            # 什么都打不开（目标里没有 QQ 音乐）。这种时候用户要的是
            # 「没找到叫QQ音乐的应用」，不是"已打开浏览器"。
            m = self.matcher.best(out.normalized)
            if m is None or not m.action_id or m.action_id not in self.registry:
                return
            if m.score < WEAK_MATCH:
                out.note += f"（别名匹配最接近的是 {m.action_id}，{m.score:.2f}，太弱没执行）"
                return
            match = m
            match.raw_input = text
            out.match = match

        if match is None:
            return
        if not match.action_id:
            out.note = "意图层读懂了这句话，但没有能执行它的动作"
            return

        if match.action_id not in self.registry:
            out.note = f"匹配到 {match.action_id}，但注册表里没有它"
            return
        out.action_id = match.action_id
        out.result = self.execute(
            match.action_id, match, timing, dry_run=dry_run, intent=intent
        )

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


def split_commands(text: str) -> list[str]:
    """一句话里说了几件事就切几段。只有一段时原样返回。

    刻意做得**很保守**：只按少数几个明确的连接词切（见 COMPOUND_SEPS），
    切完还要每一段都像一条命令（以开关动词开头）才认。理由是这个函数的失败
    方式不对称——

      * 少切一刀：后半句没执行，但前面那句是对的，用户至少看到了部分效果；
      * 多切一刀：把一条正常指令劈成两条读不懂的片段，**两件事都做不成**。

    所以宁可少切。「打开记事本和计算器」这种（连接词不在表里 + 后半段不像命令）
    会原样返回，交给下游按一条处理，行为和以前一致。
    """
    s = text.strip()
    if not s:
        return []
    pieces = [s]
    for sep in sorted(COMPOUND_SEPS, key=len, reverse=True):
        grown: list[str] = []
        for p in pieces:
            grown.extend(p.split(sep))
        pieces = grown
    parts = [lexicon.strip_punct(p) for p in pieces]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [s]
    # 第二段起必须像一条独立命令，否则整句不拆
    for p in parts[1:]:
        if not any(p.startswith(v) for v in _COMMAND_HEADS):
            return [s]
    return parts


_COMMAND_HEADS = tuple(
    sorted(
        {
            *(v for v in lexicon.OPEN_VERBS if len(v) > 1),
            *(v for v in lexicon.CLOSE_VERBS if len(v) > 1),
            *lexicon.FORCE_VERBS,
            "提醒我", "叫我", "安排", "设个", "设置", "记一下",
        },
        key=len,
        reverse=True,
    )
)
"""一段话"像不像命令"的判据：句首是不是开关/日程动词。"""


def _match_from_intent(intent: Intent, norm: str, text: str, action_id: str) -> Match:
    """把意图包装成 Match，让下游（日志、执行、统计）走同一条路。

    score 写 1.0、strategy 写 `intent:<polarity>`：报告里一眼能看出这句是
    意图层判的、还是别名匹配判的。
    """
    alias = intent.app.name if intent.app is not None else (intent.title or "(日程)")
    if intent.kind == WEB:
        alias = intent.url_name or intent.url
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


def _exe_paths(app) -> list[str]:  # noqa: ANN001 - intent.ResolvedApp
    """完整 exe 路径（能拿到就给）。关闭应用**优先**按它匹配进程。

    为什么不能只靠进程名：实测这台机器上三个不同的启动器都叫 `launcher.exe`
    （米哈游 / 鸣潮 / 鹰角）。`taskkill /IM launcher.exe` 会把三个一起关掉——
    用户说「关闭米哈游启动器」，鸣潮和鹰角跟着消失。
    """
    return [p for p in (getattr(app, "exe_path", ""),) if p]

