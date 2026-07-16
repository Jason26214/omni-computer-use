"""Find and reshape the controlling terminal hosting this CCC session.

When the computer-use session activates, the visuals (see :mod:`overlay`) want to
replicate Claude Desktop's behavior of shrinking *itself* into the top-right
corner so the rest of the screen is clean for screenshots. For CCC the analogous
window is the terminal hosting this process — typically Windows Terminal
(``WindowsTerminal.exe``) running PowerShell 7 (``pwsh.exe``).

This module:

* Resolves the controlling terminal's top-level window by walking the process
  tree from this process upward, looking for the terminal host, then its visible
  top-level window. Falls back to the foreground window captured at the first
  call when ancestry can't be resolved.
* Shrinks that window to a tunable top-right box (and remembers its original
  rectangle so it can be restored).
* Exposes the hwnd(s) the screenshot path should hide so captures look clean.

All geometry is in physical (virtual-desktop) pixels, since the process runs
Per-Monitor-V2 DPI aware. Every Win32 call is guarded; failures degrade
gracefully and never raise to the caller.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

import win32con
import win32gui
import win32process

from . import config

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Top-right shrink box, as fractions of the primary work area, flush to the
#: top-right corner (margin 0). Calibrated from the user's reference screenshot:
#: ~33% of screen width, ~67% of the work-area height. While shrunk the terminal
#: is also made TOPMOST so the user always sees it; it is moved off-screen only
#: for the duration of each capture (see :func:`hide_for_capture`) so Claude Code
#: never sees it. Module constants so the box is easy to tune.
SHRINK_WIDTH_FRAC: float = 0.334
SHRINK_HEIGHT_FRAC: float = 0.666
SHRINK_MARGIN_PX: int = 0

#: Terminal-host executables we prefer (in order) when walking the ancestry.
#: WindowsTerminal.exe is the real host window; the others are fallbacks that
#: own a console window when not running inside Windows Terminal.
_TERMINAL_EXES: tuple[str, ...] = (
    "windowsterminal.exe",
    "pwsh.exe",
    "powershell.exe",
    "openconsole.exe",
    "conhost.exe",
    "cmd.exe",
)

#: Host executables that mean "running under Claude Desktop" (CDC). The window we
#: manage there is the Electron main window (class ``Chrome_WidgetWin_1``, title
#: "Claude") owned by ``claude.exe`` — the CDC analogue of the terminal.
_CLAUDE_DESKTOP_EXES: tuple[str, ...] = ("claude.exe",)

# ---------------------------------------------------------------------------
# Module state (singleton)
# ---------------------------------------------------------------------------

#: Cached resolved controlling hwnd — the terminal (CCC) or the Claude Desktop
#: window (CDC). Resolved lazily on first call.
_terminal_hwnd: int | None = None

#: Kind of the resolved controlling window: ``'terminal'`` (CCC),
#: ``'claude-desktop'`` (CDC), ``'fallback'`` (unknown host that owns a window),
#: or ``''`` (unresolved). Drives the self-heal hwnd-reuse guard.
_controlling_kind: str = ""

#: True once we have attempted resolution (so a cached ``None`` isn't retried
#: forever in a way that would pick a *different* foreground window later).
_resolved: bool = False

#: Stashed original rectangle ``(left, top, right, bottom)`` before shrinking.
_original_rect: tuple[int, int, int, int] | None = None

#: True while the window is currently shrunk (so ``excluded_hwnds`` knows).
_shrunk: bool = False

#: The window's EX_TOPMOST state before we shrank it, so :func:`restore` can put
#: it back exactly. While shrunk we force the terminal topmost (always visible to
#: the user, but hidden from captures via the off-screen round-trip).
_was_topmost: bool = False

#: The window's full GWL_EXSTYLE before we shrank it, so restore / self-heal can
#: put it back exactly — dropping the TOPMOST we add and any leftover
#: WS_EX_TRANSPARENT from a click-through.
_original_exstyle: int | None = None

#: Whether the terminal was MAXIMIZED before we shrank it. A maximized window
#: keeps its zoomed flag after SetWindowPos shrinks its rect, so Windows snaps it
#: back to fullscreen on the next user click — we therefore SW_RESTORE it before
#: shrinking, and SW_MAXIMIZE it again on restore so the user's fullscreen
#: terminal comes back.
_was_maximized: bool = False

#: Click-through state. While a synthetic mouse action runs we drop the terminal
#: to the BOTTOM of the z-order so the click lands on the target underneath it,
#: then restore it to topmost immediately after. WS_EX_TRANSPARENT (CDC's trick)
#: does NOT pass clicks through a non-layered window like Windows Terminal
#: (verified live), and WS_EX_LAYERED can't be bolted onto its DirectComposition
#: surface — moving it in z is reliable and leaves rendering untouched.
_clickthrough_active: bool = False
_clickthrough_was_topmost: bool = False

#: Which click-through method was applied for the in-flight action, so
#: end_clickthrough undoes the right one: '' / 'zorder' (terminal, non-layered)
#: / 'transparent' (layered Claude Desktop window — CDC's own approach).
_clickthrough_method: str = ""

#: Exact ex-style captured just before WS_EX_TRANSPARENT was applied, so
#: end_clickthrough can put it back atomically (no read-modify-write race).
#: None outside a transparent pulse.
_clickthrough_saved_exstyle: int | None = None

#: Seconds to keep the terminal out of the way after the synthetic action before
#: restoring it, so the input's hit-test resolves first and the restore doesn't
#: race ahead of the click (CDC holds its pass-through ~50-120ms).
_CLICKTHROUGH_SETTLE_S: float = 0.05

#: True if SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) succeeded on the
#: terminal. When True the OS keeps the window out of captures, so the screenshot
#: path need NOT mask it (``excluded_hwnds`` returns empty). When False we fall
#: back to masking the terminal's rect in the screenshot path.
_wda_excluded: bool = False

#: Stashed rectangle ``(left, top, right, bottom)`` captured just before the
#: window was moved off-screen for a capture (see :func:`hide_for_capture`).
#: ``None`` when not currently hidden.
_hidden_rect: tuple[int, int, int, int] | None = None

#: True while the controlling window is currently parked off-screen for a
#: capture. Lets :func:`restore_after_capture` be idempotent.
_hidden: bool = False

#: Off-screen parking coordinates (far top-left of the virtual desktop).
_OFFSCREEN_X: int = -32000
_OFFSCREEN_Y: int = -32000

# SetWindowDisplayAffinity affinity values.
_WDA_NONE = 0x00000000
_WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _try_exclude_from_capture(hwnd: int, exclude: bool) -> bool:
    """Attempt SetWindowDisplayAffinity on a (cross-process) window.

    Returns ``True`` only if the call reports success. This frequently fails for
    a window owned by another process (Windows Terminal), in which case the
    caller falls back to rect masking in the screenshot path.
    """
    try:
        user32 = ctypes.windll.user32
        affinity = _WDA_EXCLUDEFROMCAPTURE if exclude else _WDA_NONE
        ok = user32.SetWindowDisplayAffinity(
            wintypes.HWND(hwnd), wintypes.DWORD(affinity)
        )
        return bool(ok)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Low-level process-tree helpers (psutil-free; ctypes Toolhelp32)
# ---------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _snapshot_processes() -> dict[int, tuple[int, str]]:
    """Return ``{pid: (parent_pid, exe_basename_lower)}`` for all processes.

    Uses ``CreateToolhelp32Snapshot`` so no third-party dependency is required.
    Returns an empty mapping on any failure.
    """
    procs: dict[int, tuple[int, str]] = {}
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        return procs
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return procs
        while True:
            pid = int(entry.th32ProcessID)
            ppid = int(entry.th32ParentProcessID)
            name = str(entry.szExeFile or "").lower()
            procs[pid] = (ppid, name)
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    except Exception:
        pass
    finally:
        try:
            kernel32.CloseHandle(snap)
        except Exception:
            pass
    return procs


def _ancestry_pids() -> list[tuple[int, str]]:
    """Return ``[(pid, exe_basename_lower), ...]`` from this process upward.

    The list starts with the current process and follows parent links until the
    root (or a cycle / missing parent). Best-effort: returns at least the current
    pid even if the snapshot fails.
    """
    chain: list[tuple[int, str]] = []
    procs = _snapshot_processes()
    pid = os.getpid()
    seen: set[int] = set()
    # Make sure the current pid is represented even if the snapshot missed it.
    if pid not in procs:
        chain.append((pid, ""))
    while pid and pid not in seen:
        seen.add(pid)
        entry = procs.get(pid)
        if entry is None:
            break
        ppid, name = entry
        chain.append((pid, name))
        if ppid == pid:  # defensive: self-parent
            break
        pid = ppid
    return chain


def _visible_top_window_for_pid(pid: int) -> int:
    """Return the best visible, non-cloaked top-level window owned by ``pid``.

    Prefers a window that has a title and a non-degenerate rectangle. Returns 0
    if none is found.
    """
    candidates: list[tuple[int, int]] = []  # (area, hwnd)

    def _cb(hwnd: int, _extra: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _tid, wpid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if wpid != pid:
            return True
        # Top-level only (no owner / parent chrome): GetParent returns 0 for
        # top-level windows.
        try:
            if win32gui.GetParent(hwnd):
                return True
        except Exception:
            pass
        if _is_cloaked(hwnd):
            return True
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        w, h = r - l, b - t
        if w <= 1 or h <= 1:
            return True
        try:
            has_title = bool(win32gui.GetWindowText(hwnd))
        except Exception:
            has_title = False
        # Score titled windows above untitled ones, then by area.
        score = w * h + (1 << 30 if has_title else 0)
        candidates.append((score, hwnd))
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    if not candidates:
        return 0
    candidates.sort(reverse=True)
    return candidates[0][1]


def _is_cloaked(hwnd: int) -> bool:
    """Return True if the window is DWM-cloaked (hidden virtual-desktop window)."""
    try:
        DWMWA_CLOAKED = 14
        value = wintypes.DWORD(0)
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_CLOAKED,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if res == 0 and value.value != 0:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Work-area / geometry helpers
# ---------------------------------------------------------------------------


def _primary_work_area() -> tuple[int, int, int, int]:
    """Return the primary monitor work area ``(left, top, right, bottom)``.

    The work area excludes the taskbar. Falls back to the full primary screen
    metrics if ``SystemParametersInfo`` fails.
    """
    try:
        SPI_GETWORKAREA = 0x0030
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        ):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    # Fallback: full primary screen (SM_CXSCREEN=0, SM_CYSCREEN=1).
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics
        return 0, 0, int(gsm(0)), int(gsm(1))
    except Exception:
        return 0, 0, 2560, 1600


def _work_area_for_hwnd(hwnd: int) -> tuple[int, int, int, int]:
    """Work area ``(l, t, r, b)`` of the monitor hosting ``hwnd``.

    Multi-monitor correctness: the controlling window must shrink into the
    corner of ITS OWN monitor (the Claude/terminal window may live on a
    secondary display), not the primary's. Falls back to
    :func:`_primary_work_area` on any failure.
    """
    try:
        MONITOR_DEFAULTTONEAREST = 2
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        if hmon:

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        pass
    return _primary_work_area()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_controlling_window() -> int | None:
    """Return the HWND of the terminal hosting this CCC session, or ``None``.

    Strategy:

    1. Walk the process tree from this process upward looking for a known
       terminal host (``WindowsTerminal.exe`` preferred, then ``pwsh.exe`` /
       ``OpenConsole.exe`` / ``conhost.exe`` / ``cmd.exe``).
    2. For the matched ancestor pid, return its best visible top-level window.
       Windows Terminal is a single process hosting many windows; preferring the
       window owned by the pid in our ancestry selects the right one.
    3. Fallback: if ancestry can't be resolved to a window, use the foreground
       window captured at the first call (``GetForegroundWindow``).

    The resolved hwnd is cached; subsequent calls return the cached value (even
    if it is ``None``) so the foreground fallback is sampled only once, at
    startup.

    Returns:
        The terminal window handle, or ``None`` if nothing could be resolved.
    """
    global _terminal_hwnd, _resolved, _controlling_kind

    if _resolved:
        return _terminal_hwnd

    hwnd, kind = _resolve_controlling_hwnd()
    _terminal_hwnd = hwnd if hwnd else None
    _controlling_kind = kind
    _resolved = True
    return _terminal_hwnd


def _owner_pid(hwnd: int) -> int:
    """Return the pid that owns ``hwnd``, or 0."""
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except Exception:
        return 0


def _is_desktop_window(hwnd: int) -> bool:
    """True if ``hwnd`` is a shell surface (desktop / taskbar) — never a host."""
    try:
        return (win32gui.GetClassName(hwnd) or "") in (
            "Progman",
            "WorkerW",
            "Shell_TrayWnd",
            "Shell_SecondaryTrayWnd",
        )
    except Exception:
        return False


def _is_claude_desktop_window(hwnd: int) -> bool:
    """True if ``hwnd`` is THE Claude Desktop main window (not just any Electron).

    Class ``Chrome_WidgetWin_1`` is shared by every Chromium/Electron app AND by
    Claude's own secondary windows (a second, untitled ``Chrome_WidgetWin_1``
    exists in the same process), so the class alone is not specific. We also pin
    a title starting "Claude", not-minimized, and a ``claude.exe`` owner. Used as
    the strong self-heal identity guard.
    """
    try:
        if (win32gui.GetClassName(hwnd) or "") != "Chrome_WidgetWin_1":
            return False
        if win32gui.IsIconic(hwnd):
            return False
        if not (win32gui.GetWindowText(hwnd) or "").startswith("Claude"):
            return False
    except Exception:
        return False
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return _snapshot_processes().get(pid, (0, ""))[1] in _CLAUDE_DESKTOP_EXES
    except Exception:
        return False


def _find_claude_desktop_window(ancestry: set[int]) -> int:
    """Find the Claude Desktop main window system-wide, or 0.

    Claude (Electron) owns several ``Chrome_WidgetWin_1`` windows in one process,
    and the claude.exe that spawned us may itself own no window, so we scan ALL
    top-level windows and score the candidates: owned by one of our ancestor pids
    wins decisively (it is *our* Claude Desktop, not a coincidental other one),
    then a "Claude" title, then larger area. Candidates must be visible, not
    minimized, not cloaked, top-level, ``Chrome_WidgetWin_1``, owned by
    ``claude.exe``, and non-degenerate.
    """
    procs = _snapshot_processes()
    best_score = -1
    best_hwnd = 0

    def _cb(hwnd: int, _extra: object) -> bool:
        nonlocal best_score, best_hwnd
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            if win32gui.GetParent(hwnd):
                return True
            if (win32gui.GetClassName(hwnd) or "") != "Chrome_WidgetWin_1":
                return True
        except Exception:
            return True
        if _is_cloaked(hwnd):
            return True
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if procs.get(pid, (0, ""))[1] not in _CLAUDE_DESKTOP_EXES:
            return True
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        w, h = r - l, b - t
        if w <= 1 or h <= 1:
            return True
        try:
            titled = (win32gui.GetWindowText(hwnd) or "").startswith("Claude")
        except Exception:
            titled = False
        if pid not in ancestry:
            # Only OUR Claude Desktop: the main window's owner must be in our
            # process ancestry. A separate Claude Desktop instance (e.g. one the
            # user happens to have open under CCC) is never grabbed. Enforcing
            # this INSIDE the finder (not just at the caller) keeps the guarantee
            # even if some future caller forgets the owner-in-ancestry re-check.
            return True
        score = w * h
        if titled:
            score += 1 << 40
        if score > best_score:
            best_score = score
            best_hwnd = hwnd
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return best_hwnd


def _resolve_controlling_hwnd() -> tuple[int, str]:
    """Resolve the controlling window (uncached). Returns ``(hwnd, kind)``.

    ``kind`` is ``'terminal'`` (CCC), ``'claude-desktop'`` (CDC), ``'fallback'``
    (some other ancestor owning a window), or ``''`` when nothing was found
    (``hwnd`` 0).
    """
    chain = _ancestry_pids()

    # 1) Terminal host (CCC): WindowsTerminal preferred, then pwsh/conhost/cmd.
    #    Highest-priority exe found anywhere in the ancestry (closest ancestor
    #    wins within a tier).
    for want in _TERMINAL_EXES:
        for pid, name in chain:
            if name == want:
                hwnd = _visible_top_window_for_pid(pid)
                if hwnd:
                    return hwnd, "terminal"

    # 2) Claude Desktop host (CDC): a claude.exe anywhere in the ancestry means
    #    we are running under Claude Desktop. Resolve the Claude main window
    #    system-wide (the claude.exe that spawned us may own no window), and only
    #    accept it if its owning process is in OUR ancestry — so a *separate*
    #    Claude Desktop that merely happens to be open (e.g. under CCC) is never
    #    grabbed.
    anc = {pid for pid, _n in chain}
    if any(name in _CLAUDE_DESKTOP_EXES for _pid, name in chain):
        hwnd = _find_claude_desktop_window(anc)
        if hwnd and _owner_pid(hwnd) in anc:
            return hwnd, "claude-desktop"

    # 3) Any ancestor that owns a visible top-level window (an unusual host) —
    #    but NEVER the shell desktop (explorer's Progman/WorkerW), which is a
    #    full-screen visible top-level window that would otherwise be grabbed and
    #    shrunk/parked.
    for pid, name in chain:
        if pid == os.getpid() or name == "explorer.exe":
            continue
        hwnd = _visible_top_window_for_pid(pid)
        if hwnd and not _is_desktop_window(hwnd):
            return hwnd, "fallback"

    # 4) Foreground-at-startup fallback.
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        fg = 0
    if fg:
        return fg, "fallback"
    return 0, ""


def foreground_is_controlling() -> bool:
    """Return True if the current foreground window is the controlling host.

    Keyboard-input safety guard: synthetic ``type`` / ``key`` go to whatever
    holds focus, so if the controlling window (the Claude Desktop window in CDC,
    or the hosting terminal in CCC) is frontmost, keystrokes would land in our
    own control surface — polluting the conversation, or with a trailing Return
    sending a message / running a shell command. Callers block keyboard actions
    when this returns True.

    Matches on any of: the foreground hwnd IS the resolved controlling window;
    it belongs to the same owning process (covers Claude Desktop's multiple
    ``Chrome_WidgetWin_1`` windows and a terminal's multiple tab windows); or,
    under CDC, it is any Claude Desktop main window. Best-effort — returns False
    on any failure so a detection glitch never blocks legitimate input.
    """
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        return False
    if not fg:
        return False
    # Resolve (and cache) the controlling window; also sets _controlling_kind.
    ctrl = find_controlling_window()
    # The exact controlling window (any host) is always blocked.
    if ctrl and fg == ctrl:
        return True
    # Same-process widening ONLY for CDC: Claude Desktop hosts several top-level
    # windows in one claude.exe process, all part of the single control surface,
    # so match by owner-PID (plus the Claude-main-window identity as a belt-and-
    # suspenders). For a terminal host we deliberately do NOT widen by owner-PID:
    # a second Windows Terminal window (same WindowsTerminal.exe process) that the
    # user legitimately wants automated is not our control surface, and blocking
    # it would violate omni's permissive intent. A terminal's own tabs share one
    # hwnd, so the exact-hwnd check above already covers the controlling terminal.
    if _controlling_kind == "claude-desktop":
        if ctrl:
            fo, co = _owner_pid(fg), _owner_pid(ctrl)
            if fo and co and fo == co:
                return True
        if _is_claude_desktop_window(fg):
            return True
    return False


def _is_maximized(hwnd: int) -> bool:
    """Return True if the window is maximized.

    ``win32gui`` has NO ``IsZoomed`` (verified), so we read the show-state from
    ``GetWindowPlacement`` — ``showCmd == SW_SHOWMAXIMIZED``. Best-effort;
    returns False on any failure.
    """
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        return placement[1] == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return False


def shrink_to_corner() -> None:
    """Shrink the controlling terminal into the top-right corner.

    Records the window's current rectangle (physical pixels) so :func:`restore`
    can put it back, then moves/resizes it to a top-right box sized by
    :data:`SHRINK_WIDTH_FRAC` / :data:`SHRINK_HEIGHT_FRAC` within the primary
    work area, leaving a :data:`SHRINK_MARGIN_PX` margin. Uses
    ``SWP_NOACTIVATE | SWP_NOZORDER``.

    No-op (and never raises) if no controlling window can be resolved.
    """
    global _original_rect, _shrunk

    hwnd = find_controlling_window()
    if not hwnd:
        return
    try:
        if not win32gui.IsWindow(hwnd):
            return
    except Exception:
        return

    # Stash the original rect + topmost + maximized state once (idempotent).
    global _was_topmost, _original_exstyle, _was_maximized
    if _original_rect is None:
        # Record whether the terminal was maximized, then clear the maximized /
        # minimized state BEFORE measuring its rect. A maximized window keeps its
        # zoomed flag even after SetWindowPos shrinks it, so Windows snaps it back
        # to fullscreen the moment the user clicks it (the "click → fullscreen"
        # bug). SW_RESTORE drops that flag; we then measure the NORMAL rect and
        # re-maximize in restore() if it was maximized.
        _was_maximized = _is_maximized(hwnd)
        try:
            if win32gui.IsIconic(hwnd) or _was_maximized:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        try:
            _original_rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            _original_rect = None
        try:
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            _original_exstyle = ex
            _was_topmost = bool(ex & win32con.WS_EX_TOPMOST)
        except Exception:
            _original_exstyle = None
            _was_topmost = False
        # Persist the pre-shrink state so a force-killed session can be
        # self-healed on the next startup (see self_heal_if_needed).
        _write_state(
            hwnd,
            _original_rect,
            _original_exstyle,
            _was_topmost,
            _was_maximized,
            _controlling_kind,
        )

    wa_l, wa_t, wa_r, wa_b = _work_area_for_hwnd(hwnd)
    wa_w = max(1, wa_r - wa_l)
    wa_h = max(1, wa_b - wa_t)

    box_w = max(200, int(wa_w * SHRINK_WIDTH_FRAC))
    box_h = max(200, int(wa_h * SHRINK_HEIGHT_FRAC))
    x = wa_r - box_w - SHRINK_MARGIN_PX
    y = wa_t + SHRINK_MARGIN_PX

    # Pin TOPMOST (HWND_TOPMOST) so the user always sees the terminal; drop
    # SWP_NOZORDER so the z-order actually changes.
    flags = win32con.SWP_NOACTIVATE
    try:
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOPMOST, x, y, box_w, box_h, flags
        )
        _shrunk = True
    except Exception:
        # Leave _shrunk as-is; restore remains safe.
        pass

    # A window may refuse to shrink below its own minimum size (Electron apps
    # like Claude Desktop have a min-width), so it can end up wider/taller than
    # box_w/box_h and overflow off-screen to the right/bottom. Re-measure the
    # ACTUAL rect and nudge it back so it stays fully inside the work area — this
    # keeps the placement on-screen regardless of the window's min-size or how
    # the SHRINK_*_FRAC values are tuned.
    try:
        al, at, ar, ab = win32gui.GetWindowRect(hwnd)
        dx = min(0, wa_r - SHRINK_MARGIN_PX - ar)  # < 0 if overflowing right
        dy = min(0, wa_b - SHRINK_MARGIN_PX - ab)  # < 0 if overflowing bottom
        nx = max(wa_l + SHRINK_MARGIN_PX, al + dx)  # but never off the left edge
        ny = max(wa_t + SHRINK_MARGIN_PX, at + dy)  # or the top edge
        if (nx, ny) != (al, at):
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                nx,
                ny,
                0,
                0,
                win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
    except Exception:
        pass

    # Optionally also exclude the controlling window from captures at the OS
    # level. This usually fails for a cross-process window; when it does, the
    # off-screen hide (hide_for_capture) does the job instead. Gated on
    # HIDE_CONTROLLING so the window can be kept visible in captures for
    # debugging / self-verification.
    global _wda_excluded
    _wda_excluded = (
        _try_exclude_from_capture(hwnd, True) if config.HIDE_CONTROLLING else False
    )


def _strip_clickthrough_exstyle(hwnd: int) -> None:
    """Remove a leaked WS_EX_TRANSPARENT from a window (no-op if not set).

    The controlling window must never be left input-transparent; this is the
    cheap, always-safe way to undo a click-through leak. Never raises.
    """
    try:
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TRANSPARENT:
            win32gui.SetWindowLong(
                hwnd, win32con.GWL_EXSTYLE, ex & ~win32con.WS_EX_TRANSPARENT
            )
    except Exception:
        pass


def _reset_shrink_state() -> None:
    """Drop the per-shrink in-memory state and the crash-safety file.

    Run on EVERY :func:`restore` exit so a session that is no longer holding a
    shrunk window leaves nothing stale (memory or disk) behind.
    """
    global _shrunk, _wda_excluded, _original_rect, _original_exstyle
    global _was_topmost, _was_maximized
    _shrunk = False
    _wda_excluded = False
    _original_rect = None
    _original_exstyle = None
    _was_topmost = False
    _was_maximized = False
    _clear_state()


def restore() -> None:
    """Restore the controlling window to its stashed original rectangle.

    Idempotent and safe to call even if :func:`shrink_to_corner` was never
    called. Always strips a leaked WS_EX_TRANSPARENT from the controlling window
    first — even on the no-shrink early-return path — so a click-through-only
    session can never leave the window permanently input-transparent. Never
    raises.
    """
    global _wda_excluded

    hwnd = _terminal_hwnd
    rect = _original_rect

    # Always neutralize a leaked click-through transparency first, regardless of
    # whether shrink ran (rect may be None for a click-through-only session).
    if hwnd:
        try:
            if win32gui.IsWindow(hwnd):
                _strip_clickthrough_exstyle(hwnd)
        except Exception:
            pass

    if not hwnd or rect is None:
        _reset_shrink_state()
        return
    try:
        if not win32gui.IsWindow(hwnd):
            _reset_shrink_state()
            return
    except Exception:
        _reset_shrink_state()
        return

    # Undo the capture exclusion so the window is visible to captures again.
    if _wda_excluded:
        _try_exclude_from_capture(hwnd, False)
        _wda_excluded = False

    l, t, r, b = rect
    w = max(1, r - l)
    h = max(1, b - t)
    # Reassert the full original ex-style first (drops any leftover
    # WS_EX_TRANSPARENT plus the TOPMOST we added), then restore position/z-order.
    if _original_exstyle is not None:
        try:
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _original_exstyle)
        except Exception:
            pass
    insert_after = (
        win32con.HWND_TOPMOST if _was_topmost else win32con.HWND_NOTOPMOST
    )
    try:
        win32gui.SetWindowPos(hwnd, insert_after, l, t, w, h, win32con.SWP_NOACTIVATE)
    except Exception:
        pass
    # Re-maximize if the window was maximized before we shrank it.
    if _was_maximized:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        except Exception:
            pass
    # Graceful restore complete — drop the per-shrink state + crash-safety file so
    # a later re-activation re-measures the window (no stale rect reuse).
    _reset_shrink_state()


def begin_clickthrough() -> bool:
    """Drop the terminal to the BOTTOM of the z-order for one synthetic action.

    A synthetic mouse action would otherwise land on the topmost terminal parked
    in the corner. Moving the terminal to the bottom of the z-order lets the
    click reach whatever target sits underneath it; position and size are
    unchanged (``SWP_NOMOVE | SWP_NOSIZE``), so the window does not appear to
    move — only its z-order blips for the brief moment the action takes, after
    which :func:`end_clickthrough` restores it to topmost.

    Why z-order, not ``WS_EX_TRANSPARENT``: transparent alone does NOT pass
    clicks through a non-layered window like Windows Terminal (verified live),
    and ``WS_EX_LAYERED`` must not be bolted onto its DirectComposition surface.
    Moving it in z is reliable and leaves rendering untouched. Guarded; never
    raises.

    Returns:
        ``True`` if the terminal was moved to the bottom, else ``False``.
    """
    global _clickthrough_active, _clickthrough_was_topmost, _clickthrough_method
    global _clickthrough_saved_exstyle
    if _clickthrough_active:
        return True
    try:
        hwnd = find_controlling_window()
    except Exception:
        return False
    if not hwnd:
        return False
    try:
        if not win32gui.IsWindow(hwnd):
            return False
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        _clickthrough_was_topmost = bool(ex & win32con.WS_EX_TOPMOST)
        applied = ""
        if _controlling_kind == "claude-desktop" and (ex & win32con.WS_EX_LAYERED):
            # Layered window (the Claude Desktop main window): make it
            # input-transparent for the action with WS_EX_TRANSPARENT — clicks
            # pass through to whatever is underneath, even the bare desktop. This
            # is CDC's own approach and only works on a LAYERED window; the window
            # keeps its place and topmost status (no z-order blip).
            _clickthrough_saved_exstyle = ex
            win32gui.SetWindowLong(
                hwnd, win32con.GWL_EXSTYLE, ex | win32con.WS_EX_TRANSPARENT
            )
            # SetWindowLong fails silently (returns 0). Confirm the bit actually
            # took; if not, fall back to the z-order drop so the click still
            # reaches the target instead of landing on the opaque Claude window.
            if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TRANSPARENT:
                applied = "transparent"
                # Crash-safety record so a force-kill DURING the transparent pulse
                # is self-healable even when shrink_to_corner never ran (CLICKTHROUGH
                # and SHRINK are independent flags). When shrink DID run it already
                # wrote a richer record (with rect) — don't clobber it.
                if _original_rect is None:
                    _write_state(hwnd, None, ex, _clickthrough_was_topmost, False, _controlling_kind)
            else:
                _clickthrough_saved_exstyle = None
        if not applied:
            # Non-layered host (Windows Terminal), or transparent didn't take:
            # drop it to the bottom of the z-order (it loses topmost; restored by
            # end_clickthrough). Position/size unchanged so it does not move.
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_BOTTOM,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
            applied = "zorder"
        _clickthrough_method = applied
        _clickthrough_active = True
        return True
    except Exception:
        _clickthrough_active = False
        return False


def end_clickthrough() -> None:
    """Restore the terminal to topmost, undoing :func:`begin_clickthrough`.

    Idempotent and ``finally``-safe: a no-op if :func:`begin_clickthrough` did
    not move anything. Never raises.
    """
    global _clickthrough_active, _clickthrough_was_topmost, _clickthrough_method
    global _clickthrough_saved_exstyle
    if not _clickthrough_active:
        return
    hwnd = _terminal_hwnd
    was_topmost = _clickthrough_was_topmost
    method = _clickthrough_method
    saved_ex = _clickthrough_saved_exstyle
    _clickthrough_active = False
    _clickthrough_method = ""
    _clickthrough_saved_exstyle = None
    if not hwnd:
        return
    try:
        if not win32gui.IsWindow(hwnd):
            return
        if method == "transparent":
            # Put the exact pre-transparent ex-style back in one write (avoids the
            # read-modify-write race), then verify WS_EX_TRANSPARENT is actually
            # gone and clear it explicitly if some other change re-set it.
            if saved_ex is not None:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, saved_ex)
            now = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if now & win32con.WS_EX_TRANSPARENT:
                win32gui.SetWindowLong(
                    hwnd, win32con.GWL_EXSTYLE, now & ~win32con.WS_EX_TRANSPARENT
                )
            # Drop the temporary crash-safety record begin wrote for the no-shrink
            # case (shrink's own record, if any, persists for restore()).
            if _original_rect is None:
                _clear_state()
        else:
            insert_after = (
                win32con.HWND_TOPMOST if was_topmost else win32con.HWND_NOTOPMOST
            )
            win32gui.SetWindowPos(
                hwnd,
                insert_after,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
    except Exception:
        pass


@contextlib.contextmanager
def clickthrough():
    """Context manager: terminal yields the click for the enclosed action.

    Drops the terminal to the z-order bottom, runs the action, holds it there a
    short settle window so the input's hit-test resolves, then restores topmost.
    Yields the bool result of :func:`begin_clickthrough`; always restores on exit.
    """
    ok = begin_clickthrough()
    try:
        yield ok
    finally:
        if ok:
            try:
                time.sleep(_CLICKTHROUGH_SETTLE_S)
            except Exception:
                pass
        end_clickthrough()


# ---------------------------------------------------------------------------
# Crash-safety state file (self-heal an orphaned terminal on next startup)
# ---------------------------------------------------------------------------


def _state_path() -> str:
    """Return the path of the crash-safety state file (next to the logs)."""
    try:
        from . import logsetup

        d = logsetup._default_log_dir()
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        d = os.path.join(os.path.dirname(os.path.dirname(here)), "logs")
    return os.path.join(d, "terminal_state.json")


def _write_state(
    hwnd: int,
    rect: tuple[int, int, int, int] | None,
    exstyle: int | None,
    was_topmost: bool,
    was_maximized: bool = False,
    kind: str = "",
) -> None:
    """Persist the pre-shrink controlling-window state so a crash can be healed.

    Best-effort; never raises. A missing rect is tolerated (we still record the
    hwnd and ex-style so self-heal can at least drop TRANSPARENT / TOPMOST).
    ``kind`` ('terminal'/'claude-desktop'/...) lets self-heal apply the right
    hwnd-reuse guard.
    """
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "hwnd": int(hwnd),
            "rect": list(rect) if rect else None,
            "exstyle": int(exstyle) if exstyle is not None else None,
            "was_topmost": bool(was_topmost),
            "was_maximized": bool(was_maximized),
            "kind": kind or "",
            "pid": os.getpid(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def _clear_state() -> None:
    """Delete the crash-safety state file (graceful shutdown). Never raises."""
    try:
        path = _state_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _looks_like_controlling_window(hwnd: int, kind: str = "") -> bool:
    """Heuristic: is ``hwnd`` still the controlling window we recorded?

    A force-killed session records the controlling window's hwnd; by the next
    startup that handle could have been recycled by the OS for an unrelated
    window. Confirm it still matches the expected ``kind`` before touching it:

    * ``'claude-desktop'`` — must still be THE Claude Desktop main window
      (strong identity: class + 'Claude' title + claude.exe owner + not iconic),
      since an Electron process owns many ``Chrome_WidgetWin_1`` windows and a
      recycled hwnd could otherwise be maximized/moved by mistake.
    * anything else (``'terminal'``, a legacy ``''`` from a pre-CDC state file,
      or ``'fallback'``) — only heal a real terminal host; a non-terminal
      fallback window is intentionally left untouched.
    """
    if kind == "claude-desktop":
        # Loose re-identity for healing a hwnd WE recorded: class + claude.exe
        # owner only. The stricter title / not-iconic checks used for system-wide
        # RESOLUTION must NOT gate healing — a stuck window can be iconic or have a
        # changed title, and we still need to un-stick it.
        try:
            if (win32gui.GetClassName(hwnd) or "") != "Chrome_WidgetWin_1":
                return False
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            return _snapshot_processes().get(pid, (0, ""))[1] in _CLAUDE_DESKTOP_EXES
        except Exception:
            return False

    cls = ""
    exe = ""
    try:
        cls = (win32gui.GetClassName(hwnd) or "").upper()
    except Exception:
        pass
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = _snapshot_processes().get(pid, (0, ""))[1]
    except Exception:
        pass
    return "CASCADIA" in cls or "CONSOLE" in cls or exe in _TERMINAL_EXES


def self_heal_if_needed() -> bool:
    """Restore an orphaned terminal left behind by a force-killed prior session.

    On startup, a lingering state file means the previous session did not shut
    down gracefully (e.g. it was force-killed mid-session, bypassing the atexit
    restore), so the terminal it controlled may be left shrunk into the corner,
    topmost, click-through, or parked off-screen. Read the recorded original
    rect / ex-style and put that window back, then delete the state file.

    Guarded against hwnd reuse via :func:`_looks_like_controlling_window`.
    Best-effort;
    never raises.

    Returns:
        ``True`` if a window was actually healed, else ``False``.
    """
    try:
        path = _state_path()
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:
        return False

    healed = False
    window_gone = False
    try:
        hwnd = int(st.get("hwnd") or 0)
        rect = st.get("rect")
        exstyle = st.get("exstyle")
        was_topmost = bool(st.get("was_topmost"))
        was_maximized = bool(st.get("was_maximized"))
        kind = str(st.get("kind") or "")
        if not hwnd or not win32gui.IsWindow(hwnd):
            window_gone = True
        else:
            # For a Claude Desktop window ALWAYS neutralize the most user-hostile
            # leaked style (input-transparent click-through) up front, even if the
            # identity check below is uncertain — clearing transparent on a wrong
            # Electron window is far less harmful than leaving the real Claude
            # window permanently un-clickable.
            if kind == "claude-desktop":
                _strip_clickthrough_exstyle(hwnd)
            if _looks_like_controlling_window(hwnd, kind):
                if exstyle is not None:
                    try:
                        win32gui.SetWindowLong(
                            hwnd, win32con.GWL_EXSTYLE, int(exstyle)
                        )
                    except Exception:
                        pass
                if rect and len(rect) == 4:
                    l, t, r, b = (int(v) for v in rect)
                    w = max(1, r - l)
                    h = max(1, b - t)
                    insert_after = (
                        win32con.HWND_TOPMOST
                        if was_topmost
                        else win32con.HWND_NOTOPMOST
                    )
                    try:
                        win32gui.SetWindowPos(
                            hwnd,
                            insert_after,
                            l,
                            t,
                            w,
                            h,
                            win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED,
                        )
                    except Exception:
                        pass
                # Re-maximize if the orphaned window was maximized before shrinking.
                if was_maximized:
                    try:
                        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                    except Exception:
                        pass
                healed = True
    except Exception:
        pass
    finally:
        # Consume the recovery record only when we actually healed or the window
        # is genuinely gone. If it is still live but failed the identity guard,
        # KEEP the record so a later startup can retry (don't strand it).
        if healed or window_gone:
            _clear_state()
    return healed


def hide_for_capture() -> bool:
    """Move the controlling terminal off-screen so it is absent from captures.

    Cross-process ``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` does not
    work on the terminal, and rect-masking blacks out anything sitting on top of
    it. Instead we park the window far off the virtual desktop for the duration
    of the screen grab, then :func:`restore_after_capture` puts it back.

    Stashes the window's current ``GetWindowRect`` then ``SetWindowPos`` to a far
    off-screen origin with ``SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSIZE`` (size is
    left unchanged). Uses the same cached controlling-window hwnd as
    :func:`find_controlling_window`. Everything is guarded; never raises.

    Returns:
        ``True`` if a window was resolved and moved off-screen, else ``False``.
    """
    global _hidden_rect, _hidden

    if not config.HIDE_CONTROLLING:
        return False

    try:
        hwnd = find_controlling_window()
    except Exception:
        return False
    if not hwnd:
        return False
    try:
        if not win32gui.IsWindow(hwnd):
            return False
    except Exception:
        return False

    try:
        _hidden_rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        _hidden_rect = None

    flags = (
        win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER | win32con.SWP_NOSIZE
    )
    try:
        win32gui.SetWindowPos(
            hwnd, 0, _OFFSCREEN_X, _OFFSCREEN_Y, 0, 0, flags
        )
        _hidden = True
        return True
    except Exception:
        _hidden = False
        return False


def restore_after_capture() -> None:
    """Move the controlling terminal back from its off-screen parking position.

    Idempotent and safe to call even if :func:`hide_for_capture` did not hide
    anything (it simply does nothing). Designed to be called from a ``finally``
    block. Never raises.
    """
    global _hidden_rect, _hidden

    if not _hidden:
        return

    hwnd = _terminal_hwnd
    rect = _hidden_rect
    # Clear state up front so repeated calls are no-ops regardless of outcome.
    _hidden = False
    _hidden_rect = None

    if not hwnd or rect is None:
        return
    try:
        if not win32gui.IsWindow(hwnd):
            return
    except Exception:
        return

    l, t, r, b = rect
    w = max(1, r - l)
    h = max(1, b - t)
    # Re-assert TOPMOST so the terminal returns above other windows after the
    # off-screen round-trip (the shrunk session keeps it topmost).
    flags = win32con.SWP_NOACTIVATE | win32con.SWP_NOSIZE
    try:
        # NOSIZE keeps the original size; the (l, t) origin is what we restore.
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, l, t, w, h, flags)
    except Exception:
        pass


def excluded_hwnds() -> list[int]:
    """Return hwnds the screenshot path should mask by rect.

    No-op for the terminal: the terminal is now hidden by moving it off-screen
    during capture (see :func:`hide_for_capture`) rather than by masking, so this
    always returns an empty list. Kept for API compatibility. The overlay windows
    exclude themselves via their own same-process WDA (see :mod:`overlay`).

    Returns:
        Always an empty list.
    """
    return []


def mask_rects() -> list[tuple[int, int, int, int]]:
    """Return physical-pixel rects for the screenshot path.

    No-op for the terminal: masking has been replaced by the off-screen hide
    (see :func:`hide_for_capture`), so this always returns an empty list. Kept
    for API compatibility. Never raises.

    Returns:
        Always an empty list.
    """
    return []


# ---------------------------------------------------------------------------
# Diagnostics CLI
# ---------------------------------------------------------------------------


def _diagnostics() -> None:
    """Print resolution diagnostics to stdout (used by ``python -m``)."""
    hwnd = find_controlling_window()
    title = ""
    exe = ""
    rect = None
    if hwnd:
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            title = "<unreadable>"
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            procs = _snapshot_processes()
            exe = procs.get(pid, (0, ""))[1]
        except Exception:
            exe = ""
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            rect = None
    print(f"find_controlling_window() = {hwnd!r}")
    print(f"  kind         = {_controlling_kind!r}")
    print(f"  window title = {title!r}")
    print(f"  owning exe   = {exe!r}")
    print(f"  window rect  = {rect!r}")
    chain = _ancestry_pids()
    print(f"  ancestry     = {chain}")


if __name__ == "__main__":  # pragma: no cover
    try:
        _diagnostics()
    except Exception as exc:  # never crash the diagnostics
        print(f"diagnostics failed: {exc}", file=sys.stderr)
