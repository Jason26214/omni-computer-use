"""Raw Win32 mouse input via ``SendInput`` (physical / virtual-desktop coords).

All functions here operate in **physical** (virtual-desktop) pixel coordinates.
Callers must convert image-space coordinates with
:func:`omni_computer_use.screen.image_to_physical` before calling in.

Absolute mouse moves are performed by normalizing physical coordinates to the
``0..65535`` range over the whole virtual screen and sending
``MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE`` events,
which is the only reliable way to hit an exact pixel across multiple monitors.

Modifier keys for :func:`click` are pressed/released through
:mod:`omni_computer_use.keyboard` so that chord semantics stay consistent with
the keyboard layer.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

# --------------------------------------------------------------------------- #
# Win32 constants
# --------------------------------------------------------------------------- #

INPUT_MOUSE = 0

# Mouse event flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

# GetSystemMetrics indices for the virtual screen
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# One wheel "click"/notch.
WHEEL_DELTA = 120

# Number of intermediate moves for a drag.
_DRAG_STEPS = 20

# Dwell times (seconds) inserted into a drag so Windows registers it as a real
# drag rather than a fast down/up. A title-bar window move runs in a modal
# move loop (WM_NCLBUTTONDOWN -> SC_MOVE) that only engages if the button stays
# down for a moment and the pointer then moves continuously; with zero delay all
# events are injected in ~1ms and the loop can see down+up almost together and
# treat it as a click (window never moves — the "reported success but nothing
# moved" bug). These space the sequence over ~350ms, like a human drag.
_DRAG_GRAB_DWELL_S = 0.02     # settle at the grab point before pressing
_DRAG_PRESS_DWELL_S = 0.06    # hold after press so the modal move loop engages
_DRAG_STEP_DWELL_S = 0.012    # between intermediate moves (continuous motion)
_DRAG_RELEASE_DWELL_S = 0.03  # settle at the destination before releasing

_user32 = ctypes.windll.user32


# --------------------------------------------------------------------------- #
# SendInput structures
# --------------------------------------------------------------------------- #

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
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


class _INPUTunion(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTunion),
    ]


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #


def _send_mouse(dx: int, dy: int, flags: int, mouse_data: int = 0) -> None:
    """Build and dispatch a single mouse ``INPUT`` event."""
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(
        dx=dx,
        dy=dy,
        mouseData=ctypes.c_long(mouse_data).value & 0xFFFFFFFF,
        dwFlags=flags,
        time=0,
        dwExtraInfo=None,
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _virtual_screen() -> tuple[int, int, int, int]:
    """Return ``(origin_x, origin_y, width, height)`` of the virtual screen."""
    x = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, w, h


def _normalize(px: int, py: int) -> tuple[int, int]:
    """Normalize physical coords to ``0..65535`` across the virtual screen."""
    ox, oy, w, h = _virtual_screen()
    # Guard against degenerate (zero-size) metrics.
    w = w if w > 1 else 1
    h = h if h > 1 else 1
    # Map so that the pixel center lands inside its 0..65535 cell.
    nx = round((px - ox) * 65535 / (w - 1)) if w > 1 else 0
    ny = round((py - oy) * 65535 / (h - 1)) if h > 1 else 0
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    return nx, ny


def _move_abs(px: int, py: int) -> None:
    """Move the cursor to an absolute physical coordinate."""
    nx, ny = _normalize(px, py)
    _send_mouse(
        nx,
        ny,
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
    )


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _button_flags(button: str) -> tuple[int, int]:
    """Return ``(down_flag, up_flag)`` for a button name."""
    try:
        return _BUTTON_FLAGS[button.lower()]
    except KeyError:
        raise ValueError(f"unknown mouse button: {button!r}") from None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def move_to(px: int, py: int) -> None:
    """Move the cursor to a physical-pixel coordinate.

    Args:
        px: Physical x coordinate (virtual-desktop space).
        py: Physical y coordinate (virtual-desktop space).
    """
    _move_abs(px, py)


def click(
    px: int,
    py: int,
    button: str = "left",
    count: int = 1,
    modifiers: list[str] | None = None,
) -> None:
    """Click at a physical-pixel coordinate, optionally with modifier keys held.

    Moves to ``(px, py)`` then issues ``count`` button down/up pairs for the
    given mouse button. If ``modifiers`` is provided, those keys are pressed
    before the click and released after (via the keyboard layer), enabling
    e.g. ctrl-click or shift-click.

    Args:
        px: Physical x coordinate.
        py: Physical y coordinate.
        button: One of ``'left'``, ``'right'``, ``'middle'``.
        count: Number of clicks (2 = double-click, 3 = triple-click).
        modifiers: Optional modifier key names (e.g. ``['ctrl', 'shift']``) to
            hold for the duration of the click.
    """
    # Lazy import to avoid a circular import (keyboard -> keymap; both layers
    # are independent of inputs, but importing at call time is the safest way
    # to keep module import order flexible).
    from . import keyboard, keymap

    down_flag, up_flag = _button_flags(button)

    vks: list[int] = []
    if modifiers:
        vks = [keymap.to_vk(name) for name in modifiers]

    if vks:
        keyboard.press_keys(vks)
    try:
        _move_abs(px, py)
        for _ in range(max(1, count)):
            _send_mouse(0, 0, down_flag)
            _send_mouse(0, 0, up_flag)
    finally:
        if vks:
            keyboard.release_keys(vks)


def mouse_down(
    px: int | None = None, py: int | None = None, button: str = "left"
) -> None:
    """Press (and hold) a mouse button.

    Args:
        px: Optional physical x to move to first; if ``None``, presses at the
            current cursor position.
        py: Optional physical y to move to first; if ``None``, presses at the
            current cursor position.
        button: One of ``'left'``, ``'right'``, ``'middle'``.
    """
    down_flag, _ = _button_flags(button)
    if px is not None and py is not None:
        _move_abs(px, py)
    _send_mouse(0, 0, down_flag)


def mouse_up(
    px: int | None = None, py: int | None = None, button: str = "left"
) -> None:
    """Release a previously pressed mouse button.

    Args:
        px: Optional physical x to move to first; if ``None``, releases at the
            current cursor position.
        py: Optional physical y to move to first; if ``None``, releases at the
            current cursor position.
        button: One of ``'left'``, ``'right'``, ``'middle'``.
    """
    _, up_flag = _button_flags(button)
    if px is not None and py is not None:
        _move_abs(px, py)
    _send_mouse(0, 0, up_flag)


def drag(px0: int, py0: int, px1: int, py1: int, button: str = "left") -> None:
    """Press at one point, move to another, then release (a drag).

    Args:
        px0: Physical x of the drag start.
        py0: Physical y of the drag start.
        px1: Physical x of the drag end.
        py1: Physical y of the drag end.
        button: Mouse button to hold during the drag.
    """
    down_flag, up_flag = _button_flags(button)

    _move_abs(px0, py0)
    time.sleep(_DRAG_GRAB_DWELL_S)
    _send_mouse(0, 0, down_flag)
    time.sleep(_DRAG_PRESS_DWELL_S)
    try:
        for step in range(1, _DRAG_STEPS + 1):
            t = step / _DRAG_STEPS
            ix = round(px0 + (px1 - px0) * t)
            iy = round(py0 + (py1 - py0) * t)
            _move_abs(ix, iy)
            time.sleep(_DRAG_STEP_DWELL_S)
    finally:
        _move_abs(px1, py1)
        time.sleep(_DRAG_RELEASE_DWELL_S)
        _send_mouse(0, 0, up_flag)


def scroll(px: int, py: int, direction: str, amount: int) -> None:
    """Scroll the wheel at a physical-pixel coordinate.

    Moves the cursor to ``(px, py)`` first, then sends wheel events. Vertical
    directions use ``MOUSEEVENTF_WHEEL``; horizontal directions use
    ``MOUSEEVENTF_HWHEEL``.

    Direction sign follows native conventions: positive wheel data scrolls
    up / right, negative scrolls down / left; one tick is ``WHEEL_DELTA``
    (120) units.

    Args:
        px: Physical x coordinate to scroll over.
        py: Physical y coordinate to scroll over.
        direction: One of ``'up'``, ``'down'``, ``'left'``, ``'right'``.
        amount: Number of wheel ticks (notches) to scroll.
    """
    _move_abs(px, py)

    d = direction.lower()
    ticks = abs(amount)
    if d == "up":
        flag, delta = MOUSEEVENTF_WHEEL, WHEEL_DELTA
    elif d == "down":
        flag, delta = MOUSEEVENTF_WHEEL, -WHEEL_DELTA
    elif d == "right":
        flag, delta = MOUSEEVENTF_HWHEEL, WHEEL_DELTA
    elif d == "left":
        flag, delta = MOUSEEVENTF_HWHEEL, -WHEEL_DELTA
    else:
        raise ValueError(f"unknown scroll direction: {direction!r}")

    for _ in range(ticks):
        _send_mouse(0, 0, flag, mouse_data=delta)


def get_cursor_pos() -> tuple[int, int]:
    """Return the current cursor position in physical pixels.

    Uses ``GetCursorPos``; because the process is Per-Monitor-V2 DPI aware, the
    returned coordinates are already physical pixels.

    Returns:
        ``(px, py)`` physical-pixel coordinates.
    """
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)
