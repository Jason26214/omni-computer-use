"""Application resolution, launching/focusing, foreground & window enumeration.

Responsibilities:

* Resolve a human app name to an :class:`AppInfo` by searching Start Menu
  shortcuts (under ProgramData and the user's AppData) and running processes,
  with a fallback path for UWP apps via ``shell:AppsFolder``.
* Launch an app and bring its main window to the foreground.
* Report the foreground window's process basename.
* Enumerate visible top-level windows with physical-pixel rectangles.
* Compute screenshot mask rectangles for windows not belonging to allowlisted
  applications.

All physical rectangles are ``(x0, y0, x1, y1)`` in virtual-desktop pixels.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process


@dataclass
class AppInfo:
    """A resolved application.

    Attributes:
        display: Human-friendly display name, e.g. ``'Notepad'``.
        exe: Expected foreground process basename, lowercased (best effort),
            e.g. ``'notepad.exe'``. Used to match foreground/allowlist.
        launch: The command or target used to launch the app (path to an
            executable, a ``.lnk``, or a ``shell:AppsFolder\\...`` moniker).
        bundle_id: Synthetic stable identifier for the app (CDC-style), used in
            permission payloads.
    """

    display: str
    exe: str
    launch: str
    bundle_id: str


@dataclass
class WinInfo:
    """A visible top-level window.

    Attributes:
        hwnd: Win32 window handle.
        rect: ``(x0, y0, x1, y1)`` window rectangle in physical pixels.
        exe: Owning process basename, lowercased.
        visible: Whether the window is currently visible.
    """

    hwnd: int
    rect: tuple[int, int, int, int]
    exe: str
    visible: bool


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400


def _bundle_id_from(display: str) -> str:
    """Build a synthetic, stable, CDC-style bundle id from a display name.

    Idempotent: if ``display`` is already a ``com.omni.*`` bundle id we previously
    emitted (callers routinely round-trip the bundleId back in as the app name),
    return it unchanged instead of slugifying the dots away and re-prefixing —
    which used to yield a double prefix like ``com.omni.com-ccc-...``.
    """
    text = display.strip()
    if text.lower().startswith("com.omni."):
        return text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        slug = "app"
    return f"com.omni.{slug}"


def _process_exe_path(pid: int) -> str:
    """Return the full executable path for ``pid`` (psutil-free), or ''.

    Uses ``OpenProcess`` + ``QueryFullProcessImageNameW`` via ctypes, which works
    for most processes when running with normal privileges.
    """
    if not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf_len = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(buf_len.value)
        if kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(buf_len)
        ):
            return buf.value
        # Fallback to win32 helper (PSAPI) if the modern call failed.
        try:
            return win32process.GetModuleFileNameEx(handle, 0)
        except Exception:
            return ""
    finally:
        kernel32.CloseHandle(handle)


def _exe_basename_for_hwnd(hwnd: int) -> str:
    """Return the lowercased process basename owning ``hwnd``, or ''."""
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return ""
    path = _process_exe_path(pid)
    if not path:
        return ""
    return os.path.basename(path).lower()


# ---------------------------------------------------------------------------
# Start Menu shortcut scanning
# ---------------------------------------------------------------------------


def _start_menu_dirs() -> list[str]:
    """Return existing Start Menu Programs roots (machine + user)."""
    dirs: list[str] = []
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    appdata = os.environ.get("APPDATA", "")
    candidates = [
        os.path.join(
            program_data,
            "Microsoft",
            "Windows",
            "Start Menu",
            "Programs",
        )
    ]
    if appdata:
        candidates.append(
            os.path.join(
                appdata,
                "Microsoft",
                "Windows",
                "Start Menu",
                "Programs",
            )
        )
    for d in candidates:
        if os.path.isdir(d):
            dirs.append(d)
    return dirs


def _iter_shortcuts() -> list[tuple[str, str]]:
    """Return ``(display_name, lnk_path)`` for every ``.lnk`` under Start Menu."""
    out: list[tuple[str, str]] = []
    for root in _start_menu_dirs():
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.lower().endswith(".lnk"):
                    display = os.path.splitext(fn)[0]
                    out.append((display, os.path.join(dirpath, fn)))
    return out


def _resolve_lnk_target(lnk_path: str) -> str:
    """Best-effort resolution of a ``.lnk`` to its target executable path.

    Uses the WScript.Shell COM object. Returns '' if it cannot be resolved.
    """
    try:
        import pythoncom  # noqa: WPS433 (local import: COM only on demand)
        import win32com.client  # noqa: WPS433

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(lnk_path)
            target = shortcut.Targetpath or ""
            # Release COM references before CoUninitialize to avoid a noisy
            # "Win32 exception releasing IUnknown" on teardown.
            del shortcut
            del shell
            return target
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        return ""


def _exe_from_lnk(lnk_path: str) -> str:
    """Return the lowercased target basename for a ``.lnk`` (best effort)."""
    target = _resolve_lnk_target(lnk_path)
    if target and target.lower().endswith(".exe"):
        return os.path.basename(target).lower()
    return ""


# ---------------------------------------------------------------------------
# Running-process scanning (for matching by display/exe of live windows)
# ---------------------------------------------------------------------------


def _running_exes() -> dict[str, str]:
    """Return ``{exe_basename_lower: full_path}`` for processes with windows."""
    result: dict[str, str] = {}

    def _cb(hwnd: int, _extra: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        path = _process_exe_path(pid)
        if path:
            base = os.path.basename(path).lower()
            result.setdefault(base, path)
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Start-Apps (Get-StartApps) resolution — covers UWP/packaged apps that have
# no .lnk in the Start Menu (Calculator, Notepad, Paint, Settings, ...).
# ---------------------------------------------------------------------------

# Module-level cache. ``Get-StartApps`` costs ~0.3s, so we only ever shell out
# once per process. ``None`` means "not fetched yet".
_START_APPS_CACHE: list[dict[str, str]] | None = None


def _start_apps() -> list[dict[str, str]]:
    """Return the Start-Apps catalogue as ``[{'Name', 'AppID'}, ...]``.

    Runs ``Get-StartApps | ConvertTo-Json -Compress`` exactly once per process
    and caches the parsed result. ``Get-StartApps`` returns ``Name -> AppID``
    where ``AppID`` is either a UWP AUMID (e.g.
    ``Microsoft.WindowsCalculator_8wekyb3d8bbwe!App``) or a filesystem path for
    classic apps. Never raises; returns ``[]`` on any failure.
    """
    global _START_APPS_CACHE
    if _START_APPS_CACHE is not None:
        return _START_APPS_CACHE

    apps: list[dict[str, str]] = []
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (proc.stdout or "").strip()
        if raw:
            data = json.loads(raw)
            # ConvertTo-Json emits a bare object (dict) when there's a single
            # result, otherwise a list of objects.
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("Name", "") or "")
                    app_id = str(item.get("AppID", "") or "")
                    if name and app_id:
                        apps.append({"Name": name, "AppID": app_id})
    except Exception:
        apps = []

    _START_APPS_CACHE = apps
    return apps


def _is_uwp_aumid(app_id: str) -> bool:
    """True if ``app_id`` looks like a UWP AUMID rather than a filesystem path.

    AUMIDs contain a ``!`` (e.g. ``...!App``) and are not paths. A filesystem
    path (``C:\\...`` or anything ending in ``.exe``) is treated as classic.
    """
    if not app_id:
        return False
    # Filesystem path / classic exe -> not a UWP moniker.
    if re.match(r"^[a-zA-Z]:[\\/]", app_id) or app_id.lower().endswith(".exe"):
        return False
    return "!" in app_id


def _match_start_app(q_lower: str, q_stem: str) -> dict[str, str] | None:
    """Find the best Start-Apps entry for the query (exact > startswith > contains).

    Matching is case-insensitive. Within the startswith/contains tiers the
    shortest name wins (least noise).
    """
    apps = _start_apps()
    if not apps:
        return None

    starts: list[dict[str, str]] = []
    contains: list[dict[str, str]] = []
    for item in apps:
        n_lower = item["Name"].lower()
        if n_lower == q_lower or n_lower == q_stem:
            return item
        if q_stem and n_lower.startswith(q_stem):
            starts.append(item)
        elif q_stem and q_stem in n_lower:
            contains.append(item)

    if starts:
        return min(starts, key=lambda it: len(it["Name"]))
    if contains:
        return min(contains, key=lambda it: len(it["Name"]))
    return None


def _app_info_from_start(item: dict[str, str]) -> AppInfo:
    """Build an :class:`AppInfo` from a Start-Apps ``{'Name', 'AppID'}`` entry."""
    name = item["Name"]
    app_id = item["AppID"]
    if _is_uwp_aumid(app_id):
        # UWP: launch via the AppsFolder moniker. The actual foreground process
        # is ApplicationFrameHost / the package host, which we can't know here.
        launch = "shell:AppsFolder\\" + app_id
        exe = ""
    else:
        # Classic app whose Start entry is a filesystem path.
        launch = app_id
        base = os.path.basename(app_id)
        exe = base.lower() if base.lower().endswith(".exe") else ""
    return AppInfo(
        display=name,
        exe=exe,
        launch=launch,
        bundle_id=_bundle_id_from(name),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_app(name: str) -> AppInfo | None:
    """Resolve a human app name to an :class:`AppInfo`.

    Searches Start Menu ``.lnk`` shortcuts under ProgramData and the user's
    AppData, plus currently running processes, using a case-insensitive
    contains/best-match strategy. For packaged (UWP) apps, allows resolution via
    ``shell:AppsFolder``.

    Args:
        name: The requested application name.

    Returns:
        The best-matching :class:`AppInfo`, or ``None`` if nothing matched.
    """
    query = (name or "").strip()
    if not query:
        return None
    q_lower = query.lower()
    q_stem = os.path.splitext(q_lower)[0]  # tolerate 'notepad.exe' input

    # Try Start-Apps (Get-StartApps) FIRST: it's the only source that covers
    # UWP/packaged apps (Calculator, Notepad, Paint, Settings, ...), which have
    # no .lnk on disk. Prefer a clean exact name match; otherwise fall through
    # to .lnk/process resolution and let those win for non-Start apps.
    start_match = _match_start_app(q_lower, q_stem)
    if start_match is not None:
        sm_lower = start_match["Name"].lower()
        if sm_lower == q_lower or sm_lower == q_stem:
            return _app_info_from_start(start_match)

    shortcuts = _iter_shortcuts()

    exact: tuple[str, str] | None = None
    contains: list[tuple[str, str]] = []
    for display, lnk in shortcuts:
        d_lower = display.lower()
        if d_lower == q_lower or d_lower == q_stem:
            exact = (display, lnk)
            break
        if q_stem and (q_stem in d_lower or d_lower in q_stem):
            contains.append((display, lnk))

    chosen: tuple[str, str] | None = exact
    if chosen is None and contains:
        # Best match: shortest display name containing the query (least noise).
        chosen = min(contains, key=lambda t: len(t[0]))

    if chosen is not None:
        display, lnk = chosen
        exe = _exe_from_lnk(lnk)
        if not exe:
            # Fall back to a sensible basename guess from the display name.
            exe = re.sub(r"\s+", "", display).lower() + ".exe"
        return AppInfo(
            display=display,
            exe=exe,
            launch=lnk,
            bundle_id=_bundle_id_from(display),
        )

    # No shortcut match -> check running processes by exe basename.
    for base, path in _running_exes().items():
        stem = os.path.splitext(base)[0]
        if stem == q_stem or q_stem in stem or stem in q_stem:
            display = os.path.splitext(os.path.basename(path))[0]
            return AppInfo(
                display=display,
                exe=base,
                launch=path,
                bundle_id=_bundle_id_from(display),
            )

    # Before the bare-name fallback, honour a non-exact Start-Apps match
    # (startswith/contains) — it's still a real installed app and beats a
    # synthesized launch target.
    if start_match is not None:
        return _app_info_from_start(start_match)

    # Last resort: if the query already looks like a bare executable name,
    # treat it as a UWP/AppsFolder or direct-launch target so the caller can
    # still attempt a launch.
    if q_stem:
        exe = q_stem + ".exe"
        return AppInfo(
            display=query,
            exe=exe,
            launch=query,
            bundle_id=_bundle_id_from(query),
        )
    return None


def _find_window_for_pid(pid: int) -> int:
    """Return the first visible top-level window owned by ``pid``, or 0."""
    found: list[int] = []

    def _cb(hwnd: int, _extra: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _tid, wpid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if wpid == pid and win32gui.GetWindowText(hwnd):
            found.append(hwnd)
            return False
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return found[0] if found else 0


def _force_foreground(hwnd: int) -> None:
    """Bring ``hwnd`` to the foreground, restoring if minimized."""
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    try:
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:
        pass
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # SetForegroundWindow can fail if the foreground lock is held; nudge it.
        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass


@dataclass
class LaunchResult:
    """Outcome of :func:`launch_and_focus`.

    Attributes:
        hwnd: Focused top-level window handle, or 0 if none appeared in time.
        pid: PID of the process we spawned directly (classic ``.exe`` path); 0
            for shell/UWP/moniker launches where no child handle is available.
        exited: True only when we had a ``pid`` and that process exited within
            the poll window without ever showing a window — i.e. it launched
            then died (crash-on-startup). Always False when ``pid`` is 0.
        kind: How the launch was dispatched — ``'popen'`` (spawned a real exe,
            so ``pid`` is set), ``'shell-file'`` (shell-launched an existing file
            or UWP/moniker target — unverifiable but legitimate), or
            ``'shell-bare'`` (shell-launched a bare name that matched no on-disk
            target — if nothing appears it almost certainly opened nothing).
    """

    hwnd: int
    pid: int
    exited: bool
    kind: str


def launch_and_focus(app: AppInfo) -> LaunchResult:
    """Launch an application and bring its window to the foreground.

    For a classic on-disk ``.exe`` the process is spawned directly via
    :class:`subprocess.Popen` with ``cwd`` set to the executable's own directory
    (mirroring an Explorer double-click). This both yields a PID we can watch for
    crash-on-startup and keeps apps that resolve sidecar resources relative to
    the working directory (Tauri/WebView2 dev builds) happy. Shortcuts, monikers
    and UWP AppsFolder targets still go through the shell, where no child PID is
    available.

    After launching, polls up to ~2s for a focusable top-level window and brings
    it forward (``AllowSetForegroundWindow`` + ``SetForegroundWindow`` +
    ``ShowWindow`` restore). Intentionally **non-blocking** and never raises.

    Args:
        app: The application to launch and focus.

    Returns:
        A :class:`LaunchResult` describing what actually happened, so the caller
        can report honestly instead of assuming success.
    """
    target = app.launch
    t_lower = target.lower()
    # Monikers/URIs (shell:AppsFolder\..., ms-settings:, http(s):, mailto:, ...)
    # must go through the shell namespace, not the filesystem.
    is_moniker = (
        t_lower.startswith("shell:")
        or t_lower.startswith("ms-settings:")
        or bool(re.match(r"^[a-z][a-z0-9+.-]*:", t_lower))
        and not re.match(r"^[a-z]:[\\/]", t_lower)  # not a drive path like C:\
    )

    proc: subprocess.Popen | None = None
    kind = "shell-bare"
    try:
        if not is_moniker and t_lower.endswith(".exe") and os.path.exists(target):
            # Classic executable: spawn directly so we get a PID to watch and a
            # working directory of the exe's own folder. Fall back to the shell
            # if Popen refuses the target.
            kind = "popen"
            try:
                proc = subprocess.Popen(
                    [target],
                    cwd=os.path.dirname(target) or None,
                    # Detach the child's std streams from ours: the MCP server's
                    # stdout IS the JSON-RPC transport, so a console child that
                    # prints to stdout would otherwise corrupt the protocol.
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
            except Exception:
                proc = None
                kind = "shell-file"
                os.startfile(target)  # type: ignore[attr-defined]
        elif is_moniker or os.path.exists(target):
            kind = "shell-file"
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            # Bare name / unknown moniker: ShellExecute resolves PATH and the
            # AppsFolder namespace.
            kind = "shell-bare"
            try:
                win32api.ShellExecute(0, "open", target, None, None, win32con.SW_SHOWNORMAL)
            except Exception:
                os.startfile(target)  # type: ignore[attr-defined]
    except Exception:
        # As a final fallback, attempt a launch via ShellExecute.
        try:
            win32api.ShellExecute(0, "open", target, None, None, win32con.SW_SHOWNORMAL)
        except Exception:
            return LaunchResult(hwnd=0, pid=0, exited=False, kind=kind)

    pid = proc.pid if proc is not None else 0
    want_exe = (app.exe or "").lower()

    # Poll briefly for a focusable window. Bounded to ~2s (13 x 0.15s) so the
    # caller never feels frozen. Prefer matching our own PID; fall back to the
    # expected exe basename (covers launcher stubs that re-exec under a new PID).
    hwnd = 0
    exited = False
    for _ in range(13):
        if pid:
            hwnd = _find_window_for_pid(pid)
        if not hwnd:
            for win in enumerate_windows():
                if (
                    want_exe
                    and win.exe == want_exe
                    and win32gui.GetWindowText(win.hwnd)
                ):
                    hwnd = win.hwnd
                    break
        if hwnd:
            break
        # No window yet: if we spawned the process and it has already exited, it
        # crashed on startup — stop early and report it instead of looking alive.
        if proc is not None and proc.poll() is not None:
            exited = True
            break
        time.sleep(0.15)

    if hwnd:
        _force_foreground(hwnd)

    return LaunchResult(hwnd=hwnd, pid=pid, exited=exited, kind=kind)


def foreground_process() -> str:
    """Return the foreground window's process basename, lowercased.

    Resolves ``GetForegroundWindow`` -> owning PID -> executable path and
    returns the lowercased basename (e.g. ``'notepad.exe'``).

    Returns:
        The lowercased process basename, or ``''`` if it cannot be determined.
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
    except Exception:
        return ""
    if not hwnd:
        return ""
    return _exe_basename_for_hwnd(hwnd)


def enumerate_windows() -> list[WinInfo]:
    """Enumerate visible top-level windows with physical-pixel rectangles.

    Walks ``EnumWindows`` for visible top-level windows and reads each rectangle
    via ``GetWindowRect`` (physical pixels, since the process is DPI aware).

    Returns:
        A list of :class:`WinInfo` for visible top-level windows.
    """
    windows: list[WinInfo] = []

    def _cb(hwnd: int, _extra: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        x0, y0, x1, y1 = rect
        # Skip degenerate / cloaked windows (zero or 1px area placeholders).
        if (x1 - x0) <= 1 or (y1 - y0) <= 1:
            return True
        exe = _exe_basename_for_hwnd(hwnd)
        windows.append(
            WinInfo(
                hwnd=hwnd,
                rect=(x0, y0, x1, y1),
                exe=exe,
                visible=True,
            )
        )
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return windows


# Shell window classes that represent the desktop / wallpaper host; these must
# never be masked or we would blank the whole screen.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_SHELL_EXES = {"explorer.exe"}


def mask_rects_for(
    allowed_exes: set[str],
) -> list[tuple[int, int, int, int]]:
    """Compute mask rectangles for windows not owned by allowlisted apps.

    Returns the physical rectangles of visible top-level windows whose process
    basename is **not** in ``allowed_exes``. Desktop/shell windows are skipped
    so the wallpaper and taskbar are not masked.

    Args:
        allowed_exes: Set of lowercased allowlisted process basenames.

    Returns:
        A list of ``(x0, y0, x1, y1)`` physical rectangles to blank out.
    """
    allowed = {e.lower() for e in allowed_exes}
    rects: list[tuple[int, int, int, int]] = []

    for win in enumerate_windows():
        exe = win.exe
        # Skip the desktop shell host itself (Progman/WorkerW) so wallpaper and
        # taskbar are preserved.
        try:
            cls = win32gui.GetClassName(win.hwnd)
        except Exception:
            cls = ""
        if cls in _SHELL_CLASSES:
            continue
        # Skip empty-title system windows (no visible chrome / overlays).
        try:
            title = win32gui.GetWindowText(win.hwnd)
        except Exception:
            title = ""
        if not title and (exe in _SHELL_EXES or not exe):
            continue
        if exe in allowed:
            continue
        rects.append(win.rect)

    return rects
