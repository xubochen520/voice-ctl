"""语义层（Laya）接线测试。

不需要真装 laya 或真权重：这批测试守的是**我踩过的三个坑**，
它们都是「代码能跑但会崩」或「配置写了没接线」的类型。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl.config import ActionConfig, AppConfig, DecisionConfig, load_config
from voice_ctl.decision import (
    WEIGHTS,
    DecisionUnavailable,
    check_weights,
    weights_dir,
)

ROOT = Path(__file__).resolve().parent.parent


def actions() -> list[ActionConfig]:
    return [
        ActionConfig(id="open.calc", handler="open_app", aliases=["计算器"], target="calc.exe",
                     describe="打开计算器算数"),
        ActionConfig(id="open.notepad", handler="open_app", aliases=["记事本"], target="notepad.exe",
                     describe="打开记事本写字"),
    ]


# --------------------------------------------------------------------------- #
# 权重检查（这是替代"import 能不能过"的正确判据）
# --------------------------------------------------------------------------- #


def test_check_weights_missing_dir(tmp_path: Path):
    ok, why = check_weights(tmp_path / "nope")
    assert not ok
    assert "目录不存在" in why


def test_check_weights_reports_each_missing_piece(tmp_path: Path):
    d = tmp_path / "w"
    d.mkdir()
    ok, why = check_weights(d)
    assert not ok
    assert "rl_agent_config.json" in why
    assert "onnx" in why, f"没提到缺 .onnx 图：{why}"


def test_check_weights_ok_when_complete(tmp_path: Path):
    d = tmp_path / "w"
    (d / "tokenizer").mkdir(parents=True)
    (d / "rl_agent_config.json").write_text("{}", encoding="utf-8")
    (d / "model_int8.onnx").write_bytes(b"\x00" * 2048)
    ok, why = check_weights(d)
    assert ok, why
    assert "model_int8.onnx" in why


def test_check_weights_ignores_non_onnx(tmp_path: Path):
    """目录里只有别的文件时必须报缺图，而不是当成可用。"""
    d = tmp_path / "w"
    (d / "tokenizer").mkdir(parents=True)
    (d / "rl_agent_config.json").write_text("{}", encoding="utf-8")
    (d / "notes.txt").write_text("hi", encoding="utf-8")
    ok, why = check_weights(d)
    assert not ok
    assert "onnx" in why


def test_weights_dir_is_absolute():
    assert weights_dir("models/x").is_absolute()


# --------------------------------------------------------------------------- #
# WEIGHTS 表：必须标注 checkpoint，否则中文会用到 english（接近随机）
# --------------------------------------------------------------------------- #


def test_weights_table_documents_checkpoint():
    for name, spec in WEIGHTS.items():
        assert "repo" in spec and "onnx" in spec and "note" in spec
        assert spec["note"], f"{name} 缺少说明"
        assert "encoder" in spec["note"] or "max_len" in spec["note"], (
            f"{name} 的说明必须点明是哪个 checkpoint —— 选错会让中文接近随机"
        )


def test_multilingual_weight_points_at_multilingual_files():
    spec = WEIGHTS["multilingual"]
    assert "multilingual" in spec["onnx"], "multilingual 的图路径里应当带 multilingual"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def test_decision_defaults_are_safe():
    d = DecisionConfig()
    assert d.enabled is False, "语义层必须默认关闭：它会拉进 torch + 900MB 权重"
    assert d.model == "multilingual", "默认必须是中文可用的 checkpoint"
    assert 0.0 < d.min_confidence < 1.0
    assert "multilingual" in d.onnx_dir


def test_decision_path_resolves_relative_to_config(tmp_path: Path):
    """相对路径必须按配置文件解析，不能按 cwd。

    否则从别的目录运行 voice-ctl 就会找不到权重——这个坑实测踩过。
    """
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        '[hotkey]\nkeys = "<f9>"\n\n'
        '[decision]\nonnx_dir = "models/laya-onnx/multilingual"\n\n'
        '[[action]]\nid = "a"\nhandler = "open_app"\n'
        'aliases = ["记事本"]\ntarget = "notepad.exe"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfgfile)
    assert cfg.decision_path() == (tmp_path / "models/laya-onnx/multilingual").resolve()


def test_decision_path_absolute_is_kept(tmp_path: Path):
    abs_target = tmp_path / "absolute" / "weights"
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text(
        '[hotkey]\nkeys = "<f9>"\n\n'
        f'[decision]\nonnx_dir = "{abs_target.as_posix()}"\n\n'
        '[[action]]\nid = "a"\nhandler = "open_app"\n'
        'aliases = ["记事本"]\ntarget = "notepad.exe"\n',
        encoding="utf-8",
    )
    assert load_config(cfgfile).decision_path() == abs_target.resolve()


def test_min_confidence_out_of_range_rejected(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[hotkey]\nkeys = "<f9>"\n\n[decision]\nmin_confidence = 2.5\n\n'
        '[[action]]\nid = "a"\nhandler = "open_app"\n'
        'aliases = ["记事本"]\ntarget = "notepad.exe"\n',
        encoding="utf-8",
    )
    from voice_ctl.config import ConfigError

    with pytest.raises(ConfigError, match="0.0-1.0"):
        load_config(p)


def test_real_config_decision_dir_is_multilingual():
    """仓库自带配置里，语义层权重目录必须指向 multilingual。"""
    cfg = load_config(ROOT / "config.toml")
    assert cfg.decision.model == "multilingual"
    assert cfg.decision_path().name == "multilingual"


# --------------------------------------------------------------------------- #
# SemanticDecider：没有 weights 时必须给出**可操作**的报错
# --------------------------------------------------------------------------- #


def test_decider_raises_actionable_error_without_weights(tmp_path: Path):
    from voice_ctl.decision import SemanticDecider

    cfg = DecisionConfig(enabled=True, onnx_dir=str(tmp_path / "empty"))
    d = SemanticDecider(actions(), cfg, root=tmp_path / "empty")
    with pytest.raises(DecisionUnavailable) as ei:
        d.load()
    msg = str(ei.value)
    assert "fetch-decision" in msg or "不存在" in msg, (
        f"报错必须告诉用户怎么修：{msg}"
    )


def test_decider_rejects_empty_action_list():
    from voice_ctl.decision import SemanticDecider

    with pytest.raises(DecisionUnavailable, match="没有启用的动作"):
        SemanticDecider([], DecisionConfig())


def test_decider_criteria_uses_describe_then_aliases():
    from voice_ctl.decision import SemanticDecider

    a1 = ActionConfig(id="x", handler="open_app", aliases=["计算器"], target="calc.exe",
                      describe="算数用的")
    a2 = ActionConfig(id="y", handler="open_app", aliases=["记事本", "本子"], target="notepad.exe")
    d = SemanticDecider([a1, a2], DecisionConfig())
    crit = d._criteria()
    assert crit["x"] == "算数用的", "有 describe 就该用 describe"
    assert "记事本" in crit["y"], "没有 describe 就退回别名"


def test_decider_explain_is_readable():
    from voice_ctl.decision import SemanticDecider

    text = SemanticDecider(actions(), DecisionConfig()).explain()
    assert "min_confidence" in text
    assert "open.calc" in text


# --------------------------------------------------------------------------- #
# pipeline 集成：语义层结论要能驱动执行
# --------------------------------------------------------------------------- #


class FakeDecider:
    source = "onnx"

    def __init__(self, action_id: str | None, conf: float = 0.9) -> None:
        self.action_id = action_id
        self.conf = conf

    def decide(self, text: str):  # noqa: ANN201
        from voice_ctl.decision import Decision

        low = self.action_id is not None and self.conf < 0.6
        return Decision(self.action_id, self.conf, "onnx", low_confidence=low)


def make_pipe(cfg: AppConfig, decider):  # noqa: ANN001, ANN201
    from voice_ctl.actions import build_registry
    from voice_ctl.app import Pipeline
    from voice_ctl.matcher import Matcher
    from voice_ctl.normalize import Normalizer

    n = Normalizer()
    return Pipeline(
        asr=None,  # type: ignore[arg-type]
        matcher=Matcher(cfg.enabled_actions, normalizer=n, threshold=cfg.match.threshold),
        registry=build_registry(cfg.enabled_actions),
        actions=cfg.enabled_actions,
        normalizer=n,
        decider=decider,
        min_confidence=0.6,
    )


def test_pipeline_uses_decider_when_alias_misses():
    cfg = AppConfig(actions=actions())
    pipe = make_pipe(cfg, FakeDecider("open.calc"))
    out = pipe.process_text("算个数", dry_run=True)
    assert out.via == "decision", f"应当走语义层，实际走了 {out.via}"
    assert out.action_id == "open.calc"
    assert out.result is not None and out.result.ok


def test_pipeline_prefers_alias_over_decider():
    """第 0 层命中就不该再花 100ms 走语义层。"""
    cfg = AppConfig(actions=actions())

    class Exploding:
        source = "onnx"

        def decide(self, text):  # noqa: ANN001
            raise AssertionError("别名命中时不该调用语义层")

    pipe = make_pipe(cfg, Exploding())  # type: ignore[arg-type]
    out = pipe.process_text("打开计算器", dry_run=True)
    assert out.via == "matcher"
    assert out.action_id == "open.calc"


def test_pipeline_rejects_low_confidence_decision():
    cfg = AppConfig(actions=actions())
    pipe = make_pipe(cfg, FakeDecider("open.calc", conf=0.2))
    out = pipe.process_text("算个数", dry_run=True)
    assert out.action_id is None
    assert "低于阈值" in out.note
    assert out.result is None, "低置信时绝不能执行"


def test_pipeline_survives_decider_exception():
    """语义层出错不能让整个助手崩掉，要回落到"未命中"。"""
    cfg = AppConfig(actions=actions())

    class Boom:
        source = "onnx"

        def decide(self, text):  # noqa: ANN001
            raise RuntimeError("模型炸了")

    pipe = make_pipe(cfg, Boom())  # type: ignore[arg-type]
    out = pipe.process_text("算个数", dry_run=True)
    assert out.action_id is None
    assert "语义层出错" in out.note
    assert "模型炸了" in out.note


def test_pipeline_without_decider_reports_it():
    cfg = AppConfig(actions=actions())
    pipe = make_pipe(cfg, None)
    out = pipe.process_text("算个数", dry_run=True)
    assert "语义层未启用" in out.note
