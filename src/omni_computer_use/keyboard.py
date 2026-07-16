"""Keyboard injection via Win32 ``SendInput`` (key chords, holds, Unicode typing).

Builds on :mod:`omni_computer_use.keymap` for name->VK resolution and chord
parsing. Modifier handling presses keys down in order and releases them in
reverse, so nested chords behave correctly.

Text typing uses ``KEYEVENTF_UNICODE`` so arbitrary Unicode (including CJK)
types correctly regardless of the active keyboard layout; surrogate pairs are
emitted as two events, and ``'\\n'`` is sent as Return.

All stubs raise :class:`NotImplementedError`.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from . import keymap

# --- SendInput structures & flags -------------------------------------------

INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Keys that require the EXTENDEDKEY flag for correct behavior.
_EXTENDED_VKS: frozenset[int] = frozenset(
    {
        keymap.VK_RIGHT,
        keymap.VK_LEFT,
        keymap.VK_UP,
        keymap.VK_DOWN,
        keymap.VK_PRIOR,
        keymap.VK_NEXT,
        keymap.VK_END,
        keymap.VK_HOME,
        keymap.VK_INSERT,
        keymap.VK_DELETE,
        keymap.VK_DIVIDE,
        keymap.VK_NUMLOCK,
        keymap.VK_RCONTROL,
        keymap.VK_RMENU,
        keymap.VK_LWIN,
        keymap.VK_RWIN,
        keymap.VK_APPS,
        keymap.VK_SNAPSHOT,
    }
)

ULONG_PTR = ctypes.POINTER(wintypes.ULONG)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_SendInput = _user32.SendInput
_SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_SendInput.restype = wintypes.UINT


def _make_vk_input(vk: int, key_up: bool) -> INPUT:
    """Build an INPUT for a virtual-key down/up event."""
    flags = 0
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    if key_up:
        flags |= KEYEVENTF_KEYUP
    ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = ki
    return inp


def _make_unicode_input(code_unit: int, key_up: bool) -> INPUT:
    """Build an INPUT that emits a single UTF-16 code unit via KEYEVENTF_UNICODE."""
    flags = KEYEVENTF_UNICODE
    if key_up:
        flags |= KEYEVENTF_KEYUP
    ki = KEYBDINPUT(
        wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=None
    )
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = ki
    return inp


def _send(inputs: list[INPUT]) -> None:
    """Dispatch a list of INPUT events via SendInput."""
    if not inputs:
        return
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = _SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)


def press_keys(vks: list[int]) -> None:
    """Press (key-down) each virtual-key code in order.

    Args:
        vks: Virtual-key codes to press down, in the given order. Typically
            modifiers, applied via ``SendInput`` keyboard events.
    """
    inputs = [_make_vk_input(vk, key_up=False) for vk in vks]
    _send(inputs)


def release_keys(vks: list[int]) -> None:
    """Release (key-up) each virtual-key code in reverse order.

    Args:
        vks: Virtual-key codes to release; released in reverse of the given
            order to mirror :func:`press_keys`.
    """
    inputs = [_make_vk_input(vk, key_up=True) for vk in reversed(vks)]
    _send(inputs)


def press_chord(text: str, repeat: int = 1) -> None:
    """Press a chord, tapping the main key ``repeat`` times.

    Parses ``text`` via :func:`keymap.parse_chord`, holds the modifier keys
    down, taps the main key ``repeat`` times, then releases the modifiers.

    Args:
        text: A chord string, e.g. ``'ctrl+a'`` or ``'Return'``.
        repeat: Number of times to tap the main key (default 1).
    """
    vks = keymap.parse_chord(text)
    if not vks:
        return
    if repeat < 1:
        repeat = 1

    modifiers = vks[:-1]
    main = vks[-1]

    press_keys(modifiers)
    try:
        for _ in range(repeat):
            _send([_make_vk_input(main, key_up=False)])
            _send([_make_vk_input(main, key_up=True)])
    finally:
        release_keys(modifiers)


def hold(text: str, duration: float) -> None:
    """Hold a chord down for a duration, then release it.

    Presses the full chord (modifiers and main key) down, sleeps for
    ``duration`` seconds, then releases everything in reverse order.

    Args:
        text: A chord string to hold.
        duration: Seconds to hold the chord before releasing.
    """
    vks = keymap.parse_chord(text)
    if not vks:
        return

    press_keys(vks)
    try:
        if duration and duration > 0:
            time.sleep(duration)
    finally:
        release_keys(vks)


def release_stuck_modifiers() -> None:
    """Panic-release every modifier key (safety net; never raises).

    A synthetic chord normally releases its modifiers in a ``finally``, but if
    the process dies BETWEEN the press and the release, the OS keeps the
    modifier logically held forever — wedging the user's own shortcuts (e.g.
    every Win+ combo). Called on session deactivate / process exit, this sends
    a key-up for every modifier unconditionally; a key-up for a key that is
    already up is a harmless no-op.
    """
    # Generic + left/right specific: Shift, Ctrl, Alt, Win.
    mods = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)
    try:
        _send([_make_vk_input(vk, key_up=True) for vk in mods])
    except Exception:
        pass


def type_text(text: str) -> None:
    """Type arbitrary Unicode text via ``KEYEVENTF_UNICODE``.

    Each character is sent as a Unicode key event independent of keyboard
    layout; surrogate pairs (characters outside the BMP) are emitted as two
    code-unit events. Newlines (``'\\n'``) are sent as Return.

    Args:
        text: The text to type.
    """
    if not text:
        return

    # Send one character at a time with a tiny pause between, so the target app
    # renders the text progressively and has ALL of it by the time this call
    # returns. A single huge SendInput burst is injected faster than apps like
    # the modern Notepad process their input queue, so an immediate screenshot
    # can catch a partial render even though every event was delivered.
    char_delay = 0.004
    for ch in text:
        if ch in ("\n", "\r"):
            _send([
                _make_vk_input(keymap.VK_RETURN, key_up=False),
                _make_vk_input(keymap.VK_RETURN, key_up=True),
            ])
        else:
            # Encode as UTF-16, one Unicode event per code unit, so characters
            # outside the BMP (emoji, rare CJK) send their surrogate pair.
            events: list[INPUT] = []
            for unit in _utf16_code_units(ch):
                events.append(_make_unicode_input(unit, key_up=False))
                events.append(_make_unicode_input(unit, key_up=True))
            _send(events)
        time.sleep(char_delay)


def _utf16_code_units(ch: str) -> list[int]:
    """Return the UTF-16 code unit(s) for a single character.

    A BMP character yields one unit; a supplementary character yields a
    surrogate pair (two units).
    """
    encoded = ch.encode("utf-16-le")
    return [
        encoded[i] | (encoded[i + 1] << 8) for i in range(0, len(encoded), 2)
    ]
