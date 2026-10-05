"""热键解析与状态机测试（不真正挂钩子，不需要管理员权限）。"""

from __future__ import annotations

import pytest

from voice_ctl.hotkey import (
    HotkeyError,
    HotkeyTimer,
    key_to_name,
    parse_hotkey,
)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("<ctrl>+<alt>+space", {"ctrl", "alt", "space"}),
        ("<ctrl>+<shift>+j", {"ctrl", "shift", "j"}),
        ("<f9>", {"f9"}),
        ("ctrl+alt+w", {"ctrl", "alt", "w"}),
        ("<Control>+<Alt>+<Space>", {"ctrl", "alt", "space"}),
        ("<win>+<alt>+q", {"cmd", "alt", "q"}),
    ],
)
def test_parse_hotkey(spec: str, expected: set[str]):
    assert set(parse_hotkey(spec).keys) == expected


def test_parse_hotkey_normalizes_aliases():
    assert set(parse_hotkey("<control>+<escape>").keys) == {"ctrl", "esc"}
    assert set(parse_hotkey("<return>+<option>").keys) == {"enter", "alt"}


def test_parse_hotkey_display_is_stable():
    hk = parse_hotkey("<alt>+<ctrl>+space")
    assert hk.display == "alt+ctrl+space"
    assert str(hk) == "alt+ctrl+space"


def test_parse_hotkey_rejects_empty():
    with pytest.raises(HotkeyError, match="不能为空"):
        parse_hotkey("")
    with pytest.raises(HotkeyError, match="不能为空"):
        parse_hotkey("   ")


def test_parse_hotkey_rejects_duplicates():
    with pytest.raises(HotkeyError, match="重复"):
        parse_hotkey("<ctrl>+<control>")


def test_parse_hotkey_rejects_only_separators():
    with pytest.raises(HotkeyError, match="没有任何键"):
        parse_hotkey("+")


def test_key_to_name_handles_char_keys():
    from pynput import keyboard

    assert key_to_name(keyboard.KeyCode.from_char("j")) == "j"
    assert key_to_name(keyboard.KeyCode.from_char("J")) == "j"


def test_key_to_name_handles_special_keys():
    from pynput import keyboard

    assert key_to_name(keyboard.Key.space) == "space"
    assert key_to_name(keyboard.Key.ctrl_l) == "ctrl"
    assert key_to_name(keyboard.Key.ctrl_r) == "ctrl"
    assert key_to_name(keyboard.Key.alt_gr) == "alt_gr"


def test_key_to_name_ignores_control_chars():
    """Ctrl+A 会给出 '\\x01'，不能把它当成可匹配的字符键。"""
    from pynput import keyboard

    assert key_to_name(keyboard.KeyCode.from_char("\x01")) is None


# --------------------------------------------------------------------------- #
# 状态机（直接喂事件，不挂钩子）
# --------------------------------------------------------------------------- #


class FakeListener:
    """绕过 pynput，只测状态机逻辑。"""

    def __init__(self, spec: str):
        from voice_ctl.hotkey import HotkeyListener

        self.pressed = 0
        self.released = 0
        self.hk = HotkeyListener(
            spec,
            on_press=self._p,
            on_release=self._r,
        )

    def _p(self) -> None:
        self.pressed += 1

    def _r(self) -> None:
        self.released += 1

    def press(self, key) -> None:  # noqa: ANN001
        self.hk._handle_press(key)

    def release(self, key) -> None:  # noqa: ANN001
        self.hk._handle_release(key)


def test_state_machine_fires_once_on_full_combo():
    from pynput import keyboard

    f = FakeListener("<ctrl>+<alt>+space")
    f.press(keyboard.Key.ctrl_l)
    assert f.pressed == 0, "只按下 ctrl 不该触发"
    f.press(keyboard.Key.alt_l)
    assert f.pressed == 0, "还差 space"
    f.press(keyboard.Key.space)
    assert f.pressed == 1, "组合完整应当触发一次"

    f.press(keyboard.Key.space)  # 重复按下（自动重复）
    assert f.pressed == 1, "长按/自动重复不该重复触发"

    f.release(keyboard.Key.space)
    assert f.released == 1
    f.release(keyboard.Key.alt_l)
    f.release(keyboard.Key.ctrl_l)
    assert f.released == 1, "松开只触发一次"


def test_state_machine_release_any_component_ends():
    from pynput import keyboard

    f = FakeListener("<ctrl>+<alt>+j")
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)
    f.press(keyboard.KeyCode.from_char("j"))
    assert f.pressed == 1
    f.release(keyboard.Key.ctrl_l)  # 松开 ctrl 而不是 j
    assert f.released == 1


def test_state_machine_unrelated_keys_ignored():
    from pynput import keyboard

    f = FakeListener("<f9>")
    f.press(keyboard.KeyCode.from_char("a"))
    f.press(keyboard.KeyCode.from_char("b"))
    assert f.pressed == 0
    f.press(keyboard.Key.f9)
    assert f.pressed == 1
    f.release(keyboard.KeyCode.from_char("a"))
    assert f.released == 0, "松开无关键不该结束"
    f.release(keyboard.Key.f9)
    assert f.released == 1


def test_force_release_recovers_from_stuck_key():
    """丢事件导致卡在按下态时，force_release 必须能恢复。

    注意 force_release 只清内部状态：物理上 ctrl/alt 可能还按着。所以恢复
    的判据是「松开这些键再重新按一遍能正常触发」，而不是「再按 space 就触发」。
    """
    from pynput import keyboard

    f = FakeListener("<ctrl>+<alt>+space")
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)
    f.press(keyboard.Key.space)
    assert f.pressed == 1
    f.hk.force_release()
    assert f.released == 1

    # 用户松手 → 重新按一遍组合键
    f.release(keyboard.Key.space)
    f.release(keyboard.Key.alt_l)
    f.release(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)
    f.press(keyboard.Key.space)
    assert f.pressed == 2, "恢复后应当能再次触发"


def test_force_release_is_noop_when_idle():
    f = FakeListener("<f9>")
    f.hk.force_release()
    assert f.released == 0, "空闲时 force_release 不该触发 on_release"


def test_second_press_works_after_clean_release():
    from pynput import keyboard

    f = FakeListener("<f9>")
    for _ in range(3):
        f.press(keyboard.Key.f9)
        f.release(keyboard.Key.f9)
    assert f.pressed == 3
    assert f.released == 3


# --------------------------------------------------------------------------- #
# 超时守护
# --------------------------------------------------------------------------- #


def test_timer_fires_after_deadline():
    import time

    fired = []
    t = HotkeyTimer(60, lambda: fired.append(1))
    t.arm()
    assert t.tick() is False
    time.sleep(0.08)
    assert t.tick() is True
    assert len(fired) == 1
    assert t.tick() is False, "只该触发一次"


def test_timer_disarm_prevents_fire():
    import time

    fired = []
    t = HotkeyTimer(20, lambda: fired.append(1))
    t.arm()
    t.disarm()
    time.sleep(0.05)
    assert t.tick() is False
    assert fired == []


def test_timer_not_armed_never_fires():
    fired = []
    t = HotkeyTimer(1, lambda: fired.append(1))
    assert t.tick() is False
    assert fired == []


# --------------------------------------------------------------------------- #
# 真实 pynput 翻译：按住 Ctrl 时字母键的 char 会变成控制字符
# --------------------------------------------------------------------------- #
#
# 上面那些测试都是手造 KeyCode.from_char("j")，绕开了 pynput 真正的行为。
# 实测（pynput 1.8.2 / Windows）：Ctrl+J 的 char 是 '\n'，Ctrl+Alt+J 的 char 是 None，
# 于是 `<ctrl>+<alt>+j` 这类 README 写着"支持"的热键永远凑不齐。
# 这里用 pynput 自己的 KeyTranslator 并伪造修饰键状态，不注入任何真实按键。

import sys  # noqa: E402

win_only = pytest.mark.skipif(sys.platform != "win32", reason="依赖 Windows 的虚拟键码")


def _real_keycode(vk: int, *held: int):  # noqa: ANN202
    from pynput import keyboard
    from pynput._util.win32 import KeyTranslator

    kt = KeyTranslator()
    kt._GetAsyncKeyState = lambda code: 0x8000 if code in held else 0  # type: ignore[method-assign]
    return keyboard.KeyCode(**kt(vk, True))


@win_only
@pytest.mark.parametrize("mods", ["ctrl", "ctrl+shift", "ctrl+alt", "ctrl+alt+shift"])
def test_letter_key_name_survives_modifiers(mods: str):
    from pynput._util import win32_vks as V

    table = {"ctrl": V.CONTROL, "shift": V.SHIFT, "alt": V.MENU}
    held = tuple(table[m] for m in mods.split("+"))
    key = _real_keycode(0x4A, *held)  # 'J'
    assert key_to_name(key) == "j", f"{mods}+J 被认成了 {key_to_name(key)!r}（char={key.char!r}）"


@win_only
def test_digit_key_name_survives_modifiers():
    from pynput._util import win32_vks as V

    assert key_to_name(_real_keycode(0x35, V.CONTROL, V.MENU)) == "5"


@win_only
def test_ctrl_alt_letter_hotkey_actually_fires_with_real_translation():
    from pynput import keyboard
    from pynput._util import win32_vks as V

    f = FakeListener("<ctrl>+<alt>+j")
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)
    f.press(_real_keycode(0x4A, V.CONTROL, V.MENU))
    assert f.pressed == 1, "Ctrl+Alt+J 在真实按键翻译下没有触发"


def test_key_to_name_vk_path_is_windows_only(monkeypatch: pytest.MonkeyPatch):
    """别的平台的 vk 是另一套编号，拿 Windows 的表去认会把热键悄悄认成别的键。"""
    from pynput import keyboard

    monkeypatch.setattr("voice_ctl.hotkey.sys.platform", "linux")
    k = keyboard.KeyCode(vk=0x4A, char="x")
    assert key_to_name(k) == "x"


# --------------------------------------------------------------------------- #
# 预热：修饰键按齐、主键还没按时提前开麦克风
# --------------------------------------------------------------------------- #


class ArmListener:
    def __init__(self, spec: str, *, arm_raises: bool = False) -> None:
        from voice_ctl.hotkey import HotkeyListener

        self.log: list[str] = []

        def on_arm() -> None:
            self.log.append("arm")
            if arm_raises:
                raise RuntimeError("开流失败")

        self.hk = HotkeyListener(
            spec,
            on_press=lambda: self.log.append("press"),
            on_release=lambda: self.log.append("release"),
            on_arm=on_arm,
            on_disarm=lambda: self.log.append("disarm"),
        )

    def press(self, key) -> None:  # noqa: ANN001
        self.hk._handle_press(key)

    def release(self, key) -> None:  # noqa: ANN001
        self.hk._handle_release(key)


def test_arms_when_all_modifiers_down_before_main_key():
    from pynput import keyboard

    f = ArmListener("<ctrl>+<alt>+space")
    f.press(keyboard.Key.ctrl_l)
    assert f.log == [], "只按了一个修饰键，还不到预热的时候"
    f.press(keyboard.Key.alt_l)
    assert f.log == ["arm"], "修饰键按齐就该预热——这时手指还在往空格挪"
    f.press(keyboard.Key.alt_l)  # 自动重复
    assert f.log == ["arm"], "自动重复不该重复预热"
    f.press(keyboard.Key.space)
    assert f.log == ["arm", "press"]


def test_rearms_after_release_if_modifiers_still_held():
    """录音一停流就关了；Ctrl+Alt 没松的话，下一次按空格还得是"已预热"的。"""
    from pynput import keyboard

    f = ArmListener("<ctrl>+<alt>+space")
    for k in (keyboard.Key.ctrl_l, keyboard.Key.alt_l, keyboard.Key.space):
        f.press(k)
    f.release(keyboard.Key.space)
    assert f.log == ["arm", "press", "release", "arm"]
    f.release(keyboard.Key.alt_l)
    assert f.log[-1] == "disarm", "修饰键一松就该取消预热，不能让麦克风一直开着"


def test_disarms_when_modifier_released_without_pressing_main_key():
    from pynput import keyboard

    f = ArmListener("<ctrl>+<alt>+space")
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)
    f.release(keyboard.Key.alt_l)
    assert f.log == ["arm", "disarm"]
    f.release(keyboard.Key.ctrl_l)
    assert f.log == ["arm", "disarm"], "已经取消了，不该重复取消"


def test_single_key_hotkey_never_arms():
    from pynput import keyboard

    f = ArmListener("<f9>")
    f.press(keyboard.Key.f9)
    f.release(keyboard.Key.f9)
    assert f.log == ["press", "release"], "没有修饰键就没有可提前的信号"


def test_arm_callback_failure_does_not_break_the_hotkey():
    from pynput import keyboard

    f = ArmListener("<ctrl>+<alt>+space", arm_raises=True)
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)  # on_arm 抛异常
    f.press(keyboard.Key.space)
    assert "press" in f.log, "预热只是优化，它出错不能让热键失灵"


def test_unrelated_key_while_armed_does_not_disarm():
    from pynput import keyboard

    f = ArmListener("<ctrl>+<alt>+space")
    f.press(keyboard.Key.ctrl_l)
    f.press(keyboard.Key.alt_l)
    f.press(keyboard.KeyCode.from_char("a"))
    f.release(keyboard.KeyCode.from_char("a"))
    assert f.log == ["arm"]


def test_listener_without_callbacks_behaves_as_before():
    from pynput import keyboard

    f = FakeListener("<ctrl>+<alt>+space")
    for k in (keyboard.Key.ctrl_l, keyboard.Key.alt_l, keyboard.Key.space):
        f.press(k)
    f.release(keyboard.Key.space)
    assert (f.pressed, f.released) == (1, 1)
