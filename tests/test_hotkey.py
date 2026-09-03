"""Tests for hotkey parsing. Validates the (modifiers, vk) tuple that
Win32 RegisterHotKey expects. This is hard to get right; these tests
prevent regressions on edge cases like 'scroll_lock', 'pause', and
function keys."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice_hotkey
from voice_hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    VK_F1,
    VK_PAUSE,
    VK_SCROLL,
    VK_SPACE,
    parse_hotkey,
)


def test_single_letter():
    mods, vk = parse_hotkey("v")
    assert mods == 0
    assert vk != 0  # some virtual key code for 'v'


def test_ctrl_letter():
    mods, vk = parse_hotkey("ctrl+v")
    assert mods & MOD_CONTROL
    assert vk != 0


def test_ctrl_alt_letter():
    mods, vk = parse_hotkey("ctrl+alt+v")
    assert mods & MOD_CONTROL
    assert mods & MOD_ALT
    assert vk != 0


def test_ctrl_shift_alt_win():
    mods, vk = parse_hotkey("ctrl+shift+alt+win+k")
    assert mods & MOD_CONTROL
    assert mods & MOD_SHIFT
    assert mods & MOD_ALT
    assert mods & MOD_WIN
    assert vk != 0


def test_space():
    mods, vk = parse_hotkey("space")
    assert mods == 0
    assert vk == VK_SPACE


def test_ctrl_space():
    mods, vk = parse_hotkey("ctrl+space")
    assert mods & MOD_CONTROL
    assert vk == VK_SPACE


def test_scroll_lock():
    mods, vk = parse_hotkey("scroll_lock")
    assert mods == 0
    assert vk == VK_SCROLL


def test_scrolllock_no_underscore():
    mods, vk = parse_hotkey("scrolllock")
    assert vk == VK_SCROLL


def test_pause():
    mods, vk = parse_hotkey("pause")
    assert mods == 0
    assert vk == VK_PAUSE


def test_break_alias():
    mods, vk = parse_hotkey("break")
    assert vk == VK_PAUSE


def test_f1():
    mods, vk = parse_hotkey("f1")
    assert mods == 0
    assert vk == VK_F1


def test_f12():
    mods, vk = parse_hotkey("f12")
    assert vk == VK_F1 + 11


def test_pagedown():
    mods, vk = parse_hotkey("pagedown")
    assert mods == 0
    assert vk == voice_hotkey.VK_NEXT


def test_page_down_with_underscore():
    mods, vk = parse_hotkey("page_down")
    assert vk == voice_hotkey.VK_NEXT


def test_escape():
    mods, vk = parse_hotkey("escape")
    assert mods == 0
    assert vk == voice_hotkey.VK_ESCAPE


def test_esc_short():
    mods, vk = parse_hotkey("esc")
    assert vk == voice_hotkey.VK_ESCAPE


def test_numpad():
    mods, vk = parse_hotkey("numpad5")
    assert vk == voice_hotkey.VK_NUMPAD0 + 5


def test_unparseable_returns_zero():
    mods, vk = parse_hotkey("notarealkey")
    assert (mods, vk) == (0, 0)


def test_case_insensitive():
    mods1, vk1 = parse_hotkey("CTRL+ALT+V")
    mods2, vk2 = parse_hotkey("ctrl+alt+v")
    assert mods1 == mods2
    assert vk1 == vk2


def test_whitespace_around_parts():
    mods, vk = parse_hotkey(" ctrl + alt + v ")
    assert mods & MOD_CONTROL
    assert mods & MOD_ALT
    assert vk != 0
