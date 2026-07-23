"""Batch action dispatcher (``computer_batch``) and teach-mode stubs.

This module hosts the shared action handlers used both by the individual
server tools and by ``computer_batch``. A single action is a dict like::

    {"action": "left_click", "coordinate": [x, y], "text": "ctrl"}

All coordinates are IMAGE-space of the most recent screenshot (the one taken
*before* the batch began) and are mapped to physical pixels via
:mod:`omni_computer_use.screen`.

``run_batch`` executes a list of actions sequentially, stops on the first
error, and interleaves screenshot/zoom image content into its output. The
``teach_step`` / ``teach_batch`` helpers are degraded stubs: they still execute
their actions (like a mini ``computer_batch``) and then return a screenshot.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from . import apps, clipboard, config, inputs, keyboard, screen, terminal
from .permissions import ALLOWLIST


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ActionError(RuntimeError):
    """Raised when an action cannot be executed (gating failure, bad args)."""


# --------------------------------------------------------------------------- #
# Coordinate / gating helpers
# --------------------------------------------------------------------------- #


def ensure_scale() -> screen.ScaleInfo:
    """Return the current scale, capturing one screenshot if none exists yet.

    This guarantees the very first click works even when no screenshot has
    been taken: it establishes the image<->physical mapping on demand.
    """
    scale = screen.last_scale()
    if scale is None:
        _img, scale = screen.capture()
    return scale


def _map_coordinate(coordinate: Any) -> tuple[int, int]:
    """Map an IMAGE-space ``[x, y]`` coordinate to physical pixels."""
    if (
        coordinate is None
        or not isinstance(coordinate, (list, tuple))
        or len(coordinate) != 2
    ):
        raise ActionError("coordinate must be a [x, y] pair in image space")
    scale = ensure_scale()
    return screen.image_to_physical(coordinate[0], coordinate[1], scale)


def _parse_modifiers(text: Any) -> list[str]:
    """Parse a modifier string like ``'ctrl+shift'`` into a list of names."""
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [str(t) for t in text]
    return [part for part in str(text).split("+") if part]


def gate_input() -> str | None:
    """Foreground gating performed before each INPUT action.

    Returns an optional advisory note. Raises :class:`ActionError` when the
    action must be blocked.

    * If the allowlist is empty -> error (call request_access first).
    * If ``ENFORCE_FOREGROUND`` and the frontmost app is not allowlisted ->
      error. When enforcement is off (the CCC default) the action proceeds
      silently — no advisory note is emitted.
    """
    if ALLOWLIST.is_empty():
        raise ActionError(
            "allowlist is empty; call request_access with the applications "
            "you need before performing input actions"
        )

    # Only inspect the foreground / emit a gate when enforcement is on. When it
    # is off (the CCC default) we proceed silently without any advisory note.
    if config.ENFORCE_FOREGROUND:
        fg = apps.foreground_process()
        if fg and not ALLOWLIST.is_allowed(fg):
            raise ActionError(
                f"frontmost app {fg!r} is not in the session allowlist; "
                "call request_access for it before continuing"
            )
    return None


# --------------------------------------------------------------------------- #
# Image content helpers
# --------------------------------------------------------------------------- #


def _png_bytes(image: Any) -> bytes:
    """Encode a PIL image as PNG bytes."""
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def image_content(image: Any):
    """Wrap a PIL image as FastMCP image content (PNG)."""
    from mcp.server.fastmcp import Image

    return Image(data=_png_bytes(image), format="png")


def _save_png(data: bytes, prefix: str = "screenshot") -> str:
    """Write PNG bytes to a temp file and return the path."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".png")
    import os

    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


# --------------------------------------------------------------------------- #
# Capture handlers (shared by tools + batch)
# --------------------------------------------------------------------------- #


def _screenshot_mask_rects() -> list[tuple[int, int, int, int]] | None:
    """Compute the mask rects for a screenshot.

    Only non-allowlisted-window masking is applied, and only when
    ``config.MASKING`` is on (off by default). The controlling terminal is NO
    longer masked: it is moved off-screen for the duration of the grab instead
    (see :func:`terminal.hide_for_capture`), which avoids blacking out windows
    placed on top of it. Returns ``None`` when nothing is to be masked so the
    capture path stays on its fast no-mask branch.
    """
    rects: list[tuple[int, int, int, int]] = []
    if config.MASKING:
        rects.extend(apps.mask_rects_for(ALLOWLIST.allowed_exes()))
    return rects or None


#: Monitor name already announced in a screenshot note (event-driven notes:
#: speak on the FIRST multi-monitor capture and whenever the monitor changes,
#: stay quiet on same-monitor repeats — mirrors the official computer-use).
_last_noted_monitor: str | None = None


def _monitor_note() -> str | None:
    """Compose the multi-monitor screenshot note, or ``None`` when silent."""
    global _last_noted_monitor
    try:
        mon = screen.last_capture_monitor()
        if mon is None:
            return None
        mons = screen.list_monitors()
        if len(mons) < 2:
            return None
        prev = _last_noted_monitor
        if prev == mon.name:
            return None
        _last_noted_monitor = mon.name
        others = ", ".join(
            screen.describe_monitor(m) for m in mons if m.name != mon.name
        )
        changed = (
            f', which is different from your previous screenshot (taken on "{prev}")'
            if prev
            else ""
        )
        return (
            f"This screenshot was taken on monitor {screen.describe_monitor(mon)}"
            f"{changed}. Other attached monitors: {others}. "
            "Use switch_display to capture a different monitor."
        )
    except Exception:
        return None


def do_screenshot(save_to_disk: bool = False) -> list:
    """Capture the active monitor and return MCP content (note? + image)."""
    if ALLOWLIST.is_empty():
        raise ActionError(
            "allowlist is empty; call request_access before taking a screenshot"
        )

    mask_rects = _screenshot_mask_rects()

    # Park the controlling terminal off-screen for the grab so it's absent from
    # the frame, then always restore it (even on error).
    hidden = False
    if config.SHRINK_TERMINAL:
        hidden = terminal.hide_for_capture()
    try:
        image, _scale = screen.capture(mask_rects)
    finally:
        if hidden:
            terminal.restore_after_capture()
    png = _png_bytes(image)

    content: list = []
    note = _monitor_note()
    if note:
        content.append(note)
    if save_to_disk:
        path = _save_png(png, "screenshot")
        content.append(f"Saved screenshot to {path}")
    from mcp.server.fastmcp import Image

    content.append(Image(data=png, format="png"))
    return content


def do_overview(save_to_disk: bool = False) -> list:
    """Composite all monitors into one orientation image (not a click surface).

    Same gating/masking/hide semantics as :func:`do_screenshot`, but captures
    every monitor and does NOT update the click coordinate frame.
    """
    if ALLOWLIST.is_empty():
        raise ActionError(
            "allowlist is empty; call request_access before taking a screenshot"
        )

    mask_rects = _screenshot_mask_rects()
    hidden = False
    if config.SHRINK_TERMINAL:
        hidden = terminal.hide_for_capture()
    try:
        image, note = screen.capture_overview(mask_rects)
    finally:
        if hidden:
            terminal.restore_after_capture()
    png = _png_bytes(image)

    content: list = [note]
    if save_to_disk:
        path = _save_png(png, "overview")
        content.append(f"Saved overview to {path}")
    from mcp.server.fastmcp import Image

    content.append(Image(data=png, format="png"))
    return content


def do_zoom(region: Any, save_to_disk: bool = False) -> list:
    """Crop the last screenshot region (image space) and upscale 2x."""
    if (
        region is None
        or not isinstance(region, (list, tuple))
        or len(region) != 4
    ):
        raise ActionError("region must be [x0, y0, x1, y1] in image space")

    scale = screen.last_scale()
    if scale is None:
        # Establish a fresh capture so zoom has something to crop.
        _img, scale = screen.capture(
            _screenshot_mask_rects() if not ALLOWLIST.is_empty() else None
        )

    if ALLOWLIST.is_empty():
        raise ActionError(
            "allowlist is empty; call request_access before zooming"
        )

    # Re-capture the current screen (masked) so zoom reflects live pixels, with
    # the controlling terminal parked off-screen for the grab.
    mask_rects = _screenshot_mask_rects()
    hidden = False
    if config.SHRINK_TERMINAL:
        hidden = terminal.hide_for_capture()
    try:
        image, scale = screen.capture(mask_rects)
    finally:
        if hidden:
            terminal.restore_after_capture()

    x0, y0, x1, y1 = (int(v) for v in region)
    x0, x1 = sorted((max(0, x0), min(scale.image_w, x1)))
    y0, y1 = sorted((max(0, y0), min(scale.image_h, y1)))
    if x1 <= x0 or y1 <= y0:
        raise ActionError("zoom region is empty after clamping to the image")

    from PIL import Image as PILImage

    crop = image.crop((x0, y0, x1, y1))
    crop = crop.resize(
        (max(1, (x1 - x0) * 2), max(1, (y1 - y0) * 2)), PILImage.LANCZOS
    )
    png = _png_bytes(crop)

    content: list = []
    if save_to_disk:
        path = _save_png(png, "zoom")
        content.append(f"Saved zoom to {path}")
    from mcp.server.fastmcp import Image

    content.append(Image(data=png, format="png"))
    return content


# --------------------------------------------------------------------------- #
# Single-action dispatch
# --------------------------------------------------------------------------- #

#: Mouse actions that hit-test the screen. These are wrapped in
#: ``terminal.clickthrough()`` so a synthetic click passes through the topmost
#: terminal to the target underneath. Keyboard / wait / capture actions don't
#: hit-test the terminal and are dispatched directly.
_MOUSE_ACTIONS = frozenset({
    "mouse_move", "left_click", "right_click", "middle_click",
    "double_click", "triple_click", "left_click_drag",
    "left_mouse_down", "left_mouse_up", "scroll",
})

#: Keyboard actions whose keystrokes go to the focused window. Guarded so they
#: never land in the controlling window (see the self-harm guard in run_action).
_KEYBOARD_ACTIONS = frozenset({"key", "hold_key", "type"})


def _dispatch_mouse(name: str, action: dict, emit) -> list:
    """Execute one mouse action (coordinates mapped here).

    The caller wraps this in ``terminal.clickthrough()`` (when CLICKTHROUGH is
    on), so the synthetic click passes through the click-through terminal to the
    target underneath; the terminal's hit-testing is restored immediately after.
    """
    if name == "mouse_move":
        px, py = _map_coordinate(action.get("coordinate"))
        inputs.move_to(px, py)
        return emit("Moved.")

    if name in ("left_click", "right_click", "middle_click",
                "double_click", "triple_click"):
        button = {
            "left_click": "left",
            "right_click": "right",
            "middle_click": "middle",
            "double_click": "left",
            "triple_click": "left",
        }[name]
        count = {"double_click": 2, "triple_click": 3}.get(name, 1)
        px, py = _map_coordinate(action.get("coordinate"))
        mods = _parse_modifiers(action.get("text"))
        inputs.click(px, py, button=button, count=count, modifiers=mods or None)
        return emit("Clicked.")

    if name == "left_click_drag":
        end = action.get("coordinate")
        start = action.get("start_coordinate")
        px1, py1 = _map_coordinate(end)
        if start is not None:
            px0, py0 = _map_coordinate(start)
        else:
            px0, py0 = inputs.get_cursor_pos()
        inputs.drag(px0, py0, px1, py1, button="left")
        return emit("Dragged.")

    if name == "left_mouse_down":
        inputs.mouse_down(button="left")
        return emit("Mouse down.")

    if name == "left_mouse_up":
        inputs.mouse_up(button="left")
        return emit("Mouse up.")

    if name == "scroll":
        px, py = _map_coordinate(action.get("coordinate"))
        direction = str(action.get("scroll_direction") or action.get("direction") or "down")
        amount = int(action.get("scroll_amount") or action.get("amount") or 3)
        inputs.scroll(px, py, direction, amount)
        return emit("Scrolled.")

    raise ActionError(f"unknown mouse action: {name!r}")


def run_action(action: dict) -> list:
    """Execute one action dict and return a list of MCP content items.

    The action's ``action`` key selects the operation; coordinates are in
    image space and mapped to physical pixels here. INPUT actions are gated by
    :func:`gate_input`. Capture actions (``screenshot``/``zoom``) interleave
    image content. Raises :class:`ActionError` on any failure.
    """
    if not isinstance(action, dict):
        raise ActionError(f"action must be an object, got {type(action).__name__}")

    name = action.get("action") or action.get("type")
    if not name:
        raise ActionError(
            "batch action is missing its 'action' field; each action is a flat "
            'object like {"action": "left_click", "coordinate": [x, y]} — the '
            "action name in 'action', plus that action's own arguments as sibling "
            'keys (not nested under "args"/"tool")'
        )
    name = str(name)

    # ----- capture (no input gating) -----
    if name == "screenshot":
        return do_screenshot(bool(action.get("save_to_disk", False)))
    if name == "zoom":
        return do_zoom(action.get("region"), bool(action.get("save_to_disk", False)))
    if name == "wait":
        duration = float(action.get("duration", 0) or 0)
        if duration > 0:
            time.sleep(duration)
        return [f"Waited {duration:g}s."]
    if name == "cursor_position":
        px, py = inputs.get_cursor_pos()
        ix, iy = screen.physical_to_image(px, py)
        return [f"Cursor at ({ix}, {iy})."]

    # ----- input actions (gated) -----
    note = gate_input()

    # Keyboard-target self-harm guard: synthetic keystrokes go to whatever holds
    # focus. If the controlling window (Claude Desktop in CDC / the hosting
    # terminal in CCC) is frontmost, block keyboard actions — otherwise the text
    # is typed into our own control surface (polluting the conversation; a
    # trailing Return would send a message or run a shell command). UNCONDITIONAL:
    # not tied to ENFORCE_FOREGROUND or DEV, because it guards against self-harm,
    # not against operating an un-allowlisted app. Mouse actions are exempt — a
    # click carries its own coordinate and naturally takes focus.
    if name in _KEYBOARD_ACTIONS and terminal.foreground_is_controlling():
        raise ActionError(
            "keyboard input blocked: the controlling window (Claude Desktop / "
            "the hosting terminal) has keyboard focus, so this text would land "
            "in Claude's own control surface instead of your target app. Click "
            "the target application first to move focus there, then retry."
        )

    def emit(msg: str) -> list:
        if note:
            return [note, msg]
        return [msg]

    # Mouse actions hit-test the screen, so they can land on the topmost
    # terminal parked in the corner. Make the terminal click-through for the
    # duration of the synthetic action so the click passes through to the target
    # underneath (mirrors CDC), then restore it immediately (finally) — the
    # user's real mouse is unaffected outside this brief window. Keyboard, wait,
    # and capture actions don't hit-test the terminal and are not wrapped.
    if name in _MOUSE_ACTIONS:
        # ALWAYS drop the terminal out of the way for any synthetic mouse action
        # — never condition on where the terminal "should" be. We can't assume
        # the user hasn't moved the terminal, and a wrong position guess could
        # let a click land on the terminal instead of the target. Simple and
        # safe: every AI mouse action makes the terminal yield, wherever it is.
        # Drags need the layered (CDC) controlling window dropped out of the
        # z-top so a target underneath it can start its title-bar move loop;
        # plain clicks don't, and skipping it keeps the transparent method's
        # no-z-order-blip behavior for the common case.
        cm = (
            terminal.clickthrough(drop_zorder=(name == "left_click_drag"))
            if config.CLICKTHROUGH
            else contextlib.nullcontext()
        )
        with cm:
            return _dispatch_mouse(name, action, emit)

    if name == "key":
        text = action.get("text")
        if not text:
            raise ActionError("key action requires 'text'")
        repeat = int(action.get("repeat", 1) or 1)
        keyboard.press_chord(str(text), repeat=repeat)
        return emit(f'Pressed "{text}".')

    if name == "hold_key":
        text = action.get("text")
        if not text:
            raise ActionError("hold_key action requires 'text'")
        duration = float(action.get("duration", 0) or 0)
        keyboard.hold(str(text), duration)
        return emit(f'Held "{text}" for {duration:g}s.')

    if name == "type":
        text = action.get("text")
        if text is None:
            raise ActionError("type action requires 'text'")
        keyboard.type_text(str(text))
        return emit("Typed.")

    raise ActionError(f"unknown action: {name!r}")


# --------------------------------------------------------------------------- #
# Batch runner
# --------------------------------------------------------------------------- #


def run_batch(actions: list) -> list:
    """Execute a list of actions sequentially, stopping on the first error.

    Coordinates in every action refer to the full-screen screenshot taken
    BEFORE the batch (the cached :func:`screen.last_scale`). Per-action text
    outputs are concatenated; screenshot/zoom images are interleaved as image
    content. On the first failing action, an error line is appended and the
    batch stops.

    Args:
        actions: The list of action dicts to run.

    Returns:
        A flat list of MCP content items (text strings and Image objects).
    """
    if actions is None or not isinstance(actions, (list, tuple)):
        raise ActionError("actions must be a list")

    # Establish a stable coordinate frame for the whole batch up front.
    ensure_scale()

    content: list = []
    for i, action in enumerate(actions):
        try:
            result = run_action(action)
        except Exception as exc:  # stop on first error
            label = ""
            if isinstance(action, dict):
                label = str(action.get("action") or action.get("type") or "")
            content.append(
                f"Action {i} ({label}) failed: {exc}. Stopping batch."
            )
            return content
        content.extend(result)
    return content


# --------------------------------------------------------------------------- #
# Teach-mode stubs (degraded: execute actions, then return a screenshot)
# --------------------------------------------------------------------------- #


def _execute_actions_quiet(actions: list) -> list:
    """Run a list of actions, returning their content; stop on first error."""
    content: list = []
    if not actions:
        return content
    for i, action in enumerate(actions):
        try:
            content.extend(run_action(action))
        except Exception as exc:
            content.append(f"Action {i} failed: {exc}. Stopping.")
            return content
    return content


def teach_step(
    explanation: str | None = None,
    next_preview: str | None = None,
    actions: list | None = None,
    anchor: Any = None,
) -> list:
    """Teach-mode step (phase-2 stub): execute actions, then screenshot.

    The teach overlay UI is not implemented in the CLI build, so this degrades
    to ``computer_batch``-like behavior: it runs ``actions`` and returns a
    fresh screenshot plus a note.
    """
    content: list = [
        {
            "exited": False,
            "note": "teach overlay not implemented (phase 2)",
        }
    ]
    ensure_scale()
    content.extend(_execute_actions_quiet(actions or []))
    try:
        content.extend(do_screenshot(False))
    except Exception as exc:
        content.append(f"(screenshot unavailable: {exc})")
    return content


def teach_batch(steps: list | None = None) -> list:
    """Teach-mode batch (phase-2 stub): execute each step, then final screenshot.

    Iterates ``steps`` (each a dict that may carry an ``actions`` list),
    executing their actions, and returns the final screenshot plus a note.
    """
    content: list = [
        {
            "exited": False,
            "note": "teach overlay not implemented (phase 2)",
        }
    ]
    ensure_scale()
    for step in steps or []:
        acts = step.get("actions") if isinstance(step, dict) else None
        content.extend(_execute_actions_quiet(acts or []))
    try:
        content.extend(do_screenshot(False))
    except Exception as exc:
        content.append(f"(screenshot unavailable: {exc})")
    return content
