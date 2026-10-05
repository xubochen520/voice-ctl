"""小模型层（可选）：只测"能不能安全地用它"，不测模型准不准。

这一层和别的层有个根本区别：它依赖一个**可能根本不存在**的外部服务。
所以测试守的是三件事：

  * 服务不在时，整条链路照常工作（这一层静默退场，绝不抛异常）
  * 模型输出再脏也能解析出 JSON（它经常加代码块标记、加解释）
  * 模型编出来的 action_id 绝不执行（它只有建议权，没有决定权）

真实推理质量不在这里测——那是 `voice-ctl llm "..."` 的活，要人来判断。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_ctl.config import ActionConfig, AppConfig
from voice_ctl.llm import (
    DEFAULT_ENDPOINT,
    META_ACTIONS,
    SlotExtractor,
    build_grammar,
    build_prompt,
    candidate_actions,
    parse_index,
    parse_reply,
)

ACTIONS = [
    ActionConfig(id="open.notepad", handler="open_app", aliases=["记事本"],
                 describe="打开记事本写字"),
    ActionConfig(id="open.calc", handler="open_app", aliases=["计算器"],
                 describe="打开计算器"),
    ActionConfig(id="off", handler="open_app", aliases=["关掉的"], enabled=False),
]


# --------------------------------------------------------------------------- #
# 解析模型输出
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "want_id", "want_title"),
    [
        ('{"action_id": "open.notepad", "title": ""}', "open.notepad", ""),
        # 小模型极爱加代码块围栏，照着"理想输出"写解析会一直失败
        ('```json\n{"action_id": "open.calc", "title": "算个数"}\n```', "open.calc", "算个数"),
        # 也爱在 JSON 外面加一句话
        ('我觉得是 {"action_id":"open.calc","title":""} 吧', "open.calc", ""),
        # null 表示"候选里没有合适的"——必须认，它是模型唯一诚实的表达方式
        ('{"action_id": null, "title": ""}', None, ""),
        ('{"action_id": "   ", "title": ""}', None, ""),
        # 字段缺失
        ('{"title": "开会"}', None, "开会"),
    ],
)
def test_parse_reply_is_tolerant(raw: str, want_id: str | None, want_title: str):
    s = parse_reply(raw)
    assert s.action_id == want_id
    assert s.title == want_title


@pytest.mark.parametrize("raw", ["", "我不知道", "{不是 json}", "[]", "null"])
def test_parse_reply_on_garbage(raw: str):
    """解析不了就是 None，**不猜**。猜出来的动作会被真的执行出去。"""
    s = parse_reply(raw)
    assert s.action_id is None
    assert s.raw == raw, "原样留着，出问题时靠它诊断"


# --------------------------------------------------------------------------- #
# 候选与提示词
# --------------------------------------------------------------------------- #


def test_candidate_actions_skips_disabled_and_uses_describe():
    cands = candidate_actions(ACTIONS)
    assert [c[0] for c in cands] == ["open.notepad", "open.calc"]
    assert "打开记事本写字" in cands[0][1]


def test_candidate_actions_respects_the_limit():
    """候选越多模型越爱乱挑（官方 20 选项任务已经掉到 0.451）。"""
    many = [
        ActionConfig(id=f"a{i}", handler="open_app", aliases=[f"别名{i}"], describe=f"第{i}个")
        for i in range(30)
    ]
    assert len(candidate_actions(many, 5)) == 5


def test_candidate_actions_falls_back_to_aliases():
    """没写 describe 就退到别名。两者都没有的话模型只能看到一个 id，没法判断。"""
    a = ActionConfig(id="x.y", handler="open_app", aliases=["甲", "乙"])
    assert candidate_actions([a]) == [("x.y", "甲、乙")]


def test_candidate_actions_hides_pipeline_actions():
    """`open.target` / `sys.close_app` / `schedule` 这类不该给模型看。

    它们没有固定目标——要开谁、关谁、什么时候提醒，得先由意图层解析出对象和时间。
    摆进候选里模型没法判断「算个数」该不该选 `open.target`；实测更糟的是它会
    **挤掉** `open.calc` 这种真正带名字的候选（候选按配置顺序取前 N 个，
    `open.target` 恰好排在第一位）。
    """
    actions = [
        ActionConfig(id="open.target", handler="open_target", aliases=["打开应用"]),
        ActionConfig(id="schedule", handler="schedule", aliases=["日程", "提醒"]),
        ActionConfig(id="sys.close_app", handler="close_app", aliases=["关闭应用"]),
        ActionConfig(id="open.calc", handler="open_app", aliases=["计算器"], describe="打开计算器"),
    ]
    ids = [c[0] for c in candidate_actions(actions)]
    assert ids == ["open.calc"], ids
    assert not (set(ids) & META_ACTIONS)


def test_candidate_actions_prefers_actions_with_real_aliases():
    """有具体别名的排前面。只写了个泛泛描述的动作不该占满候选名额。"""
    actions = [
        ActionConfig(id="a.generic", handler="open_app", aliases=[], describe="做点什么"),
        ActionConfig(id="b.specific", handler="open_app", aliases=["计算器"], describe="打开计算器"),
    ]
    assert [c[0] for c in candidate_actions(actions)] == ["b.specific", "a.generic"]


def test_prompt_numbers_the_candidates_and_carries_the_sentence():
    """候选写成**编号**清单，不写动作 id。

    id 是 `sys.volume_down` 这种带点带下划线的串，0.5B 模型经常吐得不准；
    一个数字它对得准。编号到 id 的映射在我们这边做。
    """
    p = build_prompt("有个文件要改一下", candidate_actions(ACTIONS))
    assert "1. 打开记事本写字" in p and "2. 打开计算器" in p
    assert "有个文件要改一下" in p
    assert "open.notepad" not in p, "id 不进提示词，只进我们的映射表"


# --------------------------------------------------------------------------- #
# GBNF 语法约束：这一层唯一真正的安全保证
# --------------------------------------------------------------------------- #


def test_grammar_only_allows_the_candidate_numbers():
    """语法里只出现 1..n 和 null。

    这不是"提示词里请它别乱说"，而是解码时按语法剪枝——模型**结构上不可能**
    输出候选之外的编号。整层的安全性就建在这上面。
    """
    g = build_grammar(3)
    assert g == 'root ::= "1" | "2" | "3" | "null"'
    assert '"4"' not in g


def test_grammar_scales_to_the_candidate_count():
    g = build_grammar(12)
    for i in range(1, 13):
        assert f'"{i}"' in g
    assert '"13"' not in g


@pytest.mark.parametrize(
    ("raw", "n", "want"),
    [
        ("2", 5, 1),          # 1-based 编号 → 0-based 下标
        (" 2 ", 5, 1),
        ("2.", 5, 1),         # 小模型爱加个句号
        ("编号2", 5, 1),
        ('"3"', 5, 2),
        ("null", 5, None),    # "候选里没有合适的"
        ("NULL", 5, None),
        ("", 5, None),
        ("我不知道", 5, None),
        ("9", 5, None),       # 超出候选范围
        ("0", 5, None),
    ],
)
def test_parse_index_is_tolerant_and_bounded(raw: str, n: int, want: int | None):
    assert parse_index(raw, n) == want


# --------------------------------------------------------------------------- #
# 服务不可用：这是**必须**能优雅退场的路径
# --------------------------------------------------------------------------- #


def test_a_dead_endpoint_returns_none_without_raising():
    """没装 LM Studio/Ollama 是常态。这一层挂掉不能让整句话执行不了。"""
    ex = SlotExtractor("http://127.0.0.1:9/v1", timeout=0.3)  # 9 号端口上不会有服务
    assert ex.probe() is False
    assert ex.suggest("随便说点什么", [("open.calc", "打开计算器")]) is None
    assert ex.last_error, "失败也要留下原因，doctor/日志要能看到"


def test_probe_result_is_cached():
    """每次说话都先等一次超时是不可接受的。探一次就记住。"""
    ex = SlotExtractor("http://127.0.0.1:9/v1", timeout=0.3)
    assert ex.probe() is False
    first = ex.last_error
    assert ex.probe() is False
    assert ex.last_error == first, "第二次应当是直接返回缓存结论，没有再发一次请求"


def test_no_candidates_means_no_request():
    """候选为空时连探测都不该做——连上了也没东西可挑。"""
    ex = SlotExtractor("http://127.0.0.1:9/v1", timeout=0.3)
    assert ex.suggest("随便说点什么", []) is None
    assert ex.available is None, "压根没探测过"


def test_describe_says_something_useful_in_every_state():
    ex = SlotExtractor("http://127.0.0.1:9/v1", timeout=0.3)
    assert "未探测" in ex.describe()
    ex.probe()
    d = ex.describe()
    assert "不可用" in d and ex.last_error[:20] in d


# --------------------------------------------------------------------------- #
# pipeline 集成：建议权 vs 决定权
# --------------------------------------------------------------------------- #


def _pipe_with(fake):
    from voice_ctl.actions import build_registry
    from voice_ctl.app import Pipeline
    from voice_ctl.matcher import Matcher
    from voice_ctl.normalize import Normalizer

    cfg = AppConfig(actions=ACTIONS)
    n = Normalizer()
    return Pipeline(
        asr=None,  # type: ignore[arg-type]
        matcher=Matcher(cfg.enabled_actions, normalizer=n, threshold=cfg.match.threshold),
        registry=build_registry(cfg.enabled_actions),
        actions=cfg.enabled_actions,
        normalizer=n,
        intent_enabled=False,  # 只测最外层这一层
        llm=fake,
    )


class FakeLLM:
    def __init__(self, suggestion):  # noqa: ANN001
        self._s = suggestion

    def suggest(self, text, candidates):  # noqa: ANN001, ANN201
        return self._s


def test_llm_suggestion_drives_execution():
    from voice_ctl.llm import Suggestion

    out = _pipe_with(FakeLLM(Suggestion("open.calc", "算个数"))).process_text(
        "帮我把今天花的钱算一下", dry_run=True
    )
    assert out.via == "llm" and out.action_id == "open.calc"
    assert out.result is not None and out.result.ok


def test_llm_cannot_invent_an_action():
    """模型编一个不存在的 id 时**绝不执行**，而且要说出来。

    这是这一层唯一真正危险的地方：模型输出是文本，"执行"必须由注册表把关。
    """
    from voice_ctl.llm import Suggestion

    out = _pipe_with(FakeLLM(Suggestion("rm.-rf", ""))).process_text("删掉所有东西", dry_run=False)
    assert out.action_id is None and out.result is None
    assert "不存在" in out.note


def test_llm_returning_null_is_a_normal_outcome():
    from voice_ctl.llm import Suggestion

    out = _pipe_with(FakeLLM(Suggestion(None))).process_text("随便说点什么", dry_run=False)
    assert out.action_id is None
    assert out.note, "至少要给用户一句话"


def test_llm_exception_does_not_break_the_pipeline():
    """这一层是锦上添花。它抛异常时整句话必须照常走完（退化成未命中）。"""

    class Boom:
        def suggest(self, text, candidates):  # noqa: ANN001, ANN201
            raise RuntimeError("服务炸了")

    out = _pipe_with(Boom()).process_text("随便说点什么", dry_run=False)
    assert out.action_id is None
    assert out.note


def test_llm_is_not_called_when_an_earlier_layer_matched():
    """前几层命中时不该为一句「打开记事本」等一次 HTTP 往返。"""

    class Exploding:
        def suggest(self, text, candidates):  # noqa: ANN001, ANN201
            raise AssertionError("前面的层已经命中了，不该调用小模型")

    out = _pipe_with(Exploding()).process_text("打开记事本", dry_run=True)
    assert out.action_id == "open.notepad"


def test_llm_off_by_default():
    """默认必须是关的：它依赖一个用户机器上可能根本不存在的服务。"""
    cfg = AppConfig(actions=ACTIONS)
    assert cfg.llm.enabled is False
    assert cfg.llm.endpoint == DEFAULT_ENDPOINT
    assert cfg.llm.max_candidates == 12


def test_llm_config_rejects_nonsense(tmp_path: Path):
    from voice_ctl.config import ConfigError, load_config

    p = tmp_path / "c.toml"
    p.write_text(
        '[llm]\nenabled = true\nbackend = "server"\nendpoint = ""\n\n'
        '[[action]]\nid = "a"\nhandler = "open_app"\n'
        'aliases = ["记事本"]\ntarget = "notepad.exe"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="endpoint"):
        load_config(p)


def test_llm_config_rejects_an_unknown_backend(tmp_path: Path):
    """拼错 backend 时要说出来，不能静默退回内置——那会让用户以为在用自己的服务。"""
    from voice_ctl.config import ConfigError, load_config

    p = tmp_path / "c.toml"
    p.write_text(
        '[llm]\nbackend = "llama.cpp"\n\n'
        '[[action]]\nid = "a"\nhandler = "open_app"\n'
        'aliases = ["记事本"]\ntarget = "notepad.exe"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="backend"):
        load_config(p)


def test_local_backend_does_not_need_an_endpoint(tmp_path: Path):
    """内置后端不用 endpoint —— 那是外部后端才需要的。"""
    from voice_ctl.config import load_config

    p = tmp_path / "c.toml"
    p.write_text(
        '[llm]\nenabled = true\nbackend = "local"\nendpoint = ""\n\n'
        '[[action]]\nid = "a"\nhandler = "open_app"\n'
        'aliases = ["记事本"]\ntarget = "notepad.exe"\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.llm.enabled and cfg.llm.backend == "local"


def test_real_config_parses_the_llm_section():
    """仓库自带的 config.toml 必须能被解析——用户第一眼看到的就是它。"""
    from voice_ctl.config import load_config

    root = Path(__file__).resolve().parent.parent
    cfg = load_config(root / "config.toml")
    assert cfg.llm.enabled is False, "默认不开，开了没服务会白等"
    assert cfg.schedule.enabled and cfg.intent.enabled, "这两个默认该是开的"
