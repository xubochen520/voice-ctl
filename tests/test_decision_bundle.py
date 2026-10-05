"""内嵌语义层权重的路径兜底与下载短路。

这批测试守的是一个**曾经会静默失效**的接线：把 873MB 权重打进包之后，
配置里的 `[decision].onnx_dir` 仍是相对路径，按配置文件所在目录解析，
而打包后那份 config.toml 在 exe 同级——那里没有权重，权重在 `_MEIPASS` 里。
没有 `resolve_decision_dir` 兜底，"打进包"就是白打：程序照样报"缺少 *.onnx"，
而且报错完全不指向真因（用户会以为是自己没下权重）。

ASR 模型早就有 `resolve_model_dir` 这个兜底，语义层一直没有。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from voice_ctl import bootstrap

REL = Path("models/laya-onnx/multilingual")


def _make_weights(root: Path) -> Path:
    """造一个 check_weights 认得的权重目录（判据是文件在不在，不是能否 import）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "model_int8.onnx").write_bytes(b"fake-onnx")
    (root / "rl_agent_config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer").mkdir(exist_ok=True)
    (root / "tokenizer" / "tokenizer.json").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """搭一个假的"exe 同级 + 解包目录"布局，并把 bootstrap 指过去。"""
    exe = tmp_path / "exe"
    bundle = tmp_path / "bundle"
    exe.mkdir()
    bundle.mkdir()
    monkeypatch.setattr(bootstrap, "exe_dir", lambda: exe)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: bundle)
    return exe, bundle


# --------------------------------------------------------------------------- #
# 优先级
# --------------------------------------------------------------------------- #


def test_configured_dir_wins(layout):
    """配置指向的目录真的存在 -> 用它（源码运行、或用户自己下了权重）。"""
    exe, bundle = layout
    configured = _make_weights(exe / REL)
    # 解包里也放一份，验证配置这份优先
    _make_weights(bundle / REL)

    assert bootstrap.resolve_decision_dir(configured) == configured


def test_falls_back_to_bundle(layout):
    """配置目录不存在、解包目录里有 -> 用解包里那份。这是打包后的主路径。"""
    exe, bundle = layout
    bundled = _make_weights(bundle / REL)
    configured = exe / REL  # 不存在

    assert bootstrap.resolve_decision_dir(configured) == bundled


def test_exe_sibling_beats_bundle(layout):
    """用户手动把权重放到 exe 同级 -> 赢过打包内那份。

    这条是刻意的：用户放进来的文件是显式意图，应当能换掉内置权重
    而不必重新打包。（注意顺序：配置 -> exe 同级 -> 解包目录。）
    """
    exe, bundle = layout
    sibling = _make_weights(exe / REL)
    _make_weights(bundle / REL)
    configured = exe / "somewhere" / "else"  # 不存在，逼它往下走

    assert bootstrap.resolve_decision_dir(configured) == sibling


def test_returns_configured_when_nothing_found(layout):
    """都没有 -> 原样返回，交给调用方报"缺少权重"并给下载指引。

    注意这里**不能**把权重建在 exe 同级，否则走的是"exe 同级"那条分支。
    """
    exe, _bundle = layout
    configured = exe / "data" / "laya-onnx" / "multilingual"

    assert bootstrap.resolve_decision_dir(configured) == configured


def test_source_run_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """源码运行时 _bundle_dir() 返回 None，不能因此崩。"""
    monkeypatch.setattr(bootstrap, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: None)
    configured = tmp_path / "nope" / "multilingual"

    assert bootstrap.resolve_decision_dir(configured) == configured


def test_real_repo_weights_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实仓库里那份权重也要能被找到——防止把路径常量写错。"""
    repo_weights = Path(__file__).resolve().parent.parent / REL
    if not repo_weights.is_dir():
        pytest.skip("本机没有 semantic 权重（CI 上正常）")

    monkeypatch.setattr(bootstrap, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: None)
    # 直接指向真目录，验证它是"存在"的
    assert bootstrap.resolve_decision_dir(repo_weights) == repo_weights

    from voice_ctl.decision import check_weights

    ok, why = check_weights(repo_weights)
    assert ok, why


# --------------------------------------------------------------------------- #
# decision_dir_for 必须和 model_dir_for 一样真的用上兜底
# --------------------------------------------------------------------------- #


def test_decision_dir_for_uses_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """runner.decision_dir_for 真的走了兜底，且兜底结果就是权重的真实位置。

    做法：把 exe_dir 指到一个**没有权重**的目录，把解包目录指到有权重的那个。
    于是裸的 decision_path() 会落空，只有经过兜底才拿得到。
    """
    from voice_ctl.config import AppConfig
    from voice_ctl.runner import decision_dir_for

    data = tmp_path / "data"          # data_dir，且刻意不放权重
    bundle = tmp_path / "bundle"      # 模拟 _MEIPASS
    data.mkdir()
    bundled = _make_weights(bundle / REL)

    monkeypatch.setattr(bootstrap, "data_dir", lambda: data)
    monkeypatch.setattr(bootstrap, "exe_dir", lambda: data)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: bundle)

    cfg = AppConfig(source=data / "config.toml")
    cfg.decision = cfg.decision.__class__(enabled=True, onnx_dir=str(REL))

    # 配置解析出来的位置并不存在（打包后的真实处境）
    assert cfg.decision_path() != bundled
    assert not cfg.decision_path().is_dir()
    # 兜底把它拉到解包目录里那份
    assert decision_dir_for(cfg) == bundled


# --------------------------------------------------------------------------- #
# fetch-decision 在权重已就绪时不该再下 900MB
# --------------------------------------------------------------------------- #


def test_fetch_decision_short_circuits(monkeypatch: pytest.MonkeyPatch, capsys):
    """权重已就绪 -> 直接返回 0，绝不发起下载。"""
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(decision, "available", lambda root: (True, "就绪：model_int8.onnx (873MB)"))

    def _boom(*a, **k):
        raise AssertionError("权重已就绪却仍然去下载了")

    monkeypatch.setattr(decision, "fetch_weights", _boom)

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=False)
    rc = cli.cmd_fetch_decision(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "无需下载" in out


def test_fetch_decision_force_bypasses_short_circuit(monkeypatch: pytest.MonkeyPatch, capsys):
    """--force 要能绕过短路（用户就是想重下）。"""
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(decision, "available", lambda root: (True, "就绪"))
    called: list = []

    def _fake_fetch(root, which):
        called.append((root, which))
        return ["model_int8.onnx (873MB)"]

    monkeypatch.setattr(decision, "fetch_weights", _fake_fetch)

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=True)
    rc = cli.cmd_fetch_decision(args)

    assert rc == 0
    assert called, "--force 应当真的发起下载"
    assert "✓" in capsys.readouterr().out


def test_fetch_decision_lands_in_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    """没给 --dir 时落盘位置必须是可写数据目录，不能是 cwd。

    旧实现（CLI 和界面各一份）拿配置里的**原始相对字符串**直接去 mkdir，
    那是相对**当前工作目录**的——用户从 C:\\Windows\\System32 里跑一次，
    906MB 就下到那儿了。
    """
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(decision, "available", lambda root: (False, "缺少 *.onnx"))
    seen: list = []

    def _fake_fetch(root, which):
        seen.append(Path(root))
        return []

    monkeypatch.setattr(decision, "fetch_weights", _fake_fetch)
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "writable")

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=False)
    cli.cmd_fetch_decision(args)
    capsys.readouterr()

    assert seen, "应当发起下载"
    assert seen[0].is_absolute()
    # 必须挂在可写数据目录下，而不是 Path.cwd()
    assert str(tmp_path / "writable") in str(seen[0])
    assert not str(seen[0]).startswith(str(Path.cwd() / "models"))


# --------------------------------------------------------------------------- #
# preflight_fetch：CLI 和界面共用的一份判断（分叉就是缺陷来源）
# --------------------------------------------------------------------------- #


def test_preflight_ready_when_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """内嵌了权重 -> already_ready + bundled，调用方不该再下。

    这里**直接钉死 resolve_decision_dir 的返回值**：本仓库里真实存在
    `models/laya-onnx/multilingual`，走真解析会在第一条（配置目录存在）
    就命中，于是永远测不到内嵌那条分支——一条"碰巧通过"的测试比没有更糟，
    因为它会让人以为内嵌路径被覆盖了。
    """
    from voice_ctl.decision import preflight_fetch

    data = tmp_path / "data"
    bundle = tmp_path / "bundle"
    data.mkdir()
    bundled = _make_weights(bundle / REL)
    monkeypatch.setattr(bootstrap, "data_dir", lambda: data)
    monkeypatch.setattr(bootstrap, "resolve_decision_dir", lambda configured: bundled)

    plan = preflight_fetch("models/laya-onnx/multilingual")
    assert plan.already_ready
    assert plan.allowed
    assert plan.bundled, "应当标出这是内嵌的那份"
    assert plan.target == bundled.resolve()


def test_preflight_not_bundled_when_config_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """权重是用户自己下的（解析结果就是配置那个）-> 不该标 bundled。"""
    from voice_ctl.decision import preflight_fetch

    mine = _make_weights(tmp_path / REL)
    monkeypatch.setattr(bootstrap, "resolve_decision_dir", lambda configured: mine)

    plan = preflight_fetch(str(mine))
    assert plan.already_ready
    assert not plan.bundled


def test_preflight_blocks_when_build_cannot_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """构建缺依赖（不是缺权重）-> allowed=False。

    这条是用户实测撞到的：完整版（不带 torch）从界面点下载，老实下完 906MB，
    用户开语义层才看到 No module named 'torch'。
    """
    from voice_ctl import decision
    from voice_ctl.decision import preflight_fetch

    monkeypatch.setattr(
        decision, "available",
        lambda root: (False, "这个精简版 exe 没带语义层（No module named 'torch'）"),
    )
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    plan = preflight_fetch("models/laya-onnx/multilingual")
    assert not plan.allowed
    assert not plan.already_ready
    assert plan.target is None
    assert "torch" in plan.reason


def test_preflight_allows_when_only_weights_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """只有权重缺失（构建是好的）-> allowed=True，目标挂在数据目录下。"""
    from voice_ctl import decision
    from voice_ctl.decision import preflight_fetch

    monkeypatch.setattr(decision, "available", lambda root: (False, "缺少 *.onnx（模型图）"))
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    plan = preflight_fetch("models/laya-onnx/multilingual")
    assert plan.allowed
    assert not plan.already_ready
    # 相对路径按目录名挂到数据目录下，绝不能是 cwd
    assert plan.target == (tmp_path / "data" / "models" / "laya-onnx" / "multilingual").resolve()


def test_preflight_absolute_path_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """配置给的是绝对路径 -> 就用它（用户显式指定）。"""
    from voice_ctl import decision
    from voice_ctl.decision import preflight_fetch

    monkeypatch.setattr(decision, "available", lambda root: (False, "缺少 *.onnx"))
    mine = tmp_path / "my-weights" / "multilingual"

    plan = preflight_fetch(str(mine))
    assert plan.allowed
    assert plan.target == mine.resolve()


def test_preflight_rejects_unknown_checkpoint():
    from voice_ctl.decision import preflight_fetch

    plan = preflight_fetch("models/laya-onnx/multilingual", "english")
    assert not plan.allowed
    assert "不认识" in plan.reason


def test_preflight_never_returns_relative_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """任何输入都不能产出相对路径——那正是 906MB 下到 cwd 的成因。"""
    from voice_ctl import decision
    from voice_ctl.decision import preflight_fetch

    monkeypatch.setattr(decision, "available", lambda root: (False, "缺少 *.onnx"))
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    for raw in ("models/laya-onnx/multilingual", "models/laya-onnx", "models", "multilingual", ""):
        plan = preflight_fetch(raw)
        assert plan.target is not None, raw
        assert plan.target.is_absolute(), f"{raw!r} -> {plan.target} 不是绝对路径"


# --------------------------------------------------------------------------- #
# 内嵌权重时自动开语义层（否则"装完即用"不成立）
# --------------------------------------------------------------------------- #

_TEMPLATE = """\
[model]
enabled = false

[decision]
# 这一层的实测精度与 torch 依赖都写在这里，
# 注释必须活下来——用户靠它判断要不要关掉。
enabled = false
model = "multilingual"
onnx_dir = "models/laya-onnx/multilingual"

[web]
enabled = true
"""


def test_enable_decision_in_template():
    out = bootstrap.enable_decision_in_template(_TEMPLATE)

    assert 'enabled = true' in out
    # 只有 [decision] 那一个被改：别的段本来怎样还怎样
    assert out.count("enabled = false") == 1          # [model] 那个没被动
    assert "[web]\nenabled = true" in out             # 本来就是 true，不受影响
    # 注释整段保留
    assert "实测精度与 torch 依赖" in out
    assert "用户靠它判断要不要关掉" in out
    # 其余键不动
    assert 'onnx_dir = "models/laya-onnx/multilingual"' in out
    assert 'model = "multilingual"' in out


def test_enable_decision_in_template_is_idempotent():
    once = bootstrap.enable_decision_in_template(_TEMPLATE)
    twice = bootstrap.enable_decision_in_template(once)

    assert once == twice


def test_enable_decision_in_template_without_section():
    """模板里没有 [decision] 段 -> 原样返回，不能凭空造一段。"""
    text = "[model]\nenabled = false\n"
    assert bootstrap.enable_decision_in_template(text) == text


def test_enable_decision_in_template_survives_garbage():
    """模板坏到解析不了时不能抛异常，也不能写坏它。"""
    text = "这不是 toml [[[ = = \n[decision\nenabled = "
    assert bootstrap.enable_decision_in_template(text) == text


def test_ensure_user_config_keeps_decision_off_even_with_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """**即使内嵌了权重**，首次生成的用户配置里语义层也是关的。

    这条守的是一个被量过后撤销的决定。当初想让内嵌权重的变体自动开语义层
    （"装完即用"），实测推翻了它：在 21 个候选动作下，语义层的置信度与正确性
    不相关——真该命中的低到 0.153，不该命中的高到 1.000（"这个多少钱" →
    web.bilibili 1.00，满置信度）。默认打开等于默认乱执行。

    权重内嵌解决的是"装完不用下载"，不是"默认该开"。这两件事必须分开。
    """
    data = tmp_path / "data"
    bundle = tmp_path / "bundle"
    data.mkdir()
    _make_weights(bundle / REL)                       # 权重**在**，但仍不自动开
    (bundle / "config.toml").write_text(_TEMPLATE, encoding="utf-8")

    monkeypatch.setattr(bootstrap, "data_dir", lambda: data)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: bundle)

    target = bootstrap.ensure_user_config()
    assert target is not None
    assert target.read_text(encoding="utf-8") == _TEMPLATE
    # 权重确实能被找到（否则这条测试就成了"因为找不到权重才没开"）
    assert bootstrap.bundled_decision_dir() is not None


def test_ensure_user_config_leaves_decision_off_without_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """没内嵌权重（精简版/完整版）-> 模板一字不改，语义层仍是关的。

    这条是防止"顺手把默认打开"的回归：那会让精简版用户一启动就看到
    "语义层启用失败"的警告。
    """
    data = tmp_path / "data"
    bundle = tmp_path / "bundle"
    data.mkdir()
    bundle.mkdir()                                     # 没有权重
    (bundle / "config.toml").write_text(_TEMPLATE, encoding="utf-8")

    monkeypatch.setattr(bootstrap, "data_dir", lambda: data)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: bundle)

    target = bootstrap.ensure_user_config()
    assert target is not None
    assert target.read_text(encoding="utf-8") == _TEMPLATE


def test_ensure_user_config_does_not_touch_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """用户已经有一份配置 -> 绝不覆盖，哪怕它是手动关掉语义层的。"""
    data = tmp_path / "data"
    bundle = tmp_path / "bundle"
    data.mkdir()
    _make_weights(bundle / REL)
    (bundle / "config.toml").write_text(_TEMPLATE, encoding="utf-8")

    mine = data / "config.toml"
    mine.write_text("[decision]\nenabled = false   # 我自己关的\n", encoding="utf-8")

    monkeypatch.setattr(bootstrap, "data_dir", lambda: data)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: bundle)

    assert bootstrap.ensure_user_config() == mine
    assert "我自己关的" in mine.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 这个构建用不了语义层时，不能骗用户下 906MB
# --------------------------------------------------------------------------- #


def test_fetch_decision_warns_when_build_cannot_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """精简版没带 torch -> 下载**之前**就警告，并要求确认。

    实测过这个坑：精简版会老实下完 906MB 并报"✓ 权重就绪"，用户跑 simulate
    才发现 "No module named 'torch'"。906MB 白下，报错还发生在用户以为
    已经装好的时候。
    """
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(
        decision, "available",
        lambda root: (False, "这个精简版 exe 没带语义层（No module named 'torch'）"),
    )
    downloaded: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: downloaded.append(root) or [])

    # 用户在提示里回答 "n"（默认就是不下载）
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=False)
    rc = cli.cmd_fetch_decision(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "加载不了语义层" in out
    assert "仍然用不了" in out
    assert "已取消" in out
    assert downloaded == [], "用户说 n 却还是下载了"


def test_fetch_decision_proceeds_when_user_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """用户明确要下就照下——警告不是拦截。"""
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(decision, "available",
                        lambda root: (False, "这个精简版 exe 没带语义层"))
    downloaded: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: downloaded.append(root) or [])
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=False)
    rc = cli.cmd_fetch_decision(args)

    assert rc == 0
    assert downloaded, "用户确认了却没下载"


def test_fetch_decision_no_warning_when_only_weights_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """权重只是没下（构建是好的）-> 正常下，不该弹那个警告。"""
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(decision, "available",
                        lambda root: (False, "缺少 *.onnx（模型图）；可跑 fetch-decision 自动下载"))
    downloaded: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: downloaded.append(root) or [])

    def _no_input(*a):
        raise AssertionError("构建没问题时不该问用户")
    monkeypatch.setattr("builtins.input", _no_input)
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=False)
    rc = cli.cmd_fetch_decision(args)

    assert rc == 0
    assert downloaded, "权重缺失时应当正常下载"
    assert "加载不了语义层" not in capsys.readouterr().out


def test_fetch_decision_force_skips_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """--force 是显式意图 -> 不打警告、不提问。"""
    import argparse

    from voice_ctl import cli, decision

    monkeypatch.setattr(decision, "available",
                        lambda root: (False, "这个精简版 exe 没带语义层"))
    downloaded: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: downloaded.append(root) or [])

    def _no_input(*a):
        raise AssertionError("--force 时不该问用户")
    monkeypatch.setattr("builtins.input", _no_input)
    monkeypatch.setattr(bootstrap, "data_dir", lambda: tmp_path / "data")

    args = argparse.Namespace(config=None, dir=None, model="multilingual", force=True)
    rc = cli.cmd_fetch_decision(args)

    assert rc == 0
    assert downloaded
