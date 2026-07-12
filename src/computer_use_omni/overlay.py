"""CDC-style computer-use visual overlay for computer-use-omni (Windows).

This module replicates, as closely as practical, the on-screen affordances that
Claude Desktop (CDC) shows while its computer-use feature is active:

* A **static orange glow** hugging all four edges of the virtual screen, most
  opaque at the very edge and fading smoothly to transparent toward the center
  over a band sized to a small fraction of the smaller screen dimension. The
  brand orange is ``#D97757`` (rgb 217, 119, 87).
* A **dead-centered pill** — a rounded cream capsule (``#FAF3F0``) with a solid
  orange dot and the text "Claude is using your computer" in bold ``#D97757``.
  It appears, holds ~5 seconds, then shrinks and flies toward the top-right
  corner and vanishes.

Both are drawn with **layered, click-through, topmost, no-activate, tool**
windows painted via ``UpdateLayeredWindow`` (per-pixel premultiplied alpha), so
they never steal focus, never appear on the taskbar, and never intercept input.
When requested they are excluded from screen captures via
``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` so the screenshots our MCP
returns look clean (just like CDC's).

The overlay runs on its **own daemon thread** with its **own Win32 message
pump**, because the MCP server owns the main thread with an asyncio stdio loop.

Design constraints honored here:

* The module imports cleanly even on non-Windows / headless hosts — every
  ``ctypes.windll`` access is deferred into functions and guarded.
* All Win32 calls are checked; failures are logged to stderr and degrade
  gracefully. Nothing here is allowed to crash the MCP.
* Module-level singleton state; ``start`` / ``stop`` / ``is_running`` are the
  public surface, plus a ``__main__`` CLI for human/agent visual verification.

Public API::

    start(color=(217, 119, 87), max_alpha=0.6, band_frac=0.05,
          exclude_from_capture=True, show_pill=True,
          pill_text="Claude is using your computer") -> None
    stop() -> None
    is_running() -> bool

CLI::

    python -m computer_use_omni.overlay --preview SECONDS [--no-exclude] [--no-pill]
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import PIL.Image


# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

# Extended window styles.
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

# Window styles.
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

# ShowWindow.
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

# UpdateLayeredWindow flags.
ULW_ALPHA = 0x00000002

# AlphaBlend op / flags.
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

# SetWindowDisplayAffinity.
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# SetWindowPos flags.
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

# Window messages.
WM_DESTROY = 0x0002
WM_NCDESTROY = 0x0082
WM_APP = 0x8000
WM_USER = 0x0400

# GetSystemMetrics virtual-screen indices.
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# CreateDIBSection.
BI_RGB = 0
DIB_RGB_COLORS = 0

# Class style.
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

# Custom message to request the pump thread to tear down and quit.
WM_OVERLAY_QUIT = WM_APP + 1


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# LRESULT / WPARAM / LPARAM are pointer-sized. wintypes provides WPARAM and
# LPARAM; LRESULT is just a signed pointer-sized integer.
LRESULT = ctypes.c_ssize_t

# WNDPROC signature: LRESULT CALLBACK WndProc(HWND, UINT, WPARAM, LPARAM).
# Using the exact wintypes (WPARAM/LPARAM) matters: a mismatch makes the
# callback fault during WM_NCCREATE, which surfaces as CreateWindowExW failing
# with ERROR_MOD_NOT_FOUND (126).
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Best-effort diagnostic to stderr + the persistent log. Never raises."""
    try:
        print(f"[overlay] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        from . import logsetup

        logsetup.info(f"[overlay] {msg}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------


@dataclass
class _State:
    thread: "threading.Thread | None" = None
    thread_id: int = 0  # Win32 thread id of the pump thread, for PostThreadMessage.
    ready: "threading.Event" = field(default_factory=threading.Event)
    running: bool = False
    # Config snapshot for the active session.
    color: tuple[int, int, int] = (217, 119, 87)
    max_alpha: float = 0.6
    band_frac: float = 0.05
    exclude_from_capture: bool = True
    show_pill: bool = True
    pill_text: str = "Claude is using your computer"


_state = _State()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start(
    color: tuple[int, int, int] = (217, 119, 87),
    max_alpha: float = 0.6,
    band_frac: float = 0.05,
    exclude_from_capture: bool = True,
    show_pill: bool = True,
    pill_text: str = "Claude is using your computer",
) -> None:
    """Start the overlay on a dedicated daemon thread. Idempotent; never raises.

    Spawns a daemon thread that creates the layered glow window covering the
    full virtual screen and (optionally) the centered pill window, then runs a
    Win32 message pump until :func:`stop` is called.

    Args:
        color: Glow RGB color (default Anthropic brand orange ``(217, 119, 87)``).
        max_alpha: Peak alpha at the very edge, ``0.0``-``1.0`` (default ``0.6``).
        band_frac: Glow band width as a fraction of ``min(screen_w, screen_h)``
            (default ``0.05``).
        exclude_from_capture: When ``True`` (default) the glow and pill are
            excluded from screen captures via ``SetWindowDisplayAffinity``. Set
            ``False`` so they ARE captured (used for visual self-verification).
        show_pill: When ``True`` (default) show the centered pill announcement.
        pill_text: Text rendered in the pill.
    """
    with _lock:
        if _state.running:
            return
        _state.color = color
        _state.max_alpha = max_alpha
        _state.band_frac = band_frac
        _state.exclude_from_capture = exclude_from_capture
        _state.show_pill = show_pill
        _state.pill_text = pill_text
        _state.ready = threading.Event()

        t = threading.Thread(
            target=_thread_main,
            name="ccc-overlay",
            daemon=True,
        )
        _state.thread = t
        _state.running = True
        try:
            t.start()
        except Exception as exc:  # pragma: no cover - thread start is reliable
            _state.running = False
            _state.thread = None
            _log(f"failed to start overlay thread: {exc!r}")
            return

    # Wait briefly for the pump to come up so the windows exist before we return,
    # but never block the caller for long.
    _state.ready.wait(timeout=2.0)


def stop() -> None:
    """Tear down the overlay. Idempotent; never raises.

    Posts a quit request to the pump thread, which destroys its windows and
    exits its message loop, then joins the thread with a timeout.
    """
    with _lock:
        t = _state.thread
        tid = _state.thread_id
        if not _state.running and t is None:
            return
        _state.running = False

    # Ask the pump thread to quit. PostThreadMessageW is the canonical way to
    # nudge a foreign thread's message loop.
    if tid:
        try:
            ctypes.windll.user32.PostThreadMessageW(
                wintypes.DWORD(tid), wintypes.UINT(WM_OVERLAY_QUIT), 0, 0
            )
        except Exception as exc:
            _log(f"PostThreadMessage on stop failed: {exc!r}")

    if t is not None:
        try:
            t.join(timeout=3.0)
        except Exception as exc:  # pragma: no cover
            _log(f"join on stop failed: {exc!r}")

    with _lock:
        _state.thread = None
        _state.thread_id = 0


def is_running() -> bool:
    """Return ``True`` if the overlay thread is currently active."""
    t = _state.thread
    return bool(_state.running and t is not None and t.is_alive())


# ---------------------------------------------------------------------------
# Virtual-screen geometry
# ---------------------------------------------------------------------------


@dataclass
class _VScreen:
    x: int
    y: int
    w: int
    h: int


def _virtual_screen() -> _VScreen:
    """Return the full virtual-screen rectangle in physical pixels."""
    gsm = ctypes.windll.user32.GetSystemMetrics
    x = gsm(SM_XVIRTUALSCREEN)
    y = gsm(SM_YVIRTUALSCREEN)
    w = gsm(SM_CXVIRTUALSCREEN)
    h = gsm(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        # Fallback to primary screen metrics.
        w = gsm(0) or 1920  # SM_CXSCREEN
        h = gsm(1) or 1080  # SM_CYSCREEN
        x, y = 0, 0
    return _VScreen(int(x), int(y), int(w), int(h))


def _primary_screen() -> _VScreen:
    """Return the primary monitor rectangle in physical pixels (origin at 0,0)."""
    gsm = ctypes.windll.user32.GetSystemMetrics
    w = gsm(0) or 1920  # SM_CXSCREEN
    h = gsm(1) or 1080  # SM_CYSCREEN
    return _VScreen(0, 0, int(w), int(h))


def _controlling_screen() -> _VScreen:
    """Monitor rectangle (physical) hosting the controlling window.

    Multi-monitor parity with the official computer-use: the glow and pill
    belong on the monitor the Claude/terminal window lives on — and ONLY that
    monitor; other displays stay untouched. Falls back to the primary monitor
    when the controlling window cannot be resolved.
    """
    try:
        from . import terminal

        hwnd = terminal.find_controlling_window()
        if hwnd:
            MONITOR_DEFAULTTONEAREST = 2
            user32 = ctypes.windll.user32
            hmon = user32.MonitorFromWindow(
                wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST
            )
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
                    r = mi.rcMonitor
                    return _VScreen(
                        int(r.left),
                        int(r.top),
                        int(r.right - r.left),
                        int(r.bottom - r.top),
                    )
    except Exception:
        pass
    return _primary_screen()


# ---------------------------------------------------------------------------
# Glow image construction (Pillow)
# ---------------------------------------------------------------------------


def _glow_image(
    w: int, h: int, color: tuple[int, int, int], max_alpha: float, band: float
) -> "PIL.Image.Image":
    """Return a top-down RGBA :class:`PIL.Image.Image` of the edge glow.

    ``alpha(d) = max_alpha * clamp(1 - d/band, 0, 1) ** 1.3`` where ``d`` is the
    distance to the nearest edge. Built with NumPy when available for speed,
    else with a pure-Pillow per-edge gradient composite.
    """
    from PIL import Image

    band = max(1.0, float(band))
    r, g, b = color
    a_peak = max(0.0, min(1.0, float(max_alpha)))

    try:
        import numpy as np

        # Distance to nearest edge for every pixel.
        xs = np.minimum(np.arange(w), np.arange(w)[::-1]).astype(np.float32)  # to L/R edge
        ys = np.minimum(np.arange(h), np.arange(h)[::-1]).astype(np.float32)  # to T/B edge
        dist = np.minimum(xs[None, :], ys[:, None])  # (h, w)
        t = np.clip(1.0 - dist / band, 0.0, 1.0)
        alpha = (a_peak * np.power(t, 1.3) * 255.0).astype(np.uint8)

        rgba = np.empty((h, w, 4), dtype=np.uint8)
        rgba[..., 0] = r
        rgba[..., 1] = g
        rgba[..., 2] = b
        rgba[..., 3] = alpha
        return Image.fromarray(rgba, "RGBA")
    except Exception:
        # Pure-Pillow fallback: build a 1-D alpha ramp and paint four edge bands.
        return _glow_image_pillow(w, h, color, a_peak, band)


def _glow_image_pillow(
    w: int, h: int, color: tuple[int, int, int], a_peak: float, band: float
) -> "PIL.Image.Image":
    """NumPy-free edge glow using max-compositing of four directional ramps."""
    from PIL import Image

    r, g, b = color
    bandi = int(round(band))
    if bandi < 1:
        bandi = 1

    # Per-distance alpha lookup (0..bandi).
    def alpha_at(d: int) -> int:
        if d >= bandi:
            return 0
        t = 1.0 - (d / band)
        if t < 0.0:
            t = 0.0
        return int(a_peak * (t ** 1.3) * 255.0)

    alpha = Image.new("L", (w, h), 0)
    px = alpha.load()
    # Only the band region near each edge is non-zero; iterate that perimeter.
    for y in range(h):
        dy = min(y, h - 1 - y)
        for x in range(w):
            dx = min(x, w - 1 - x)
            d = dx if dx < dy else dy
            if d < bandi:
                px[x, y] = alpha_at(d)

    rgba = Image.new("RGBA", (w, h), (r, g, b, 0))
    rgba.putalpha(alpha)
    return rgba


def _image_to_premultiplied_bgra_bottomup(img: "PIL.Image.Image") -> bytes:
    """Convert a top-down RGBA image to a **bottom-up premultiplied BGRA** buffer.

    ``UpdateLayeredWindow`` with a DIB section expects premultiplied alpha
    (``AC_SRC_ALPHA``). We build a bottom-up DIB (positive ``biHeight``), so the
    image's last row is written first.

    Returns:
        ``bytes`` of length ``w * h * 4``.
    """
    from PIL import Image

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size

    try:
        import numpy as np

        arr = np.asarray(img, dtype=np.uint16)  # (h, w, 4) RGBA
        a = arr[..., 3:4]
        # Premultiply RGB by alpha.
        rgb = (arr[..., :3] * a // 255).astype(np.uint8)
        a8 = arr[..., 3].astype(np.uint8)
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[..., 0] = rgb[..., 2]  # B
        bgra[..., 1] = rgb[..., 1]  # G
        bgra[..., 2] = rgb[..., 0]  # R
        bgra[..., 3] = a8  # A
        # Bottom-up DIB: flip rows.
        bgra = bgra[::-1, :, :]
        return bgra.tobytes()
    except Exception:
        # Pure-Python fallback using Pillow channel access.
        flipped = img.transpose(Image.FLIP_TOP_BOTTOM)
        r, g, b, a = flipped.split()
        rb = r.tobytes()
        gb = g.tobytes()
        bb = b.tobytes()
        ab = a.tobytes()
        out = bytearray(w * h * 4)
        for i in range(w * h):
            av = ab[i]
            out[i * 4 + 0] = (bb[i] * av) // 255  # B
            out[i * 4 + 1] = (gb[i] * av) // 255  # G
            out[i * 4 + 2] = (rb[i] * av) // 255  # R
            out[i * 4 + 3] = av  # A
        return bytes(out)


# ---------------------------------------------------------------------------
# Pill image construction (Pillow)
# ---------------------------------------------------------------------------


def _pill_image(text: str, scale: float = 1.0) -> "PIL.Image.Image":
    """Render the announcement pill to a top-down RGBA image.

    A rounded cream capsule (~``#FAF3F0``) with a solid orange dot on the left
    and ``text`` in bold Anthropic orange (``#D97757``). ``scale`` shrinks the
    whole thing for the fly-away animation (geometry scales; the returned canvas
    is sized to the scaled pill).

    Returns:
        A tight RGBA image of the pill at the requested ``scale``.
    """
    from PIL import Image, ImageDraw, ImageFont

    scale = max(0.05, float(scale))

    # Reference metrics measured on a 1600px-tall screen; scale by the actual
    # screen height so the pill keeps its proportions at any resolution.
    try:
        import ctypes as _ct
        _sh = _ct.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN (physical px)
        ui = max(0.5, _sh / 1600.0) if _sh else 1.0
    except Exception:
        ui = 1.0
    k = ui * scale
    pad_x = int(32 * k)
    pad_y = int(25 * k)
    icon_d = int(26 * k)
    icon_gap = int(14 * k)
    font_px = max(8, int(30 * k))

    font = _load_font(font_px)

    # Measure text.
    tmp = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    d0 = ImageDraw.Draw(tmp)
    try:
        bbox = d0.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        toff = (bbox[0], bbox[1])
    except Exception:
        tw, th = d0.textsize(text, font=font) if hasattr(d0, "textsize") else (
            len(text) * font_px // 2,
            font_px,
        )
        toff = (0, 0)

    content_w = icon_d + icon_gap + tw
    content_h = max(icon_d, th)
    w = content_w + pad_x * 2
    h = content_h + pad_y * 2
    radius = h // 2

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Cream capsule (#FAF3F0), near-opaque, with a faint warm rim.
    bg = (250, 243, 240, 250)
    draw.rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=radius, fill=bg,
        outline=(228, 208, 199, 130), width=max(1, int(2 * k)),
    )

    cy = h // 2
    orange = (217, 119, 87, 255)

    # Solid orange dot on the left (no sparkle).
    icon_cx = pad_x + icon_d // 2
    rdot = icon_d // 2
    draw.ellipse(
        [icon_cx - rdot, cy - rdot, icon_cx + rdot, cy + rdot], fill=orange,
    )

    # Text in bold Anthropic orange, vertically centered.
    tx = pad_x + icon_d + icon_gap - toff[0]
    ty = (h - th) // 2 - toff[1]
    draw.text((tx, ty), text, font=font, fill=orange)

    return img


def _load_font(px: int):
    """Load a UI font at ``px`` pixels, falling back to Pillow's default."""
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Layered window plumbing
# ---------------------------------------------------------------------------


@dataclass
class _LayeredWindow:
    """A single layered, click-through, topmost overlay window."""

    hwnd: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


def _module_handle() -> int:
    """Return this process's module handle (HINSTANCE) without truncation.

    The default ctypes ``restype`` is a signed ``int``, which truncates 64-bit
    handles on 64-bit Python and yields an invalid ``hInstance`` (the root cause
    of ``CreateWindowExW`` failing with last-error 0). Declaring the proper
    pointer-sized ``restype`` fixes this.
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    return kernel32.GetModuleHandleW(None)


def _register_class(class_name: str, wndproc_ref) -> bool:
    """Register a minimal window class. Returns True on success (or already)."""
    user32 = ctypes.windll.user32

    hinst = _module_handle()
    wc = WNDCLASSW()
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = wndproc_ref
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = 0
    wc.hCursor = 0
    wc.hbrBackground = 0
    wc.lpszMenuName = None
    wc.lpszClassName = class_name

    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        # ERROR_CLASS_ALREADY_EXISTS == 1410 is fine.
        if err not in (0, 1410):
            _log(f"RegisterClassW failed for {class_name!r} (err={err})")
            return False
    return True


def _create_layered_window(class_name: str, vs: _VScreen) -> int:
    """Create a layered/transparent/topmost/noactivate/tool popup window."""
    user32 = ctypes.windll.user32
    hinst = _module_handle()

    ex_style = (
        WS_EX_LAYERED
        | WS_EX_TRANSPARENT
        | WS_EX_TOPMOST
        | WS_EX_NOACTIVATE
        | WS_EX_TOOLWINDOW
    )
    style = WS_POPUP

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    hwnd = user32.CreateWindowExW(
        ex_style,
        class_name,
        None,
        style,
        vs.x,
        vs.y,
        vs.w,
        vs.h,
        None,
        None,
        hinst,
        None,
    )
    if not hwnd:
        _log("CreateWindowExW returned NULL")
        return 0
    return int(hwnd)


def _set_exclude_from_capture(hwnd: int, exclude: bool) -> None:
    """Apply SetWindowDisplayAffinity. Best-effort; logs on failure."""
    try:
        user32 = ctypes.windll.user32
        affinity = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
        ok = user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(affinity))
        if not ok and exclude:
            _log("SetWindowDisplayAffinity(EXCLUDEFROMCAPTURE) failed")
    except Exception as exc:
        _log(f"SetWindowDisplayAffinity error: {exc!r}")


def _declare_paint_prototypes(user32, gdi32) -> None:
    """Declare restype/argtypes for every GDI/user32 call used while painting.

    Without these, ctypes assumes ``c_int`` results and arguments, which both
    truncates returned 64-bit handles and overflows when a pointer-sized handle
    object is passed positionally. Declared once, idempotent.
    """
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(POINT),
        ctypes.POINTER(SIZE),
        wintypes.HDC,
        ctypes.POINTER(POINT),
        wintypes.DWORD,
        ctypes.POINTER(BLENDFUNCTION),
        wintypes.DWORD,
    ]

    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]


def _paint_layered(win: _LayeredWindow, img: "PIL.Image.Image") -> bool:
    """Paint ``img`` (top-down RGBA) into ``win`` via UpdateLayeredWindow.

    Builds a bottom-up 32-bpp DIB with premultiplied BGRA, then blits it with
    AC_SRC_OVER/AC_SRC_ALPHA. The window is positioned at ``(win.x, win.y)`` with
    size ``img.size``.

    Returns:
        ``True`` on success.
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    w, h = img.size
    bgra = _image_to_premultiplied_bgra_bottomup(img)

    # Declare pointer-sized restypes/argtypes so 64-bit handles are neither
    # truncated nor overflow during marshalling.
    _declare_paint_prototypes(user32, gdi32)

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        _log("GetDC(None) failed")
        return False
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not mem_dc:
        user32.ReleaseDC(None, screen_dc)
        _log("CreateCompatibleDC failed")
        return False

    hbmp = 0
    old_bmp = 0
    try:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = h  # positive -> bottom-up DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            mem_dc,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
            ctypes.byref(bits_ptr),
            None,
            0,
        )
        if not hbmp or not bits_ptr:
            _log("CreateDIBSection failed")
            return False

        ctypes.memmove(bits_ptr, bgra, len(bgra))

        old_bmp = gdi32.SelectObject(mem_dc, hbmp)

        src_pos = POINT(0, 0)
        dst_pos = POINT(win.x, win.y)
        size = SIZE(w, h)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        ok = user32.UpdateLayeredWindow(
            win.hwnd,
            screen_dc,
            ctypes.byref(dst_pos),
            ctypes.byref(size),
            mem_dc,
            ctypes.byref(src_pos),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
        if not ok:
            _log("UpdateLayeredWindow failed")
            return False
        win.w, win.h = w, h
        return True
    finally:
        try:
            if old_bmp:
                gdi32.SelectObject(mem_dc, old_bmp)
            if hbmp:
                gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
        except Exception:
            pass


def _move_layered(win: _LayeredWindow, x: int, y: int) -> None:
    """Move a layered window without changing its bitmap (SetWindowPos)."""
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos(
            wintypes.HWND(win.hwnd),
            wintypes.HWND(HWND_TOPMOST),
            int(x),
            int(y),
            0,
            0,
            SWP_NOSIZE | SWP_NOACTIVATE,
        )
        win.x, win.y = int(x), int(y)
    except Exception as exc:
        _log(f"SetWindowPos move failed: {exc!r}")


def _destroy_window(hwnd: int) -> None:
    """Destroy a window. Best-effort."""
    if not hwnd:
        return
    try:
        ctypes.windll.user32.DestroyWindow(wintypes.HWND(hwnd))
    except Exception as exc:
        _log(f"DestroyWindow failed: {exc!r}")


# ---------------------------------------------------------------------------
# Overlay thread: message pump + lifecycle
# ---------------------------------------------------------------------------

# We must keep a strong reference to the WNDPROC callback for the lifetime of
# the windows or the GC will collect it and Windows will call freed memory.
_wndproc_holder: "WNDPROC | None" = None
_GLOW_CLASS = "CCCOverlayGlow"
_PILL_CLASS = "CCCOverlayPill"


def _default_wndproc(hwnd, msg, wparam, lparam):
    """Minimal WNDPROC: default-process everything.

    ``DefWindowProcW`` must have its ``restype``/``argtypes`` declared (done once
    in :func:`_declare_defwindowproc`), otherwise the call faults during window
    creation (``WM_NCCREATE``) and CreateWindowExW fails with
    ERROR_MOD_NOT_FOUND.
    """
    try:
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    except Exception:
        return 0


def _declare_defwindowproc() -> None:
    """Declare ``DefWindowProcW`` prototypes once (idempotent)."""
    user32 = ctypes.windll.user32
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]


def _thread_main() -> None:
    """Entry point for the overlay daemon thread: build windows, pump, clean up."""
    global _wndproc_holder

    glow = _LayeredWindow()
    pill = _LayeredWindow()

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Record the Win32 thread id so stop() can PostThreadMessage us.
        _state.thread_id = int(kernel32.GetCurrentThreadId())

        # Declare DefWindowProcW prototypes before any window is created.
        _declare_defwindowproc()

        # Register the window class once (callback kept alive in module global).
        if _wndproc_holder is None:
            _wndproc_holder = WNDPROC(_default_wndproc)
        _register_class(_GLOW_CLASS, _wndproc_holder)
        _register_class(_PILL_CLASS, _wndproc_holder)

        # Glow + pill live on the controlling window's monitor only (other
        # displays stay untouched — official-computer-use parity).
        vs = _controlling_screen()

        # ---- Glow window -------------------------------------------------
        glow.hwnd = _create_layered_window(_GLOW_CLASS, vs)
        if glow.hwnd:
            glow.x, glow.y, glow.w, glow.h = vs.x, vs.y, vs.w, vs.h
            _set_exclude_from_capture(glow.hwnd, _state.exclude_from_capture)
            try:
                band = _state.band_frac * min(vs.w, vs.h)
                img = _glow_image(vs.w, vs.h, _state.color, _state.max_alpha, band)
                _paint_layered(glow, img)
                user32.ShowWindow(wintypes.HWND(glow.hwnd), SW_SHOWNOACTIVATE)
            except Exception as exc:
                _log(f"glow paint failed: {exc!r}")
        else:
            _log("glow window not created")

        # ---- Pill window (created up front, animated below) --------------
        pill_imgs_ready = False
        if _state.show_pill:
            try:
                ps = vs  # same monitor as the glow (the controlling window's)
                pill.hwnd = _create_layered_window(_PILL_CLASS, ps)
                if pill.hwnd:
                    _set_exclude_from_capture(pill.hwnd, _state.exclude_from_capture)
                    base = _pill_image(_state.pill_text, scale=1.0)
                    px = ps.x + (ps.w - base.width) // 2
                    py = ps.y + (ps.h - base.height) // 2  # dead center
                    pill.x, pill.y = px, py
                    if _paint_layered(pill, base):
                        user32.ShowWindow(wintypes.HWND(pill.hwnd), SW_SHOWNOACTIVATE)
                        pill_imgs_ready = True
            except Exception as exc:
                _log(f"pill setup failed: {exc!r}")

        # Signal start() that windows are up.
        try:
            _state.ready.set()
        except Exception:
            pass

        # ---- Pill animation scheduling -----------------------------------
        # We drive the pill purely from time within the pump loop (no blocking).
        pill_anim = _PillAnim(pill, primary=vs) if pill_imgs_ready else None
        if pill_anim is not None:
            pill_anim.start(_state.pill_text)

        # ---- Message pump -------------------------------------------------
        _pump(user32, pill_anim)

    except Exception as exc:
        _log(f"overlay thread crashed: {exc!r}")
        try:
            _state.ready.set()
        except Exception:
            pass
    finally:
        # Tear down windows on the same thread that created them.
        _destroy_window(pill.hwnd)
        _destroy_window(glow.hwnd)
        with _lock:
            _state.running = False


class _PillAnim:
    """Time-driven pill lifecycle: hold, then shrink + fly to top-right."""

    HOLD_S = 5.0
    ANIM_S = 0.6

    def __init__(self, win: _LayeredWindow, primary: _VScreen):
        self.win = win
        self.primary = primary
        self.text = ""
        self.t0 = 0.0
        self.done = False
        # Anchor: starting center of the pill, and the top-right target.
        self.start_cx = win.x + win.w / 2.0
        self.start_cy = win.y + win.h / 2.0
        margin = 12
        self.target_cx = primary.x + primary.w - margin
        self.target_cy = primary.y + margin
        self.last_scale = 1.0

    def start(self, text: str) -> None:
        self.text = text
        self.t0 = time.monotonic()

    def tick(self) -> None:
        """Advance the animation; cheap no-op once done."""
        if self.done or not self.win.hwnd:
            return
        elapsed = time.monotonic() - self.t0
        if elapsed < self.HOLD_S:
            return
        p = (elapsed - self.HOLD_S) / self.ANIM_S
        if p >= 1.0:
            # Hide and finish.
            try:
                ctypes.windll.user32.ShowWindow(wintypes.HWND(self.win.hwnd), SW_HIDE)
            except Exception:
                pass
            self.done = True
            return
        # Ease-in for a snappy fly-away.
        ease = p * p
        scale = 1.0 - 0.8 * ease  # 1.0 -> 0.2
        # Repaint the pill at the new scale, then position so its center moves
        # from start center toward the top-right target.
        try:
            img = _pill_image(self.text, scale=scale)
            cx = self.start_cx + (self.target_cx - self.start_cx) * ease
            cy = self.start_cy + (self.target_cy - self.start_cy) * ease
            self.win.x = int(cx - img.width / 2.0)
            self.win.y = int(cy - img.height / 2.0)
            _paint_layered(self.win, img)
        except Exception as exc:
            _log(f"pill anim tick failed: {exc!r}")
            self.done = True


def _pump(user32, pill_anim: "_PillAnim | None") -> None:
    """Run the message loop until WM_OVERLAY_QUIT / WM_QUIT, ticking the pill.

    Uses ``MsgWaitForMultipleObjects`` so we wake on input OR a ~16ms timeout to
    drive the pill animation without busy-spinning.
    """
    msg = wintypes.MSG()
    PM_REMOVE = 0x0001
    QS_ALLINPUT = 0x04FF
    WM_QUIT = 0x0012

    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]

    while True:
        if not _state.running:
            break

        # Drain pending messages.
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message in (WM_QUIT, WM_OVERLAY_QUIT):
                return
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Advance animation.
        if pill_anim is not None:
            pill_anim.tick()

        # Sleep up to ~16ms, waking early on any input/posted message.
        try:
            user32.MsgWaitForMultipleObjectsEx(
                0, None, wintypes.DWORD(16), wintypes.DWORD(QS_ALLINPUT), 0
            )
        except Exception:
            time.sleep(0.016)


# ---------------------------------------------------------------------------
# CLI for human/agent visual verification
# ---------------------------------------------------------------------------


def _main(argv: "list[str] | None" = None) -> int:
    """CLI: ``--preview SECONDS [--no-exclude] [--no-pill]``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m computer_use_omni.overlay",
        description="Preview the computer-use overlay glow / pill.",
    )
    parser.add_argument(
        "--preview",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Show the overlay for SECONDS, then stop (default: 2).",
    )
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="Do NOT exclude from capture (so screenshots SEE the glow).",
    )
    parser.add_argument(
        "--no-pill",
        action="store_true",
        help="Hide the centered announcement pill.",
    )
    args = parser.parse_args(argv)

    seconds = max(0.1, float(args.preview))
    _log(
        f"preview: {seconds}s exclude={not args.no_exclude} pill={not args.no_pill}"
    )

    # The MCP server makes itself Per-Monitor-V2 DPI-aware at startup; mirror
    # that here so the standalone preview covers the true physical resolution.
    try:
        from . import screen

        screen.set_dpi_awareness()
    except Exception as exc:
        _log(f"set_dpi_awareness failed (continuing): {exc!r}")

    start(
        exclude_from_capture=not args.no_exclude,
        show_pill=not args.no_pill,
    )
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        stop()
    _log("preview done")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
