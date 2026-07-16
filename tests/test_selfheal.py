"""Standalone test of terminal.self_heal_if_needed().

Launches a throwaway Windows Terminal, mangles it (shrink + topmost) like an
orphaned force-killed session would leave it, writes a state file recording its
TRUE original rect/exstyle, then calls self_heal_if_needed() and verifies the
window is restored and the state file deleted. Uses an isolated
COMPUTER_USE_LOG_DIR so it never touches the live MCP's state file.
"""
import os
import subprocess
import sys
import tempfile
import time

os.environ["COMPUTER_USE_LOG_DIR"] = tempfile.mkdtemp(prefix="ccc-heal-")

import win32con
import win32gui
import win32process

from omni_computer_use import terminal

CASCADIA = "CASCADIA_HOSTING_WINDOW_CLASS"


def find_wt():
    found = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == CASCADIA:
                found.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


before = set(find_wt())
subprocess.Popen("wt -w new", shell=True)
hwnd = 0
for _ in range(30):
    time.sleep(0.2)
    new = set(find_wt()) - before
    if new:
        hwnd = next(iter(new))
        break
if not hwnd:
    print("FAIL: could not spawn a throwaway Windows Terminal")
    sys.exit(1)

orig_rect = win32gui.GetWindowRect(hwnd)
orig_ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
orig_topmost = bool(orig_ex & win32con.WS_EX_TOPMOST)
print(f"test wt   hwnd={hwnd} rect={orig_rect} ex=0x{orig_ex:08X} topmost={orig_topmost}")

# Mangle it like an orphaned session: shrink into a corner + topmost.
win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 1400, 0, 500, 400, win32con.SWP_NOACTIVATE)
time.sleep(0.3)
m_rect = win32gui.GetWindowRect(hwnd)
m_ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
print(f"mangled   rect={m_rect} ex=0x{m_ex:08X} topmost={bool(m_ex & win32con.WS_EX_TOPMOST)}")

# Write the state file recording the TRUE original (as shrink_to_corner would).
terminal._write_state(hwnd, orig_rect, orig_ex, orig_topmost)
sp = terminal._state_path()
print(f"state written: exists={os.path.exists(sp)}")

# Self-heal.
healed = terminal.self_heal_if_needed()
time.sleep(0.3)
h_rect = win32gui.GetWindowRect(hwnd)
h_ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
print(f"self_heal returned {healed}")
print(f"healed    rect={h_rect} ex=0x{h_ex:08X} topmost={bool(h_ex & win32con.WS_EX_TOPMOST)}")
print(f"state file deleted: {not os.path.exists(sp)}")

ok = (
    healed
    and abs(h_rect[0] - orig_rect[0]) < 5
    and abs(h_rect[1] - orig_rect[1]) < 5
    and abs(h_rect[2] - orig_rect[2]) < 5
    and abs(h_rect[3] - orig_rect[3]) < 5
    and bool(h_ex & win32con.WS_EX_TOPMOST) == orig_topmost
    and not os.path.exists(sp)
)
print("SELF-HEAL TEST:", "PASS" if ok else "FAIL")

# Cleanup: close ONLY the throwaway window with WM_CLOSE — NEVER taskkill the
# process. Windows Terminal is single-process / multi-window, so killing the
# process also closes the user's CCC terminal sharing it (learned the hard way).
# WM_CLOSE on this hwnd closes just this window; the shared process survives
# while other windows (the CCC terminal) remain.
try:
    WM_CLOSE = 0x0010
    win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
    print("closed throwaway wt window (WM_CLOSE; shared process untouched)")
except Exception as exc:
    print(f"cleanup failed (please close the test window manually): {exc!r}")
