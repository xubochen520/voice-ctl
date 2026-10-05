"""第 1 层：语义决策（可选，基于 Laya）。

什么时候需要它：第 0 层别名匹配只能命中「说话里出现了别名」的情况。
用户说「有个文件要改」（想开记事本）、「把声音关小」（想调音量）这类
没有命中别名的口语化表达，才需要语义层。

实测表现（scripts/probe_decision.py，6 选项零样本，本机 CPU）：
    有个文件要改一下 → open.notepad    0.914
    算个数           → open.calc       0.957
    我想聊个天       → open.wechat     0.828
    帮我截个图       → sys.screenshot  0.999
    屏幕别让人看了   → sys.lock        0.701
    随便说点什么     → open.wechat     0.589  ← 低于阈值会被正确拒绝
    加载 10.4s，单次推理 42-58ms

三个必须知道的坑（都是实测撞出来的，不是照文档抄的）：
  1. **`ONNXAgent` 没有 `from_pretrained`**。真实签名是
     `ONNXAgent(model_id_or_path, onnx_path=...)`——第一个参数是**目录**
     （放 rl_agent_config.json 和 tokenizer/），第二个是**导出的 .onnx 图**。
     文档和直觉都会让人写成 from_pretrained，那是 AttributeError。
  2. **官方仓库不发布 ONNX 权重**。`convaiinnovations/laya` 只有 torch 权重；
     ONNX 图得靠 `scripts/export_onnx.py` 自己导。社区有现成的（见 WEIGHTS）。
  3. **中文务必用 multilingual**。`tozp/laya-onnx` 那个包虽然小（405MB），
     但它是 **english** checkpoint（encoder 是 ModernBERT-large，max_len 512），
     中文用它接近随机，而且**置信度不会报警**。
  4. **"ONNX 路径不加载 torch" 是错的**（0.3.2 更正）。`laya.onnx_agent` 顶层
     import `laya.common`，而 `laya.common` 第 13 行就是 `import torch`
     ——它还要用 `torch.nn`、`torch.device("meta")`、`torch.softmax` 等等
     （单文件里 40 多处）。所以语义层**永远需要 torch（约 500MB）**，
     不管权重是不是 ONNX。打包时这直接决定了 exe 尺寸：不带 torch 的 exe
     一开语义层就报 `No module named 'torch'`，这不是"用户没装好"，
     是包本身少了一半。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from .config import ActionConfig, DecisionConfig

# 可用的 ONNX 权重来源（社区导出，非官方）。
# 每条都注明 checkpoint，因为选错会让中文准确率掉到接近随机。
WEIGHTS: dict[str, dict[str, str]] = {
    "multilingual": {
        "repo": "techtheist/laya-onnx",
        "onnx": "multilingual/model_int8.onnx",
        "config": "multilingual/rl_agent_config.json",
        "tokenizer": "multilingual/tokenizer.json",
        "tokenizer_config": "multilingual/tokenizer_config.json",
        "note": "mmBERT-base / max_len 1024，中文可用；int8 约 873MB",
    },
}


class DecisionUnavailable(Exception):
    """语义层不可用（没装/没权重/加载失败）。消息要说清**怎么办**。"""


@dataclass
class Decision:
    action_id: str | None
    confidence: float
    source: str
    """onnx / torch —— 实际走的哪条路径。"""

    raw: dict | None = None
    low_confidence: bool = False

    def describe(self) -> str:
        c = f"{self.confidence:.3f}"
        flag = " [低置信]" if self.low_confidence else ""
        return f"{self.action_id or '(未决)'} conf={c} via {self.source}{flag}"


def weights_dir(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def check_weights(root: str | Path) -> tuple[bool, str]:
    """检查 ONNX 权重目录是否完整。返回 (可用, 说明)。

    判据是**文件是否到位**，不是「import 能不能过」——后者会给出假阳性
    （`laya.onnx_agent` 能导入不代表有 ONNX 图，实测过）。
    """
    d = weights_dir(root)
    if not d.is_dir():
        return False, f"目录不存在：{d}"
    need = {
        "rl_agent_config.json": d / "rl_agent_config.json",
        "tokenizer/": d / "tokenizer",
    }
    missing = [name for name, p in need.items() if not p.exists()]
    graph = next((p for p in d.glob("*.onnx")), None)
    if graph is None:
        missing.append("*.onnx（模型图）")
    if missing:
        return False, f"缺少 {', '.join(missing)}"
    return True, f"就绪：{graph.name} ({graph.stat().st_size / 1024 / 1024:.0f}MB)"


def _running_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def available(root: str | Path) -> tuple[bool, str]:
    """语义层整体是否可用。"""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, '没装 onnxruntime。装法：.venv\\Scripts\\pip install "laya[onnx]"'

    try:
        from laya.onnx_agent import ONNXAgent  # noqa: F401
    except ImportError as e:
        if _running_frozen():
            # 打包 exe 里缺模块不是"用户没装好"，装 pip 包也修不了——得换一个
            # 把语义层打进包里的构建。说清楚，省得用户白折腾 pip。
            return (
                False,
                f"这个精简版 exe 没带语义层（{e}）。"
                "语义层需要 torch（约 500MB），所以默认不打包；"
                "要用它得按 VOICE_CTL_BUNDLE_DECISION=1 重新打包，"
                "或直接跑源码版（python -m voice_ctl）。",
            )
        return False, f'没装 laya 或版本不对（{e}）。装法：pip install "laya[onnx]"'

    ok, why = check_weights(root)
    if not ok:
        return False, f"{why}；可跑 `voice-ctl fetch-decision` 自动下载"
    return True, f"可用—— {why}"


class SemanticDecider:
    """把「用户说了什么」映射到「执行哪个动作」。"""

    def __init__(
        self,
        actions: Iterable["ActionConfig"],
        cfg: "DecisionConfig",
        root: str | Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.actions = [a for a in actions if a.enabled]
        if not self.actions:
            raise DecisionUnavailable("没有启用的动作，语义层无从选择")
        # root 由调用方按配置文件所在目录解析后传入，避免依赖 cwd
        self.root = weights_dir(root if root is not None else cfg.onnx_dir)
        self._agent = None
        self._source = ""

    # -- 加载 ------------------------------------------------------------- #

    def load(self) -> None:
        if self._agent is not None:
            return

        ok, why = available(self.root)
        if not ok:
            raise DecisionUnavailable(why)

        graph = next(self.root.glob("*.onnx"))
        try:
            # 注意：是构造函数，不是 from_pretrained（那个方法不存在）
            from laya.onnx_agent import ONNXAgent

            self._agent = ONNXAgent(str(self.root), onnx_path=str(graph))
            self._source = "onnx"
        except Exception as e:  # noqa: BLE001
            raise DecisionUnavailable(
                f"加载 ONNX 失败（{self.root}）：{type(e).__name__}: {e}"
            ) from e

    @property
    def source(self) -> str:
        return self._source

    # -- 决策 ------------------------------------------------------------- #

    def _criteria(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for a in self.actions:
            desc = a.describe.strip() or "、".join(a.aliases) or a.id
            out[a.id] = desc
        return out

    def decide(self, text: str) -> Decision:
        """返回要执行的动作。低置信时 action_id 仍给出，但标记 low_confidence。"""
        self.load()
        assert self._agent is not None

        questions = {
            "action": {
                "type": "choice",
                "instructions": "用户想执行哪个操作？只选最贴近的一个。",
                "criteria": self._criteria(),
            }
        }

        kwargs = {}
        if self.cfg.min_confidence:
            kwargs["min_confidence"] = self.cfg.min_confidence
        try:
            res = self._agent.predict({"text": text}, questions, **kwargs)
        except TypeError:
            # 某些版本不接受 min_confidence，退一步手动判阈值
            res = self._agent.predict({"text": text}, questions)

        answers = res.get("answers", {})
        node = answers.get("action", {}) or {}
        choice = node.get("choice")
        conf_raw = node.get("answer_confidence", node.get("confidence", 0.0))
        conf = float(conf_raw) if isinstance(conf_raw, (int, float)) else 0.0
        low = bool(node.get("low_confidence", False)) or conf < self.cfg.min_confidence
        return Decision(
            action_id=choice,
            confidence=conf,
            source=self._source,
            raw=node,
            low_confidence=low,
        )

    def explain(self) -> str:
        lines = [
            f"语义层    : {self.cfg.model} dir={self.cfg.onnx_dir}",
            f"阈值      : min_confidence={self.cfg.min_confidence}",
            f"候选动作  : {len(self.actions)}",
        ]
        for a in self.actions:
            lines.append(f"  - {a.id:24} {a.describe or '、'.join(a.aliases)}")
        return "\n".join(lines)


def fetch_weights(root: str | Path, which: str = "multilingual") -> list[str]:
    """下载 ONNX 权重到 root。返回下载的文件名列表。

    只下 int8 图 + config + tokenizer；tokenizer 走官方仓库（和权重同源），
    图走社区导出。这是社区导出仓库的布局决定的，不是随便拼的。
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise DecisionUnavailable(
            '需要 huggingface_hub（装 laya 时会一起装上）。pip install "laya[onnx]"'
        ) from e

    spec = WEIGHTS.get(which)
    if spec is None:
        raise DecisionUnavailable(
            f"不认识 {which!r}；可选：{', '.join(WEIGHTS)}"
        )

    d = weights_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "tokenizer").mkdir(exist_ok=True)

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    jobs = [
        (spec["repo"], spec["onnx"], d / "model_int8.onnx"),
        (spec["repo"], spec["config"], d / "rl_agent_config.json"),
        ("convaiinnovations/laya", f"{which}/tokenizer/tokenizer_config.json",
         d / "tokenizer" / "tokenizer_config.json"),
        ("convaiinnovations/laya", f"{which}/tokenizer/tokenizer.json",
         d / "tokenizer" / "tokenizer.json"),
    ]
    done: list[str] = []
    for repo, remote, dest in jobs:
        if dest.is_file() and dest.stat().st_size > 0:
            continue
        src = hf_hub_download(repo, remote)
        dest.write_bytes(Path(src).read_bytes())
        done.append(f"{dest.name} ({dest.stat().st_size / 1024 / 1024:.0f}MB)")
    return done
