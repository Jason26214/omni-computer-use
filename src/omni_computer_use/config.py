"""Environment-driven configuration for omni-computer-use.

All values are resolved once at import time from environment variables, with
defaults tuned for the Claude Code CLI (CCC) use case.

Environment variables:
    COMPUTER_USE_MAX_PIXELS         Max pixel budget for a downscaled screenshot.
                                    Default: 1_200_000.
    COMPUTER_USE_MASKING            'on' to mask non-allowlisted windows in
                                    screenshots; 'off' (default) to disable.
                                    Default off: for a user-controlled CLI tool,
                                    masking hides wanted content and the rect-based
                                    impl over-masks background full-screen windows.
    COMPUTER_USE_ENFORCE_FOREGROUND 'on' to block input when the frontmost app is
                                    not allowlisted; 'off' (default) to proceed.
    COMPUTER_USE_AUTOGRANT          'on' (default) to auto-grant resolvable apps on
                                    request_access; 'off' to disable.
    COMPUTER_USE_DEV                'on' to register the developer hot-reload
                                    'reload' tool (30 tools); 'off' (default) for
                                    the production 29-tool surface.

    Phase-2 CDC-style visuals (see overlay.py / terminal.py):
    COMPUTER_USE_GLOW               'on' (default) to show the orange edge glow.
    COMPUTER_USE_SHRINK_TERMINAL    'on' (default) to shrink the controlling
                                    terminal into the top-right corner.
    COMPUTER_USE_GLOW_COLOR         Glow RGB as 'R,G,B'. Default '217,119,87'.
    COMPUTER_USE_GLOW_ALPHA         Peak glow alpha (0.0-1.0). Default 0.6.
    COMPUTER_USE_GLOW_BAND          Glow band width as a fraction of the smaller
                                    screen dimension. Default 0.05.
    COMPUTER_USE_GLOW_EXCLUDE       'on' (default) to exclude the glow/pill from
                                    screen captures via SetWindowDisplayAffinity.
    COMPUTER_USE_PILL               'on' (default) to show the centered pill.
"""

import os


def _flag(name: str, default_on: bool) -> bool:
    """Parse an on/off env flag. ``default_on`` selects the absence behavior."""
    raw = os.environ.get(name)
    if raw is None:
        return default_on
    return raw.strip().lower() in ("on", "1", "true", "yes")


def _parse_color(raw: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """Parse an 'R,G,B' string into a clamped (r, g, b) tuple; fall back on error."""
    if not raw:
        return default
    try:
        parts = [int(p.strip()) for p in raw.split(",")]
        if len(parts) != 3:
            return default
        return tuple(max(0, min(255, v)) for v in parts)  # type: ignore[return-value]
    except Exception:
        return default


def _parse_float(name: str, default: float) -> float:
    """Parse a float env var; fall back to ``default`` on error."""
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


#: Maximum number of pixels (width * height) in a downscaled screenshot.
MAX_PIXELS: int = int(os.environ.get("COMPUTER_USE_MAX_PIXELS", 1_200_000))

#: Whether to mask windows of non-allowlisted apps before returning a screenshot.
MASKING: bool = os.environ.get("COMPUTER_USE_MASKING", "off").lower() in ("on", "1", "true")

#: Whether to block input actions when the frontmost app is not allowlisted.
ENFORCE_FOREGROUND: bool = os.environ.get("COMPUTER_USE_ENFORCE_FOREGROUND", "off") == "on"

#: Whether request_access auto-grants apps that resolve on this machine.
AUTOGRANT: bool = os.environ.get("COMPUTER_USE_AUTOGRANT", "on") != "off"

#: Developer mode. When on, a hot-reload ``reload`` tool is registered that
#: reloads the logic modules in-process (see server.py). Off by default for the
#: clean production 29-tool surface; turn on for local development.
DEV: bool = _flag("COMPUTER_USE_DEV", False)


# --------------------------------------------------------------------------- #
# Phase-2 CDC-style visuals
# --------------------------------------------------------------------------- #

#: Show the static orange edge glow while the session is active.
GLOW: bool = _flag("COMPUTER_USE_GLOW", True)

#: Shrink the controlling terminal into the top-right corner on activation.
SHRINK_TERMINAL: bool = _flag("COMPUTER_USE_SHRINK_TERMINAL", True)

#: Make the controlling terminal click-through (WS_EX_TRANSPARENT) for the
#: duration of each synthetic mouse action, so it never blocks a click aimed at
#: something underneath it (mirrors CDC). Restored immediately after each action,
#: so the user's real mouse is unaffected outside that brief window.
CLICKTHROUGH: bool = _flag("COMPUTER_USE_CLICKTHROUGH", True)

#: Hide the controlling window (the terminal in CCC / the Claude Desktop window
#: in CDC) from screenshots by parking it off-screen for the duration of each
#: capture. Default on. Turn off (COMPUTER_USE_HIDE_CONTROLLING=off) to keep it
#: visible in captures — useful for self-verifying the shrink / click-through
#: behavior before enabling the hide. Only takes effect when SHRINK_TERMINAL is
#: also on (the off-screen hide is wired into the same code path as the shrink).
HIDE_CONTROLLING: bool = _flag("COMPUTER_USE_HIDE_CONTROLLING", True)

#: Glow color (Anthropic brand orange by default).
GLOW_COLOR: tuple[int, int, int] = _parse_color(
    os.environ.get("COMPUTER_USE_GLOW_COLOR"), (217, 119, 87)
)

#: Peak glow alpha at the very edge (0.0-1.0). Lower = more transparent / subtle.
GLOW_MAX_ALPHA: float = _parse_float("COMPUTER_USE_GLOW_ALPHA", 0.4)

#: Glow band width as a fraction of min(screen_w, screen_h).
GLOW_BAND_FRAC: float = _parse_float("COMPUTER_USE_GLOW_BAND", 0.05)

#: Exclude the glow/pill windows from screen captures (WDA_EXCLUDEFROMCAPTURE).
GLOW_EXCLUDE_CAPTURE: bool = _flag("COMPUTER_USE_GLOW_EXCLUDE", True)

#: Show the centered "Claude is using your computer" pill on activation.
PILL: bool = _flag("COMPUTER_USE_PILL", True)
