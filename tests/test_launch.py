"""Standalone check of launch_and_focus honest reporting (no MCP restart needed).

Run:  uv run python tests/test_launch.py

Verifies the paths that open_application's honest reporting relies on:
  1. A real GUI exe launches and shows a window (exited=False, hwnd set), so the
     caller can honestly report "Opened".
  2. An instantly-exiting exe is detected as crash-on-startup (exited=True), so
     the caller reports an error instead of a fake "Opened".
  3. A bare name that resolves to nothing is flagged (kind=shell-bare, no window).
"""

import time

import win32con
import win32gui

from omni_computer_use import apps

NOTEPAD = r"C:\Windows\System32\notepad.exe"   # a real GUI exe that shows a window
WHERE = r"C:\Windows\System32\where.exe"        # exits immediately, no window


def _close(hwnd: int) -> None:
    if hwnd:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


def _verdict(r: apps.LaunchResult) -> str:
    if r.exited:
        return "CRASH-REPORTED (exited=True -> open_application raises)"
    if r.hwnd:
        return "OPENED (honest success)"
    if r.pid:
        return "ALIVE-NO-WINDOW (honest soft message)"
    if r.kind == "shell-bare":
        return "SHELL-BARE-NOTHING (open_application raises 'could not confirm')"
    return "MONIKER/FILE unverifiable (lenient 'Opened')"


def main() -> None:
    print("== Test 1: real GUI exe (expect a window, exited=False) ==")
    info = apps.AppInfo(
        display="notepad",
        exe="notepad.exe",
        launch=NOTEPAD,
        bundle_id="com.omni.notepad",
    )
    r = apps.launch_and_focus(info)
    print(f"  -> hwnd={r.hwnd} pid={r.pid} exited={r.exited}")
    print("  verdict:", _verdict(r))
    time.sleep(0.6)
    _close(r.hwnd)

    print("== Test 2: instantly-exiting exe (expect exited=True) ==")
    info2 = apps.AppInfo(
        display="where",
        exe="where.exe",
        launch=WHERE,
        bundle_id="com.omni.where",
    )
    r2 = apps.launch_and_focus(info2)
    print(f"  -> hwnd={r2.hwnd} pid={r2.pid} exited={r2.exited} kind={r2.kind}")
    print("  verdict:", _verdict(r2))

    print("== Test 3: bogus bare name (expect kind=shell-bare, nothing) ==")
    info3 = apps.AppInfo(
        display="com.omni.nonexistent-app",
        exe="com.omni.nonexistent-app.exe",
        launch="com.omni.nonexistent-app",
        bundle_id="com.omni.nonexistent-app",
    )
    r3 = apps.launch_and_focus(info3)
    print(f"  -> hwnd={r3.hwnd} pid={r3.pid} exited={r3.exited} kind={r3.kind}")
    print("  verdict:", _verdict(r3))


if __name__ == "__main__":
    main()
