"""界面「设置」页那个「下载语义层权重」按钮的行为。

这个文件的存在本身就是一次教训。0.3.4 修了 CLI 的 `fetch-decision`（构建加载
不了语义层就别下 906MB），修完还验证过。**但界面走的是另一份代码**，
那道检查没被加上——用户从界面点下载，906MB 白下，开语义层才看到
`No module named 'torch'`。

所以这里测的是**按钮本身**，不是共享函数。共享函数那边
`tests/test_decision_bundle.py` 已经覆盖了；这里要保证界面真的调它、
并且把"不能下"如实变成用户看得见的东西。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from voice_ctl import bootstrap, events

pytest.importorskip("tkinter")

# cfg_path / tcl_root / app 三个 fixture 在 conftest.py 里（与 test_ui_smoke 共用）
pytestmark = pytest.mark.usefixtures("app")


def _since() -> int:
    """当前事件 seq，用作断言起点。

    只取 seq 之后的新事件，避免上一个测试的残留事件干扰。
    """
    snap = events.get_bus().since(0)
    return snap[-1].seq if snap else 0


def _texts(start: int) -> str:
    """start 之后所有事件的文本。

    **用轮询 `since()` 而不是 `subscribe()`**，两个原因：

    1. `EventBus.close()` 会把 `_closed` 永久置真（events.py:213），而 conftest
       的 app fixture 每次 teardown 都会 close 那条 module 级总线。于是同文件里
       **只有第一个测试**能收到 sink 派发，后面的全哑——实测表现正是
       "单跑通过、一起跑必挂"。
    2. 界面本来就是轮询消费事件（`since()`，不依赖后台线程），走同一条路
       才是真实用法。
    """
    return "\n".join(e.text for e in events.get_bus().since(start))


def pump(app, n: int = 6) -> None:  # noqa: ANN001
    for _ in range(n):
        app.root.update()
        time.sleep(0.02)


def _wait_text(start: int, needle: str, app, timeout: float = 3.0) -> str:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        app.root.update()
        if needle in _texts(start):
            return _texts(start)
        time.sleep(0.02)
    return _texts(start)


def _wait_for(pred, timeout: float = 3.0) -> bool:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# --------------------------------------------------------------------------- #
# 核心：构建加载不了时，按钮不能下载
# --------------------------------------------------------------------------- #


def test_button_refuses_when_build_cannot_load(app, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001
    """这是用户实际撞到的场景：完整版没带 torch，从界面点下载。

    以前它会老实下完 906MB 并只报一句「开始下载…」。现在必须**拒绝**，
    并说清缺的是构建而不是权重。
    """
    from voice_ctl import decision

    monkeypatch.setattr(
        decision, "available",
        lambda root: (False, "这个精简版 exe 没带语义层（No module named 'torch'）"),
    )
    started: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: started.append(root) or [])

    app.show_tab("settings")
    start = _since()
    app.tabs["settings"]._download_decision()
    text = _wait_text(start, "加载不了语义层", app)

    assert "加载不了语义层" in text, text
    assert "torch" in text
    # 关键：什么都没下，也没有"开始下载"那句误导的话
    assert started == [], "拒绝了却还是发起了下载"
    assert "开始下载" not in text, text


def test_button_says_ready_when_bundled(app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN001
    """权重已就绪（内嵌）-> 报好消息，不下载。"""
    from voice_ctl import decision
    from voice_ctl.decision import FetchPlan

    plan = FetchPlan(True, "可用—— 就绪：model_int8.onnx (873MB)", True,
                     tmp_path / "w" / "multilingual", bundled=True)
    monkeypatch.setattr(decision, "preflight_fetch", lambda *a, **k: plan)
    started: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: started.append(root) or [])

    app.show_tab("settings")
    start = _since()
    app.tabs["settings"]._download_decision()
    text = _wait_text(start, "无需下载", app)

    assert "无需下载" in text, text
    assert "打包内嵌" in text
    assert started == []


def test_button_downloads_to_plan_target(app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN001
    """构建能用且缺权重 -> 下载，且落盘用 preflight 算出来的绝对路径。

    这里守的是另一个 0.3.4 只修了 CLI 的缺陷：界面以前用
    `self._onnx_dir.get()` 的**原始相对字符串**去 mkdir，那是相对当前工作目录。
    """
    from voice_ctl import decision
    from voice_ctl.decision import FetchPlan

    target = (tmp_path / "data" / "models" / "laya-onnx" / "multilingual").resolve()
    plan = FetchPlan(True, "缺少 *.onnx（模型图）", False, target)
    monkeypatch.setattr(decision, "preflight_fetch", lambda *a, **k: plan)
    seen: list = []
    monkeypatch.setattr(decision, "fetch_weights",
                        lambda root, which: seen.append(Path(root)) or [])

    app.show_tab("settings")
    start = _since()
    app.tabs["settings"]._download_decision()
    assert _wait_for(lambda: bool(seen)), "没有发起下载"
    text = _wait_text(start, "开始下载", app)

    assert seen[0] == target
    assert seen[0].is_absolute(), "落盘路径必须是绝对的，否则会落到 cwd"
    assert "开始下载" in text, text
    assert str(target) in text


def test_button_reports_unknown_checkpoint(app, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001
    """模型名不认识 -> 报错，且不去碰 preflight（避免无谓的权重探测）。"""
    from voice_ctl import decision

    def _boom(*a, **k):
        raise AssertionError("模型名不认识时不该走到 preflight")
    monkeypatch.setattr(decision, "preflight_fetch", _boom)

    app.show_tab("settings")
    tab = app.tabs["settings"]
    tab._decision_model.set("english-nonexistent")
    start = _since()
    tab._download_decision()
    text = _wait_text(start, "不认识", app)

    assert "不认识" in text, text


def test_download_uses_resolved_dir_not_raw_config(app, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001
    """界面必须把**配置里的相对路径**交给 preflight，而不是自己拼。

    这条是"别再把判断分叉出去"的守卫：只要有人又在界面里手写一遍路径逻辑，
    这条就会因为 preflight 没被调用而失败。
    """
    from voice_ctl import decision
    from voice_ctl.decision import preflight_fetch as real

    calls: list = []

    def spy(configured, *a, **k):
        calls.append(configured)
        return real(configured, *a, **k)

    monkeypatch.setattr(decision, "preflight_fetch", spy)

    app.show_tab("settings")
    tab = app.tabs["settings"]
    tab._download_decision()

    assert calls, "界面没有调用 preflight_fetch —— 判断又被分叉了"
    # 传进去的应当是配置里那个值（相对路径），由 preflight 负责绝对化
    assert calls[0] == tab._onnx_dir.get().strip()
