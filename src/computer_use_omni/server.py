"""FastMCP server — registers the 27 computer-use tools.

This module wires the full tool surface defined in ``SPEC.md`` into a FastMCP
application, dispatching into :mod:`screen`, :mod:`inputs`, :mod:`keyboard`,
:mod:`apps`, :mod:`permissions`, :mod:`clipboard` and :mod:`batch`.

Coordinate convention: every mouse/move/scroll/drag/zoom coordinate from a tool
input is in IMAGE space of the most recent screenshot; it is mapped to physical
pixels via :func:`computer_use_omni.screen.image_to_physical`. If no screenshot
has been taken yet, the first input action captures one to establish scale.

Foreground gating is permissive by default (``ENFORCE_FOREGROUND`` off): input
actions proceed even when the frontmost app is not allowlisted, but the
allowlist must be non-empty (call ``request_access`` first).
"""

from __future__ import annotations

import atexit
import functools
import sys
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import apps, batch, clipboard, config, inputs, logsetup, screen
from .permissions import ALLOWLIST

#: The FastMCP application instance with all 27 tools registered below.
app = FastMCP("computer-use-omni")

#: Persistent rotating log (<project>/logs/mcp.log). Set up once; never raises.
_LOG_PATH = logsetup.setup()
logsetup.info(
    "=== server start === "
    f"DEV={config.DEV} GLOW={config.GLOW} SHRINK_TERMINAL={config.SHRINK_TERMINAL} "
    f"PILL={config.PILL} MASKING={config.MASKING} "
    f"ENFORCE_FOREGROUND={config.ENFORCE_FOREGROUND} MAX_PIXELS={config.MAX_PIXELS}"
)
try:
    print(f"[server] logging to {_LOG_PATH}", file=sys.stderr, flush=True)
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Session activation / cleanup (CDC-style visuals)
# --------------------------------------------------------------------------- #

#: Guards one-time activation; set once the visuals have been turned on.
_session_activated = False
_session_lock = threading.Lock()

#: Guards one-time atexit registration so it survives deactivate→reactivate
#: cycles (a second activation must not register a second cleanup).
_cleanup_registered = False


def _log(msg: str) -> None:
    """Best-effort diagnostic to stderr + the persistent log. Never raises."""
    try:
        print(f"[server] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass
    logsetup.info(f"[server] {msg}")


def _activate_session() -> None:
    """Turn on the computer-use visuals exactly once.

    Called on the first successful ``request_access``. Idempotent: subsequent
    calls are no-ops. Each effect is behind its config flag and fully guarded,
    so a headless/no-display host never crashes. Registers cleanup on first run.
    """
    global _session_activated
    with _session_lock:
        if _session_activated:
            return
        _session_activated = True

    # Eagerly resolve (and cache) the controlling window from this clean
    # activation context, BEFORE the overlay thread starts (the overlay reads it
    # to pick the glow/pill monitor — resolving here avoids a cross-thread race
    # on first resolution) and independent of SHRINK_TERMINAL / CLICKTHROUGH.
    # The keyboard self-harm guard (terminal.foreground_is_controlling) relies
    # on this resolution; deferring it to the first keyboard action means that
    # with the visuals off, the controlling window may already have yielded
    # focus to a target app, seeding the cache with the wrong window. A None
    # result means the guard may fail open, so surface it in the log.
    try:
        from . import terminal as _tm_resolve

        if not _tm_resolve.find_controlling_window():
            _log(
                "controlling-window resolution returned None at activation; "
                "keyboard self-harm guard may fail open"
            )
    except Exception as exc:
        _log(f"controlling-window resolution failed (continuing): {exc!r}")

    # Import lazily so a non-Windows / headless import of this module never pulls
    # in Win32-only machinery, and so an import error here can't break the tools.
    try:
        if config.GLOW:
            from . import overlay

            overlay.start(
                color=config.GLOW_COLOR,
                max_alpha=config.GLOW_MAX_ALPHA,
                band_frac=config.GLOW_BAND_FRAC,
                exclude_from_capture=config.GLOW_EXCLUDE_CAPTURE,
                show_pill=config.PILL,
            )
    except Exception as exc:
        _log(f"overlay.start failed (continuing): {exc!r}")

    try:
        if config.SHRINK_TERMINAL:
            from . import terminal

            terminal.shrink_to_corner()
    except Exception as exc:
        _log(f"terminal.shrink_to_corner failed (continuing): {exc!r}")

    # Ensure visuals are torn down even if __main__'s try/finally is bypassed.
    # Register once, even across deactivate → request_access reactivation cycles.
    global _cleanup_registered
    if not _cleanup_registered:
        atexit.register(_cleanup_session)
        _cleanup_registered = True


def _cleanup_session() -> None:
    """Tear down the computer-use visuals. Idempotent; never raises.

    Stops the overlay and restores the controlling terminal. Safe to call even
    if the session was never activated (both operations are no-ops then).
    """
    try:
        from . import overlay

        overlay.stop()
    except Exception as exc:
        _log(f"overlay.stop failed: {exc!r}")

    try:
        from . import terminal

        terminal.restore()
    except Exception as exc:
        _log(f"terminal.restore failed: {exc!r}")

    # Panic-release all modifier keys: if the process died (or is exiting)
    # between a synthetic chord's press and release, a modifier would stay
    # logically held forever and wedge the user's own shortcuts (every Win+
    # combo). Releasing an already-up key is a harmless no-op.
    try:
        from . import keyboard

        keyboard.release_stuck_modifiers()
    except Exception as exc:
        _log(f"release_stuck_modifiers failed: {exc!r}")


# --------------------------------------------------------------------------- #
# Tool logging
# --------------------------------------------------------------------------- #


def _summ(args: tuple, kwargs: dict) -> str:
    """Compact one-line repr of a tool call's arguments (truncated)."""
    parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    s = ", ".join(parts)
    return s if len(s) <= 300 else s[:300] + "…"


def _logged(fn):
    """Wrap a tool so every call + outcome (and any traceback) hits the log."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        logsetup.info(f"→ {fn.__name__}({_summ(args, kwargs)})")
        try:
            result = fn(*args, **kwargs)
            logsetup.info(f"✓ {fn.__name__}")
            return result
        except Exception:
            logsetup.exception(f"✗ {fn.__name__} FAILED")
            raise

    return wrapper


def _ltool(
    title: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool | None = None,
):
    """Register a tool with MCP annotations + automatic call/error logging.

    ``title`` is the human-readable display name. Exactly one of ``read_only`` /
    ``destructive`` must be set, so every tool carries the ``readOnlyHint`` /
    ``destructiveHint`` the Claude connectors directory uses for auto-permissions
    (read-only tools may run without a per-call prompt; destructive ones — those
    that inject input or otherwise change the machine/session — always prompt).
    """
    if read_only == destructive:
        raise ValueError(
            f"tool {title!r}: set exactly one of read_only / destructive"
        )
    annotations = ToolAnnotations(
        title=title,
        readOnlyHint=True if read_only else False,
        destructiveHint=True if destructive else None,
        idempotentHint=idempotent,
    )

    def deco(fn):
        return app.tool(title=title, annotations=annotations)(_logged(fn))

    return deco


# --------------------------------------------------------------------------- #
# 1-3  Meta / permission
# --------------------------------------------------------------------------- #


@_ltool("Request Application Access", destructive=True)
def request_access(
    apps: list[str],
    reason: str,
    clipboardRead: bool = False,
    clipboardWrite: bool = False,
    systemKeyCombos: bool = False,
) -> dict:
    """Request access to applications (and optional clipboard/system-key capabilities).

    Must be called before any input action. In CCC autogrant mode each resolvable
    app is granted at tier 'full'; unresolved names are returned under
    'notInstalled'.

    On the first call that successfully grants at least one app (the session
    becoming active), the CDC-style visuals are turned on exactly once.
    """
    result = ALLOWLIST.request_access(
        apps,
        reason,
        clipboardRead=clipboardRead,
        clipboardWrite=clipboardWrite,
        systemKeyCombos=systemKeyCombos,
    )
    # Activate visuals once the session is actually live (allowlist non-empty).
    if not ALLOWLIST.is_empty():
        _activate_session()
    return result


@_ltool("List Granted Applications", read_only=True)
def list_granted_applications() -> dict:
    """List the applications currently granted in this session, plus grant flags."""
    return ALLOWLIST.list_granted()


@_ltool("Request Teach Access", destructive=True)
def request_teach_access(apps: list[str], reason: str) -> dict:
    """Request teach-mode access (phase-2 stub).

    Auto-grants the requested apps (so subsequent actions work) and notes that
    the teach overlay UI is not implemented in the CLI build.
    """
    granted = ALLOWLIST.request_access(apps, reason)
    return {
        "granted": granted.get("granted", []),
        "note": "teach mode overlay not implemented in CLI build (phase 2)",
    }


@_ltool("End Session", destructive=True)
def deactivate() -> str:
    """End the computer-use session WITHOUT killing the MCP process.

    This is the "stop using my computer" action — the counterpart to
    ``request_access``. It hides the glow, restores the controlling terminal to
    its original size/state (re-maximizing it if it was maximized), and revokes
    all app grants so no further control is possible until a new
    ``request_access``. The MCP server stays alive and ready to reactivate.

    Mirrors how Claude Desktop auto-restores its window when a computer-use task
    ends — call this when finished controlling the screen so the user gets their
    normal desktop (and full-size terminal) back.
    """
    global _session_activated
    # Glow off + terminal restored to its original size/state (+ state file cleared).
    _cleanup_session()
    # Revoke control: an empty allowlist gates input off until a fresh
    # request_access (which will also re-activate the visuals).
    try:
        ALLOWLIST.clear()
    except Exception as exc:  # noqa: BLE001
        _log(f"deactivate: ALLOWLIST.clear failed: {exc!r}")
    with _session_lock:
        _session_activated = False
    return (
        "Computer-use session ended: glow off, terminal restored, all grants "
        "revoked. The MCP is still running — call request_access to use it again."
    )


# --------------------------------------------------------------------------- #
# 4-7  Capture
# --------------------------------------------------------------------------- #


@_ltool("Take Screenshot", read_only=True)
def screenshot(save_to_disk: bool = False) -> list:
    """Capture the active monitor and return a PNG image (windows of non-allowlisted apps masked).

    On multi-monitor systems a note identifies which monitor was captured
    whenever that changes; use display_overview to see every monitor at once.
    """
    return batch.do_screenshot(save_to_disk)


@_ltool("Preview All Monitors", read_only=True)
def display_overview(save_to_disk: bool = False) -> list:
    """Return ONE composite image of ALL monitors, laid out as arranged on the virtual desktop.

    Orientation aid for multi-monitor systems ("which screen is that window
    on?"): each monitor is labeled with its name and resolution. Coordinates in
    this image are NOT clickable — pick the monitor you need, call
    switch_display with its name, then screenshot to interact.
    """
    return batch.do_overview(save_to_disk)


@_ltool("Zoom Into Region", read_only=True)
def zoom(region: list[int], save_to_disk: bool = False) -> list:
    """Crop a region [x0, y0, x1, y1] of the screen (image space) and return it upscaled 2x. Read-only."""
    return batch.do_zoom(region, save_to_disk)


@_ltool("Switch Target Monitor", read_only=True)
def switch_display(display: str) -> str:
    """Switch which monitor subsequent screenshots capture (and clicks target).

    Pass a device name exactly as listed in screenshot notes / this tool's
    output (e.g. '\\\\.\\DISPLAY2'), or 'auto' to follow the foreground window's
    monitor automatically (falling back to the primary). Input coordinates
    always map into the monitor of the most recent screenshot.
    """
    screen.select_monitor(display)
    mons = screen.list_monitors()
    cur = screen.current_monitor()
    listing = ", ".join(
        f"{m.name} ({m.width}x{m.height}{'*' if m.primary else ''})" for m in mons
    )
    return f"Active display: {cur.name} ({cur.width}x{cur.height}). Monitors: {listing}"


@_ltool("Get Cursor Position", read_only=True)
def cursor_position() -> dict:
    """Return the current cursor position in image-space coordinates of the last screenshot."""
    px, py = inputs.get_cursor_pos()
    batch.ensure_scale()
    ix, iy = screen.physical_to_image(px, py)
    return {"x": ix, "y": iy}


# --------------------------------------------------------------------------- #
# 8-17  Mouse (coordinates in IMAGE space)
# --------------------------------------------------------------------------- #


@_ltool("Move Mouse", destructive=True)
def mouse_move(coordinate: list[int]) -> list:
    """Move the mouse to an image-space coordinate [x, y]."""
    return batch.run_action({"action": "mouse_move", "coordinate": coordinate})


@_ltool("Left Click", destructive=True)
def left_click(coordinate: list[int], text: str | None = None) -> list:
    """Left-click at an image-space coordinate; optional 'text' holds modifiers (e.g. 'ctrl+shift')."""
    return batch.run_action(
        {"action": "left_click", "coordinate": coordinate, "text": text}
    )


@_ltool("Right Click", destructive=True)
def right_click(coordinate: list[int], text: str | None = None) -> list:
    """Right-click at an image-space coordinate; optional 'text' holds modifiers."""
    return batch.run_action(
        {"action": "right_click", "coordinate": coordinate, "text": text}
    )


@_ltool("Middle Click", destructive=True)
def middle_click(coordinate: list[int], text: str | None = None) -> list:
    """Middle-click at an image-space coordinate; optional 'text' holds modifiers."""
    return batch.run_action(
        {"action": "middle_click", "coordinate": coordinate, "text": text}
    )


@_ltool("Double-Click", destructive=True)
def double_click(coordinate: list[int], text: str | None = None) -> list:
    """Double-click at an image-space coordinate; optional 'text' holds modifiers."""
    return batch.run_action(
        {"action": "double_click", "coordinate": coordinate, "text": text}
    )


@_ltool("Triple-Click", destructive=True)
def triple_click(coordinate: list[int], text: str | None = None) -> list:
    """Triple-click at an image-space coordinate; optional 'text' holds modifiers."""
    return batch.run_action(
        {"action": "triple_click", "coordinate": coordinate, "text": text}
    )


@_ltool("Click and Drag", destructive=True)
def left_click_drag(
    coordinate: list[int], start_coordinate: list[int] | None = None
) -> list:
    """Drag with the left button to 'coordinate'; start at 'start_coordinate' or the current cursor."""
    return batch.run_action(
        {
            "action": "left_click_drag",
            "coordinate": coordinate,
            "start_coordinate": start_coordinate,
        }
    )


@_ltool("Press Left Mouse Button", destructive=True)
def left_mouse_down() -> list:
    """Press and hold the left mouse button at the current cursor position."""
    return batch.run_action({"action": "left_mouse_down"})


@_ltool("Release Left Mouse Button", destructive=True)
def left_mouse_up() -> list:
    """Release the left mouse button at the current cursor position."""
    return batch.run_action({"action": "left_mouse_up"})


@_ltool("Scroll", destructive=True)
def scroll(
    coordinate: list[int], scroll_direction: str, scroll_amount: int
) -> list:
    """Scroll at an image-space coordinate. Direction: up/down/left/right; amount in wheel ticks."""
    return batch.run_action(
        {
            "action": "scroll",
            "coordinate": coordinate,
            "scroll_direction": scroll_direction,
            "scroll_amount": scroll_amount,
        }
    )


# --------------------------------------------------------------------------- #
# 18-20  Keyboard
# --------------------------------------------------------------------------- #


@_ltool("Press Key Chord", destructive=True)
def key(text: str, repeat: int = 1) -> list:
    """Press a key chord (e.g. 'Return', 'ctrl+a', 'alt+F4'), optionally repeated."""
    return batch.run_action({"action": "key", "text": text, "repeat": repeat})


@_ltool("Hold Key", destructive=True)
def hold_key(text: str, duration: float) -> list:
    """Hold a key chord down for 'duration' seconds, then release it."""
    return batch.run_action(
        {"action": "hold_key", "text": text, "duration": duration}
    )


@_ltool("Type Text", destructive=True)
def type(text: str) -> list:
    """Type Unicode text at the current focus (layout-independent, supports CJK/emoji)."""
    return batch.run_action({"action": "type", "text": text})


# --------------------------------------------------------------------------- #
# 21-24  Misc
# --------------------------------------------------------------------------- #


@_ltool("Wait", read_only=True)
def wait(duration: float) -> list:
    """Wait for 'duration' seconds."""
    return batch.run_action({"action": "wait", "duration": duration})


@_ltool("Open Application", destructive=True)
def open_application(app: str) -> str:
    """Resolve and launch an application, bringing it to the foreground.

    The app must be granted in the session allowlist; if it is not, guidance to
    call request_access is returned.
    """
    info = apps.resolve_app(app)
    if info is None:
        raise RuntimeError(
            f"could not find an application matching {app!r}; check the name "
            "or install it, then try again"
        )
    # Match by app identity (bundle id) not just exe basename: UWP apps
    # (Calculator, Settings, ...) are granted with an empty exe, so an
    # exe-only check would wrongly reject a freshly granted UWP app.
    if not ALLOWLIST.is_app_allowed(info):
        raise RuntimeError(
            f"{info.display!r} is not in the session allowlist; call "
            f"request_access(apps=[{info.display!r}], reason=...) first"
        )
    result = apps.launch_and_focus(info)
    if result.exited:
        raise RuntimeError(
            f'launched "{info.display}" but its process exited within ~2s '
            "without showing a window — it likely crashed on startup. The "
            '"Opened" result no longer hides this; verify the exe runs '
            "standalone before retrying."
        )
    if result.hwnd:
        # Multi-monitor: precisely warn when the window landed on a different
        # monitor than the one captures currently target (empty otherwise).
        return f'Opened "{info.display}".' + screen.cross_monitor_hint(result.hwnd)
    if result.pid:
        # Spawned and still alive, but no visible window yet: don't claim it
        # opened — let the caller wait and screenshot to confirm.
        return (
            f'Launched "{info.display}" (pid {result.pid}); the process is '
            "running but no window has appeared yet — wait a moment, then "
            "screenshot to confirm it came up." + screen.cross_monitor_hint(0)
        )
    if result.kind == "shell-bare":
        # No process, no window, and the name resolved to no on-disk target:
        # it almost certainly opened nothing. Don't fake an "Opened".
        raise RuntimeError(
            f'could not confirm "{info.display}" opened: no process or window '
            "appeared and the name did not resolve to an on-disk application. "
            "If this is a loose dev build, pass its full .exe path instead."
        )
    # Moniker/UWP or an existing-file shell launch we cannot positively verify
    # (UWP apps run under a shared host with no matchable exe): assume success.
    return f'Opened "{info.display}".' + screen.cross_monitor_hint(0)


@_ltool("Read Clipboard", read_only=True)
def read_clipboard() -> str:
    """Read text from the system clipboard (requires clipboardRead grant)."""
    if not ALLOWLIST.clipboard_read:
        raise RuntimeError(
            "clipboard read not granted; call request_access with "
            "clipboardRead=true first"
        )
    return clipboard.read_text()


@_ltool("Write Clipboard", destructive=True)
def write_clipboard(text: str) -> str:
    """Write text to the system clipboard (requires clipboardWrite grant)."""
    if not ALLOWLIST.clipboard_write:
        raise RuntimeError(
            "clipboard write not granted; call request_access with "
            "clipboardWrite=true first"
        )
    clipboard.write_text(text)
    return "Wrote clipboard."


# --------------------------------------------------------------------------- #
# 25-27  Batch / teach
# --------------------------------------------------------------------------- #


@_ltool("Run Action Batch", destructive=True)
def computer_batch(actions: list[dict]) -> list:
    """Run a list of actions sequentially against the pre-batch screenshot's coordinates.

    Stops on the first error; screenshot/zoom images are interleaved in the output.
    """
    return batch.run_batch(actions)


@_ltool("Teach Step", destructive=True)
def teach_step(
    explanation: str | None = None,
    next_preview: str | None = None,
    actions: list[dict] | None = None,
    anchor: Any = None,
) -> list:
    """Teach-mode step (phase-2 stub): executes the actions, then returns a screenshot."""
    return batch.teach_step(explanation, next_preview, actions, anchor)


@_ltool("Teach Batch", destructive=True)
def teach_batch(steps: list[dict]) -> list:
    """Teach-mode batch (phase-2 stub): executes each step's actions, then returns a final screenshot."""
    return batch.teach_batch(steps)


# --------------------------------------------------------------------------- #
# 28  Dev-only hot reload (COMPUTER_USE_DEV)
# --------------------------------------------------------------------------- #

#: Logic modules reloaded by the ``reload`` tool, in dependency order. NOTE:
#: ``permissions`` is deliberately excluded — it holds the live session
#: ALLOWLIST, and reloading it would create a fresh singleton, wiping all grants
#: (and the bound ``ALLOWLIST`` reference here would go stale).
_RELOAD_MODULES: tuple[str, ...] = (
    "config",
    "screen",
    "inputs",
    "keymap",
    "keyboard",
    "apps",
    "clipboard",
    "terminal",
    "overlay",
    "batch",
)


def _register_reload_tool() -> None:
    """Register the dev-only ``reload`` tool when ``config.DEV`` is on.

    Kept inside a function (rather than a module-level ``@_ltool()``) so the
    tool surface stays at 27 when DEV is off. Called once at import time.
    """

    @app.tool(
        name="reload",
        title="Reload Modules (dev)",
        annotations=ToolAnnotations(
            title="Reload Modules (dev)", readOnlyHint=False, destructiveHint=True
        ),
    )
    def reload() -> str:
        """Hot-reload the logic modules in-process (developer tool).

        Reloads ``config, screen, inputs, keymap, keyboard, apps, clipboard,
        terminal, overlay, batch`` (NOT ``permissions`` — that holds the live
        session allowlist). Server tools dispatch via module-attribute access
        (``batch.run_action(...)``, ``apps.resolve_app(...)``, ...) so reloaded
        code takes effect immediately. If the computer-use session is active, the
        visuals (glow + terminal shrink) are torn down before the reload and
        re-applied after, so tweaked geometry/alpha take effect without a restart.
        Returns the list of reloaded modules.
        """
        import importlib

        # If visuals are live, tear them down with the CURRENT code first — the
        # terminal must return to its true original position before its module
        # state is wiped by the reload — then re-apply with the reloaded code.
        reapply = _session_activated
        if reapply:
            try:
                from . import overlay as _ov

                _ov.stop()
            except Exception as exc:  # noqa: BLE001
                _log(f"reload: overlay.stop failed: {exc!r}")
            try:
                from . import terminal as _tm

                _tm.restore()
            except Exception as exc:  # noqa: BLE001
                _log(f"reload: terminal.restore failed: {exc!r}")

        reloaded: list[str] = []
        failed: list[str] = []
        for short in _RELOAD_MODULES:
            full = f"{__package__}.{short}"
            mod = sys.modules.get(full)
            if mod is None:
                try:
                    mod = importlib.import_module(full)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{short} ({exc})")
                    continue
            try:
                importlib.reload(mod)
                reloaded.append(short)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{short} ({exc})")

        # Re-apply visuals with the freshly reloaded code/config (no pill — it's a
        # one-time activation flourish, not wanted on every tweak).
        if reapply:
            # The reload just wiped terminal's controlling-window cache; resolve
            # it on THIS thread before the overlay thread reads it to pick the
            # glow monitor (mirrors _activate_session's eager resolve).
            try:
                from . import terminal as _tm

                _tm.find_controlling_window()
            except Exception as exc:  # noqa: BLE001
                _log(f"reload: controlling-window resolve failed: {exc!r}")
            try:
                from . import overlay as _ov

                if config.GLOW:
                    _ov.start(
                        color=config.GLOW_COLOR,
                        max_alpha=config.GLOW_MAX_ALPHA,
                        band_frac=config.GLOW_BAND_FRAC,
                        exclude_from_capture=config.GLOW_EXCLUDE_CAPTURE,
                        show_pill=False,
                    )
            except Exception as exc:  # noqa: BLE001
                _log(f"reload: overlay.start failed: {exc!r}")
            try:
                from . import terminal as _tm

                if config.SHRINK_TERMINAL:
                    _tm.shrink_to_corner()
            except Exception as exc:  # noqa: BLE001
                _log(f"reload: terminal.shrink_to_corner failed: {exc!r}")

        msg = "Reloaded: " + ", ".join(reloaded)
        if failed:
            msg += " | failed: " + ", ".join(failed)
        if reapply:
            msg += " | visuals re-applied"
        return msg


if config.DEV:
    _register_reload_tool()


# --------------------------------------------------------------------------- #
# Startup self-heal
# --------------------------------------------------------------------------- #

# If a prior session was force-killed (or crashed) without running its atexit
# cleanup, it may have left the controlling terminal shrunk / topmost /
# click-through / parked off-screen. Undo that now, before any new session
# starts. The state file is written by terminal.shrink_to_corner and cleared by
# terminal.restore on graceful shutdown, so its presence here means "the last
# run did not clean up". Fully guarded — a headless host just no-ops.
try:
    from . import terminal as _terminal_boot

    if _terminal_boot.self_heal_if_needed():
        _log("startup self-heal: restored an orphaned terminal from a prior session")
except Exception as _exc:  # noqa: BLE001
    _log(f"startup self-heal failed (continuing): {_exc!r}")
