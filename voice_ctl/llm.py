"""可选的小模型层：用**本机跑着的大模型服务**兜住前几层都读不懂的话。

前几层是：意图层（动词/否定/时间）→ 别名匹配 → 动态应用词典。它们覆盖的是
"说得清楚"的指令。剩下的长尾是这类：

    「有个文件要改一下」        想开记事本，但一个名字都没提
    「把声音关小」              想调音量
    「待会儿提醒我拿快递」      「待会儿」没有具体时间

用一个通用大模型去补这一层，比训练分类器合适——这些句子的共同点是**缺字**，
不是缺类别。

三条刻意的约束：

  1. **只抽片段，不做决定，更不执行。** 模型返回的 JSON 里只有两个字段：
     候选动作的 id 和标题。时间由 `timeparse` 解析，动作由注册表执行。
     模型说"打开 rm -rf /"这种话没有意义，因为它根本没有那个字段可以填。
  2. **只在前面几层都没结果时才被调用。** 本机模型服务（LM Studio / Ollama）
     一次推理要几百毫秒到几秒，还可能没启动。为一句「打开微信」付这个代价
     是荒唐的。
  3. **服务不可用等于功能不存在，而不是功能报错。** 用户没装 LM Studio 是
     常态。探测失败就静默返回 None，日志里留一行——绝不能让助手因为
     "模型服务连不上"而无法执行本来能执行的指令。

刻意只用标准库（urllib）而不是 openai 包：这一层是可选的，不该为了它给
整个项目加一个运行时依赖。协议就是 HTTP + JSON，够用。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://127.0.0.1:1234/v1"
"""LM Studio 的默认监听地址。这台机器上装的就是它（lms.exe 在 PATH 里）。

Ollama 用户把它改成 http://127.0.0.1:11434/v1 即可——两家都实现了
OpenAI 的 /chat/completions 协议，所以这一层不需要为它们写两份代码。
"""

SYSTEM_PROMPT = (
    "你是一个语音指令理解器。用户说一句中文口令，你要在给定的候选动作里挑一个。\n"
    "只输出一行 JSON，不要解释，不要代码块标记：\n"
    '{"action_id": "候选里的id，挑不出就写 null", "title": "如果是日程/提醒，这里写要做的事，否则空串"}\n'
    "规则：\n"
    "1. action_id 必须是候选列表里出现过的 id，不许自己编。\n"
    "2. 候选里没有能表达用户意思的，action_id 写 null。宁可说不知道，不要瞎猜。\n"
    "3. title 只放用户真正要做的那件事，去掉「提醒我」「帮我」这类壳子。"
)


@dataclass
class Suggestion:
    """模型给的建议。**建议**，不是命令——调用方仍要校验 action_id 在不在注册表里。"""

    action_id: str | None
    title: str = ""
    raw: str = ""
    """模型的原样输出，出问题时用来诊断。"""

    ms: float = 0.0


class LLMUnavailable(RuntimeError):
    """服务连不上 / 没配模型。调用方应当**安静地**退回前几层。"""


def build_prompt(text: str, candidates: list[tuple[str, str]]) -> str:
    """把候选动作写成模型能读的清单。

    `candidates` 是 (id, 描述) 而不是全部别名——描述是人写的、语义完整
    （「打开微信，用来聊天、发消息」），比一串别名更好判断。候选**必须由
    调用方筛过**：把上百个动作全塞进去，小模型会开始乱挑。
    """
    lines = [f"- {aid}：{desc}" for aid, desc in candidates]
    return f"候选动作：\n" + "\n".join(lines) + f"\n\n用户说：「{text}」"


def parse_reply(raw: str) -> Suggestion:
    """从模型输出里抠出 JSON。

    必须容错：小模型经常会加代码块标记、加解释、或者把 JSON 包在句子里。
    照着"理想输出"写解析，实际用起来会一直是"模型没返回有效结果"。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        # 去掉 ```json ... ``` 这类围栏
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return Suggestion(None, raw=raw)
    try:
        data = json.loads(text[start: end + 1])
    except ValueError:
        return Suggestion(None, raw=raw)
    if not isinstance(data, dict):
        return Suggestion(None, raw=raw)
    aid = data.get("action_id")
    title = data.get("title")
    return Suggestion(
        aid if isinstance(aid, str) and aid.strip() else None,
        title.strip() if isinstance(title, str) else "",
        raw=raw,
    )


class SlotExtractor:
    """问本机的大模型服务要一个候选动作。"""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = "",
        *,
        timeout: float = 4.0,
        max_tokens: int = 120,
        temperature: float = 0.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model.strip()
        self.timeout = timeout
        self.max_tokens = max_tokens
        # 抽槽位要的是**稳定**，不是创造力。同一句话问两次给两个答案的助手没法用。
        self.temperature = temperature
        self.last_error = ""
        self.available: bool | None = None
        """None = 还没探过。探过之后缓存结论，避免每句话都等一次超时。"""

    # -- 探测 ------------------------------------------------------------- #

    def probe(self, *, force: bool = False) -> bool:
        """服务在不在。探过一次就记住——每次说话都先等 4 秒超时是不可接受的。"""
        if self.available is not None and not force:
            return self.available
        try:
            req = urllib.request.Request(f"{self.endpoint}/models", method="GET")
            with urllib.request.urlopen(req, timeout=min(self.timeout, 2.0)) as r:  # noqa: S310
                body = json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.available = False
            return False
        self.available = True
        if not self.model:
            # 没指定模型时用服务上第一个——LM Studio 通常只加载一个
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, list) and data and isinstance(data[0], dict):
                self.model = str(data[0].get("id") or "")
        return True

    # -- 抽槽位 ----------------------------------------------------------- #

    def suggest(
        self, text: str, candidates: list[tuple[str, str]]
    ) -> Suggestion | None:
        """让模型在候选里挑一个。服务不可用、超时、输出解析不了都返回 None。

        **永不抛异常**：这一层是锦上添花，它挂掉不该让整句话执行不了。
        """
        if not candidates:
            return None
        if not self.probe():
            return None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(text, candidates)},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        import time

        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                body = json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            # 连接被拒通常意味着服务关了：把探测结论改掉，后面几句不再白等
            self.available = False
            _log.info("小模型层不可用：%s", self.last_error)
            return None

        ms = (time.perf_counter() - t0) * 1000
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.last_error = f"返回结构不认识：{str(body)[:200]}"
            return None
        s = parse_reply(str(content))
        s.ms = ms
        return s

    def describe(self) -> str:
        if self.available is None:
            state = "未探测"
        elif self.available:
            state = f"可用（{self.model or '默认模型'}）"
        else:
            state = f"不可用（{self.last_error or '连不上'}）"
        return f"{self.endpoint}  {state}"


def candidate_actions(actions: list[Any], limit: int = 12) -> list[tuple[str, str]]:
    """给模型看的候选清单：id + 描述（没有描述就用别名凑）。

    `limit` 是有意的：小模型的判断力随候选数量迅速下降，官方 benchmark 里
    20 选项就已经掉到 0.45。宁可只给它最像的一小撮。
    """
    out: list[tuple[str, str]] = []
    for a in actions[:limit]:
        if not getattr(a, "enabled", True):
            continue
        desc = (getattr(a, "describe", "") or "").strip()
        if not desc:
            aliases = getattr(a, "aliases", None) or []
            desc = "、".join(str(x) for x in aliases[:4]) or a.id
        out.append((a.id, desc))
    return out


__all__ = [
    "DEFAULT_ENDPOINT",
    "SYSTEM_PROMPT",
    "LLMUnavailable",
    "SlotExtractor",
    "Suggestion",
    "build_prompt",
    "candidate_actions",
    "parse_reply",
]
