"""Clipboard text read/write via ``win32clipboard``.

Thin wrappers around the Win32 clipboard API for reading and writing Unicode
text. Gating (whether the session is permitted to read/write the clipboard) is
enforced by the server layer, not here.

All stubs raise :class:`NotImplementedError`.
"""

from __future__ import annotations

import contextlib
import time

import win32clipboard  # type: ignore[import-not-found]

#: Number of times to retry opening the clipboard before giving up. The
#: clipboard is a single global resource that other processes briefly lock, so
#: ``OpenClipboard`` can transiently fail; a short retry loop makes access
#: robust.
_OPEN_RETRIES = 10

#: Seconds to wait between clipboard-open attempts.
_OPEN_DELAY = 0.02


def _open_clipboard() -> None:
    """Open the clipboard, retrying on transient failures.

    Raises:
        The last :class:`Exception` raised by ``OpenClipboard`` if every attempt
        fails.
    """
    last_exc: Exception | None = None
    for attempt in range(_OPEN_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as exc:  # pywintypes.error and friends
            last_exc = exc
            if attempt < _OPEN_RETRIES - 1:
                time.sleep(_OPEN_DELAY)
    if last_exc is not None:
        raise last_exc


def read_text() -> str:
    """Read Unicode text from the system clipboard.

    A short retry loop tolerates delayed-rendering clipboard owners (e.g. the
    modern Notepad), which advertise text but don't materialize it for a few
    milliseconds right after a programmatic copy.

    Returns:
        The clipboard's text content, or ``''`` if the clipboard holds no text.
    """
    data = ""
    for attempt in range(6):
        _open_clipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                raw = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                data = raw if isinstance(raw, str) else ""
            else:
                data = ""
        finally:
            with contextlib.suppress(Exception):
                win32clipboard.CloseClipboard()
        if data:
            return data
        if attempt < 5:
            time.sleep(0.03)
    return data


def write_text(s: str) -> None:
    """Write Unicode text to the system clipboard.

    Args:
        s: The text to place on the clipboard.
    """
    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(
            win32clipboard.CF_UNICODETEXT, "" if s is None else str(s)
        )
    finally:
        with contextlib.suppress(Exception):
            win32clipboard.CloseClipboard()
