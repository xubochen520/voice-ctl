"""单实例保护 + 开机自启。

自启测试用一个**临时注册表子键**，绝不碰用户真实的 Run 键。
"""

from __future__ import annotations

import sys
import uuid

import pytest

from voice_ctl import autostart, bootstrap
from voice_ctl.singleton import SingleInstance, acquire_or_signal

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows 专有")


def uniq(prefix: str = "voice-ctl-test") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# 单实例
# --------------------------------------------------------------------------- #


@win_only
def test_second_instance_is_detected():
    name = uniq()
    a = SingleInstance(name)
    b = SingleInstance(name)
    try:
        assert a.already_running is False
        assert b.already_running is True, "同名互斥量已存在，第二个必须知道自己是第二个"
    finally:
        a.close()
        b.close()


@win_only
def test_different_names_do_not_collide():
    a, b = SingleInstance(uniq()), SingleInstance(uniq())
    try:
        assert not a.already_running and not b.already_running
    finally:
        a.close()
        b.close()


@win_only
def test_signal_reaches_the_first_instance_once():
    name = uniq()
    first = SingleInstance(name)
    second = SingleInstance(name)
    try:
        assert first.poll_signal() is False, "没人通知时不该有信号"
        assert second.signal_existing() is True
        assert first.poll_signal() is True
        assert first.poll_signal() is False, "读一次就清掉，不然窗口会被反复拉到前台"
    finally:
        first.close()
        second.close()


@win_only
def test_mutex_is_released_on_close():
    name = uniq()
    a = SingleInstance(name)
    a.close()
    b = SingleInstance(name)
    try:
        assert b.already_running is False, "第一个实例退出后，名字必须能被重新占用"
    finally:
        b.close()


@win_only
def test_acquire_or_signal():
    name = uniq()
    first = acquire_or_signal(name)
    assert first is not None
    try:
        assert acquire_or_signal(name) is None, "已经有实例在跑，第二个应拿到 None"
        assert first.poll_signal() is True, "并且已经通知了第一个"
    finally:
        first.close()


def test_close_is_idempotent():
    s = SingleInstance(uniq())
    s.close()
    s.close()
    assert s.poll_signal() is False
    assert s.signal_existing() is False


def test_non_windows_is_always_first(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("voice_ctl.singleton.sys.platform", "linux")
    s = SingleInstance(uniq())
    assert s.already_running is False
    assert s.poll_signal() is False


# --------------------------------------------------------------------------- #
# 开机自启
# --------------------------------------------------------------------------- #


@pytest.fixture()
def key():  # noqa: ANN201
    """临时 Run 键。结束时整棵删掉。"""
    if sys.platform != "win32":
        pytest.skip("Windows 专有")
    import winreg

    root = rf"Software\{uniq()}"
    run = root + r"\Run"
    yield run
    for sub in (run, root):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def test_command_has_minimized_flag_and_is_quoted():
    c = autostart.command()
    assert c.startswith('"'), "路径含空格（Program Files、中文目录）必须加引号"
    assert c.endswith("--minimized"), "开机自启不该蹦出窗口"


def test_source_run_prefers_pythonw(monkeypatch: pytest.MonkeyPatch, tmp_path):  # noqa: ANN001
    py = tmp_path / "python.exe"
    pyw = tmp_path / "pythonw.exe"
    py.write_bytes(b"")
    pyw.write_bytes(b"")
    monkeypatch.setattr(autostart.sys, "executable", str(py))
    monkeypatch.setattr(autostart.sys, "frozen", False, raising=False)
    assert "pythonw.exe" in autostart.command(), "用 python.exe 的话每次开机都闪一个黑框"
    assert "-m voice_ctl.cli ui" in autostart.command()


def test_frozen_run_uses_the_exe_directly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(autostart.sys, "executable", r"C:\Apps\voice ctl\voice-ctl.exe")
    assert autostart.command() == r'"C:\Apps\voice ctl\voice-ctl.exe" ui --minimized'


@win_only
def test_enable_disable_roundtrip(key: str):
    assert autostart.is_enabled(key=key) is False
    assert autostart.enable(key=key) is True
    assert autostart.is_enabled(key=key) is True
    assert autostart.current(key=key) == autostart.command()
    assert autostart.disable(key=key) is True
    assert autostart.is_enabled(key=key) is False


@win_only
def test_disable_when_never_enabled_is_success(key: str):
    assert autostart.disable(key=key) is True


@win_only
def test_stale_detection(key: str, monkeypatch: pytest.MonkeyPatch):
    """exe 挪了位置：登记的路径指向不存在的文件，Windows 会静默忽略——表现为"自启坏了"。"""
    assert autostart.is_stale(key=key) is False, "没登记就谈不上过期"
    autostart.enable(key=key)
    assert autostart.is_stale(key=key) is False
    monkeypatch.setattr(autostart, "command", lambda: '"D:\\新位置\\voice-ctl.exe" ui --minimized')
    assert autostart.is_stale(key=key) is True
    autostart.enable(key=key)  # 「修复」就是重新写一遍
    assert autostart.is_stale(key=key) is False


def test_non_windows_is_inert(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    assert autostart.enable() is False
    assert autostart.disable() is False
    assert autostart.is_enabled() is False


# --------------------------------------------------------------------------- #
# 数据目录覆盖
# --------------------------------------------------------------------------- #


def test_data_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path):  # noqa: ANN001
    target = tmp_path / "我的数据"
    monkeypatch.setenv("VOICE_CTL_DATA", str(target))
    assert bootstrap.data_dir() == target
    assert target.is_dir(), "覆盖的目录不存在时要自动建出来"


def test_data_dir_without_override_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VOICE_CTL_DATA", raising=False)
    assert bootstrap.data_dir() in (bootstrap.exe_dir(), bootstrap.Path.home() / ".voice-ctl")
