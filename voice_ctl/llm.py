"""可选的小模型层：用**本地跑着的大模型**兜住前几层都读不懂的话。

前几层是：意图层（动词/否定/时间）→ 别名匹配 → 动态应用词典。它们覆盖的是
"说得清楚"的指令。剩下的长尾是这类：

    「有个文件要改一下」        想开记事本，但一个名字都没提
    「把声音关小」              想调音量
    「待会儿提醒我拿快递」      「待会儿」没有具体时间

用一个通用大模型去补这一层，比训练分类器合适——这些句子的共同点是**缺字**，
不是缺类别。

## 两种后端

  内置（默认，`backend = "local"`）
      自己下 llama.cpp 的 llama-server 和一个小 GGUF 模型（见 llamacpp.py）。
      打包后的应用**不需要任何外部软件**，这是这一版的重点。
  外部（`backend = "server"`）
      接一个已经在跑的服务：LM Studio（1234）、Ollama（11434）、llama.cpp
      自己起的 llama-server。协议都是 OpenAI 的 /chat/completions。

两者行为一致，`make_client()` 按配置挑一个。

## 三条刻意的约束

  1. **只抽片段，不做决定，更不执行。** 模型能填的只有"选第几个候选"和"标题"。
     时间由 `timeparse` 解析、动作由注册表执行。模型说"打开 rm -rf /"没有意义，
     因为它根本没有那个字段可以填。
  2. **只在前面几层都没结果时才被调用。** 本地小模型一次推理 60-350ms，加载
     还要几秒。为一句「打开微信」付这个代价是荒唐的。
  3. **不可用等于功能不存在，而不是功能报错。** 没下模型、没装运行时都是常态。
     探测失败就静默返回 None，日志里留一行——绝不能让助手因为"小模型没准备好"
     而无法执行本来能执行的指令。

## 关于"编造动作 id"

这一层唯一的真实危险是模型吐出一个不存在的动作。两道锁：

  * **语法约束**（llama.cpp 的 GBNF）：解码时只允许输出候选编号。实测 20 次
    八竿子打不着的输入，**一次都没越界**——这不是"提示词里求它别乱说"能比的。
  * **注册表校验**：即便没有语法（外部服务走的是普通 JSON），回来的 id 也必须
    在注册表里，否则丢弃并说出来。

只用标准库（urllib）而不是 openai 包：这一层是可选的，不该为它给整个项目加
一个运行时依赖。协议就是 HTTP + JSON，够用。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

_log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://127.0.0.1:1234/v1"
"""外部服务的默认地址。LM Studio 的默认监听端口就是这个。

Ollama 用户改成 http://127.0.0.1:11434/v1 即可——两家都实现了 OpenAI 的
/chat/completions 协议，所以不需要为它们写两份代码。
"""

SYSTEM_PROMPT = (
    "你是语音指令理解器。在候选动作里挑一个最符合用户意思的，"
    "只输出它的编号；候选里确实没有能表达这个意思的，只输出 null。\n"
    "不要解释，不要输出别的任何字。"
)

NONE = "null"
"""模型表示"候选里没有合适的"。"""


@dataclass
class Suggestion:
    """模型给的建议。**建议**，不是命令——调用方仍要校验 action_id 在不在注册表里。"""

    action_id: str | None
    title: str = ""
    raw: str = ""
    """模型的原样输出，出问题时用来诊断。"""

    ms: float = 0.0


class LLMUnavailable(RuntimeError):
    """服务连不上 / 模型没下 / 运行时没装。调用方应当**安静地**退回前几层。"""


class Client(Protocol):
    """两种后端共有的接口。`Pipeline` 只认这三个成员。"""

    def probe(self, *, force: bool = False) -> bool: ...

    def suggest(self, text: str, candidates: list[tuple[str, str]]) -> Suggestion | None: ...

    def describe(self) -> str: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# 候选与提示词
# --------------------------------------------------------------------------- #


META_ACTIONS = frozenset({"open.target", "sys.close_app", "sys.close_window", "schedule"})
"""不给模型看的"管道"动作。

这些动作没有固定目标——要开谁、关谁、什么时候提醒，得先由意图层解析出对象和
时间。把 `open.target` 摆进候选里，模型只知道"这是打开任意应用"，根本没法判断
「算个数」该不该选它。实测更糟：它挤掉了 `open.calc` 这类**真正带名字**的候选
（候选按配置顺序取前 12 个，`open.target` 排在第一位）。

日程同理：它需要的是标题和时间两个槽位，不是一个动作 id。真要支持"模型帮忙建日程"，
该单独走一条通路，而不是把它混在"挑一个动作"里。
"""


def candidate_actions(actions: list[Any], limit: int = 12) -> list[tuple[str, str]]:
    """给模型看的候选清单：id + 描述（没有描述就用别名凑）。

    `limit` 是有意的：小模型的判断力随候选数量迅速下降，官方 benchmark 里
    20 选项就已经掉到 0.451。宁可只给它最像的一小撮。
    """
    out: list[tuple[str, str]] = []
    for a in actions:
        aid = str(getattr(a, "id", ""))
        if aid in META_ACTIONS:
            continue
        if not getattr(a, "enabled", True):
            continue
        aliases = [str(x) for x in (getattr(a, "aliases", None) or []) if str(x).strip()]
        desc = (getattr(a, "describe", "") or "").strip()
        if not desc:
            desc = "、".join(aliases[:4]) or aid
        # 有具体别名的排前面：它们才是"用户真的会这么说"的动作。
        # 只写了一个泛泛描述（「打开应用」）的排在后面，免得占满候选名额。
        out.append((aid, desc, 0 if aliases else 1))
    out.sort(key=lambda t: t[2])
    return [(aid, desc) for aid, desc, _rank in out[:limit]]


def _numbered(candidates: list[tuple[str, str]]) -> str:
    """候选写成编号清单。

    用编号而不是动作 id：id 是 `sys.volume_down` 这种带点带下划线的串，0.5B 的
    模型经常吐得不准（少个点、多个空格），而一个数字它对得准。编号到 id 的映射
    在我们这边做，模型不需要知道 id 长什么样。
    """
    return "\n".join(f"{i + 1}. {desc}" for i, (_aid, desc) in enumerate(candidates))


def build_prompt(text: str, candidates: list[tuple[str, str]]) -> str:
    return f"候选动作：\n{_numbered(candidates)}\n\n用户说：「{text}」"


def build_grammar(n: int) -> str:
    """GBNF：把输出**限制**成 1..n 或 null。

    llama.cpp 在解码时按这个语法剪枝，所以模型**结构上不可能**吐出一个候选之外
    的编号。这比在提示词里写"不许编"可靠得多——那条路只是"请求"，这条是"约束"。
    """
    opts = " | ".join(f'"{i}"' for i in range(1, n + 1))
    return f'root ::= {opts} | "{NONE}"'


def parse_index(raw: str, n: int) -> int | None:
    """把模型输出读成候选下标（0-based）。读不出来或表示"没有"时返回 None。

    容错是必需的：`"2"`、`" 2 "`、`"2."`、`"编号2"` 都要认——小模型加个句号
    或前缀是常态，照着"理想输出"写解析会一直失败。
    """
    s = (raw or "").strip().strip('"\'').strip()
    if not s:
        return None
    low = s.lower()
    if NONE in low:
        return None
    for ch in s:
        if ch.isdigit():
            i = int(ch)
            return i - 1 if 1 <= i <= n else None
    return None


def parse_reply(raw: str) -> Suggestion:
    """从**外部服务**的 JSON 输出里抠出结果。

    外部服务（LM Studio / Ollama）走的是普通 JSON，没有语法约束，所以两种格式
    都要认：动作 id 直接写出来，或者写编号。都不认识就当"没有建议"。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
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


# --------------------------------------------------------------------------- #
# 外部服务后端
# --------------------------------------------------------------------------- #


class SlotExtractor:
    """问一个**已经在跑**的 OpenAI 兼容服务要一个候选动作。"""

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

    def suggest(self, text: str, candidates: list[tuple[str, str]]) -> Suggestion | None:
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
        body, ms = self._post(payload)
        if body is None:
            return None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.last_error = f"返回结构不认识：{str(body)[:200]}"
            return None
        raw = str(content).strip()
        idx = parse_index(raw, len(candidates))
        if idx is not None:
            return Suggestion(candidates[idx][0], raw=raw, ms=ms)
        return parse_reply(raw) if "{" in raw else Suggestion(None, raw=raw, ms=ms)

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
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
                return json.loads(r.read().decode("utf-8", errors="replace")), (
                    time.perf_counter() - t0
                ) * 1000
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            # 连接被拒通常意味着服务关了：把探测结论改掉，后面几句不再白等
            self.available = False
            _log.info("小模型层不可用：%s", self.last_error)
            return None, (time.perf_counter() - t0) * 1000

    def describe(self) -> str:
        if self.available is None:
            state = "未探测"
        elif self.available:
            state = f"可用（{self.model or '默认模型'}）"
        else:
            state = f"不可用（{self.last_error or '连不上'}）"
        return f"外部服务 {self.endpoint}  {state}"

    def close(self) -> None:
        """外部服务的进程不归我们管，什么都不用做。"""


# --------------------------------------------------------------------------- #
# 内置 llama.cpp 后端
# --------------------------------------------------------------------------- #


class LlamaCppClient:
    """自己起 llama-server、自己收掉，推理时用 GBNF 把输出锁死在候选上。"""

    def __init__(
        self,
        server: Any,  # noqa: ANN001 - llamacpp.LlamaServer，避免循环导入
        *,
        timeout: float = 30.0,
        max_tokens: int = 16,
    ) -> None:
        self.server = server
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.last_error = ""
        self.available: bool | None = None

    # -- 探测 ------------------------------------------------------------- #

    def probe(self, *, force: bool = False) -> bool:
        """运行时和模型都在、并且服务起得来。

        `force=True` 会真的把服务起起来（慢，几秒）；否则只看文件在不在——
        启动那一刻不该为了一个可选功能卡住。
        """
        if self.available is not None and not force:
            return self.available
        from . import llamacpp

        if not llamacpp.server_ready(self.server.runtime_dir):
            self.last_error = (
                f"没装 llama.cpp 运行时（{self.server.runtime_dir}）；跑 `voice-ctl llm --install`"
            )
            self.available = False
            return False
        if not self.server.model_path.is_file():
            self.last_error = (
                f"没有模型文件（{self.server.model_path}）；跑 `voice-ctl llm --download`"
            )
            self.available = False
            return False
        self.available = True
        if force:
            self.available = self.server.ensure_started()
            if not self.available:
                self.last_error = self.server.last_error
        return self.available

    # -- 抽槽位 ----------------------------------------------------------- #

    def suggest(self, text: str, candidates: list[tuple[str, str]]) -> Suggestion | None:
        if not candidates:
            return None
        if not self.probe():
            return None
        if not self.server.ensure_started():
            self.last_error = self.server.last_error
            self.available = False
            return None

        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(text, candidates)},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "grammar": build_grammar(len(candidates)),
            "stream": False,
        }
        import time

        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(
                f"{self.server.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                body = json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            _log.info("llama-server 推理失败：%s", self.last_error)
            return None
        ms = (time.perf_counter() - t0) * 1000

        try:
            raw = str(body["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            self.last_error = f"返回结构不认识：{str(body)[:200]}"
            return None
        idx = parse_index(raw, len(candidates))
        return Suggestion(candidates[idx][0] if idx is not None else None, raw=raw, ms=ms)

    def describe(self) -> str:
        from . import llamacpp

        rt = "已装" if llamacpp.server_ready(self.server.runtime_dir) else "未装"
        md = "有" if self.server.model_path.is_file() else "没有"
        state = "未探测" if self.available is None else ("可用" if self.available else "不可用")
        return (
            f"内置 llama.cpp（运行时{rt}，模型{md}，{self.server.model_path.name}）  {state}"
            + (f"  {self.last_error}" if self.last_error and not self.available else "")
        )

    def close(self) -> None:
        """收掉子进程。Engine.close() 会调它——留着就是个占 400MB 的孤儿。"""
        try:
            self.server.stop()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def make_client(cfg: Any) -> Client | None:  # noqa: ANN001 - config.LLMConfig
    """按配置造一个客户端。`enabled = false` 时返回 None（这一层不存在）。

    这里**只造对象，不启动进程**：启动由第一次 `suggest()` 触发。用户开着这一层
    却整场没说过一句长尾话时，不该多一个常驻进程。
    """
    if not getattr(cfg, "enabled", False):
        return None
    backend = (getattr(cfg, "backend", "local") or "local").strip().lower()
    if backend == "server":
        return SlotExtractor(cfg.endpoint, cfg.model, timeout=cfg.timeout)

    from . import llamacpp

    model_path = llamacpp.find_model(getattr(cfg, "model", "") or "")
    if model_path is None:
        # 模型没下时不要抛——返回一个"描述得清楚"的客户端，probe 会告诉用户怎么办
        spec = llamacpp.MODELS.get(getattr(cfg, "model", "") or "", llamacpp.QWEN_05B)
        model_path = llamacpp.model_dir() / spec.filename
    server = llamacpp.LlamaServer(
        model_path,
        runtime_dir=llamacpp.resolve_runtime_dir(),
        ctx_size=getattr(cfg, "ctx_size", 2048),
        threads=getattr(cfg, "threads", 0),
        startup_timeout=getattr(cfg, "startup_timeout", 60.0),
    )
    return LlamaCppClient(server, timeout=getattr(cfg, "request_timeout", 30.0))


__all__ = [
    "DEFAULT_ENDPOINT",
    "META_ACTIONS",
    "NONE",
    "SYSTEM_PROMPT",
    "Client",
    "LLMUnavailable",
    "LlamaCppClient",
    "SlotExtractor",
    "Suggestion",
    "build_grammar",
    "build_prompt",
    "candidate_actions",
    "make_client",
    "parse_index",
    "parse_reply",
]
