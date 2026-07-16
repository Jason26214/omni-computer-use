"""Screen capture, DPI awareness, monitor enumeration and coordinate scaling.

This module owns the relationship between three coordinate spaces:

* **Physical / virtual-desktop pixels** — the real framebuffer pixels that
  ``mss`` captures and that the Win32 input layer ultimately targets.
* **Image pixels** — the (possibly downscaled) screenshot space that tool
  callers see and pass coordinates in.
* **Logical pixels** — DPI-scaled OS coordinates (not used for I/O here; the
  process runs Per-Monitor-V2 DPI-aware so Win32 geometry APIs report physical
  pixels).

The most recent capture's :class:`ScaleInfo` is cached so that mouse/keyboard
tools can map image-space coordinates back to physical pixels.

All stubs raise :class:`NotImplementedError`; only the public contract
(signatures, dataclasses, docstrings) is defined here.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import PIL.Image

# Module state -------------------------------------------------------------

#: Cached :class:`ScaleInfo` from the most recent :func:`capture`.
_last_scale: "ScaleInfo | None" = None

#: Monitor used by the most recent :func:`capture` (for screenshot notes).
_last_monitor: "Monitor | None" = None

#: Name of the explicitly selected monitor, or ``None`` for automatic selection.
_selected_name: str | None = None

#: Thread-local ``mss.mss`` instances (mss objects are not thread-safe and must
#: be created on the thread that uses them).
_mss_local = threading.local()


def _get_sct():
    """Return a thread-local ``mss.mss`` instance, creating it on first use."""
    sct = getattr(_mss_local, "sct", None)
    if sct is None:
        import mss

        sct = mss.mss()
        _mss_local.sct = sct
    return sct


def set_dpi_awareness() -> None:
    """Make this process Per-Monitor-V2 DPI aware.

    Calls ``SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)``
    (the magic value ``-4``) via ``user32``. Falls back to the older
    ``shcore.SetProcessDpiAwareness`` and finally ``user32.SetProcessDPIAware``
    on systems where the newer entry points are unavailable. Best-effort: must
    never raise to the caller (the entry point guards this anyway).
    """
    import ctypes

    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
    try:
        user32 = ctypes.windll.user32
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass

    # Fallback: shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE == 2)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    # Last resort: system-DPI awareness.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass
class Monitor:
    """A physical monitor in virtual-desktop (physical-pixel) coordinates.

    Attributes:
        name: Device name, e.g. ``\\\\.\\DISPLAY1``.
        index: Stable index into :func:`list_monitors` (0-based).
        x: Left edge of the monitor in virtual-desktop physical pixels.
        y: Top edge of the monitor in virtual-desktop physical pixels.
        width: Monitor width in physical pixels.
        height: Monitor height in physical pixels.
        primary: ``True`` if this is the primary monitor.
    """

    name: str
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool


@dataclass
class ScaleInfo:
    """Mapping between image-pixel space and physical-pixel space for one capture.

    A coordinate ``(x, y)`` in image space maps to physical
    ``(origin_x + round(x * scale_x), origin_y + round(y * scale_y))``.

    Attributes:
        image_w: Width of the returned (downscaled) image in pixels.
        image_h: Height of the returned (downscaled) image in pixels.
        phys_w: Width of the captured monitor region in physical pixels.
        phys_h: Height of the captured monitor region in physical pixels.
        origin_x: Physical x of the captured region's top-left (monitor origin).
        origin_y: Physical y of the captured region's top-left (monitor origin).
        scale_x: phys_w / image_w (physical pixels per image pixel, x axis).
        scale_y: phys_h / image_h (physical pixels per image pixel, y axis).
    """

    image_w: int
    image_h: int
    phys_w: int
    phys_h: int
    origin_x: int
    origin_y: int
    scale_x: float
    scale_y: float


def list_monitors() -> list[Monitor]:
    """Enumerate all physical monitors in virtual-desktop coordinates.

    Returns:
        A list of :class:`Monitor` ordered by index, with physical-pixel
        geometry. The primary monitor has ``primary=True``.
    """
    sct = _get_sct()

    # mss.monitors[0] is the union "virtual screen"; [1:] are the real monitors
    # in physical pixels (the process is Per-Monitor-V2 DPI aware).
    raw = sct.monitors[1:]

    # Best-effort device names via EnumDisplayDevices; fall back to DISPLAY<N>.
    names = _device_names(len(raw))

    monitors: list[Monitor] = []
    for i, m in enumerate(raw):
        x, y = int(m["left"]), int(m["top"])
        w, h = int(m["width"]), int(m["height"])
        # Primary monitor has its top-left at the virtual-desktop origin (0, 0).
        primary = x == 0 and y == 0
        monitors.append(
            Monitor(
                name=names[i],
                index=i,
                x=x,
                y=y,
                width=w,
                height=h,
                primary=primary,
            )
        )

    # Guarantee exactly one primary even on odd layouts.
    if monitors and not any(m.primary for m in monitors):
        monitors[0].primary = True

    return monitors


def _device_names(count: int) -> list[str]:
    """Best-effort device names (``\\\\.\\DISPLAY<N>``) for ``count`` monitors."""
    names: list[str] = []
    try:
        import ctypes
        from ctypes import wintypes

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        user32 = ctypes.windll.user32
        i = 0
        while len(names) < count:
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            DISPLAY_DEVICE_ACTIVE = 0x00000001
            if dd.StateFlags & DISPLAY_DEVICE_ACTIVE:
                names.append(dd.DeviceName)
            i += 1
    except Exception:
        names = []

    # Pad / fall back to synthetic names so the list always has `count` entries.
    while len(names) < count:
        names.append(f"\\\\.\\DISPLAY{len(names) + 1}")
    return names[:count]


def select_monitor(name: str | None) -> None:
    """Select the active monitor for subsequent captures.

    Args:
        name: ``None`` or ``'auto'`` selects the monitor automatically (e.g. the
            one containing the foreground window or the primary). Otherwise the
            monitor whose :attr:`Monitor.name` matches ``name`` is selected.

    Raises:
        ValueError: If ``name`` does not match any known monitor.
    """
    global _selected_name

    if name is None or name.lower() == "auto":
        _selected_name = None
        return

    monitors = list_monitors()
    for m in monitors:
        if m.name == name or m.name.lower() == name.lower():
            _selected_name = m.name
            return

    known = ", ".join(m.name for m in monitors)
    raise ValueError(f"unknown monitor {name!r}; available: {known}")


def current_monitor() -> Monitor:
    """Return the currently selected active monitor.

    Returns:
        The :class:`Monitor` that :func:`capture` will read from.
    """
    monitors = list_monitors()
    if not monitors:
        raise RuntimeError("no monitors detected")

    if _selected_name is not None:
        for m in monitors:
            if m.name == _selected_name:
                return m
        # Selection went stale (monitor unplugged); fall through to automatic.

    # Automatic: prefer the monitor under the foreground window, else primary.
    auto = _auto_monitor(monitors)
    if auto is not None:
        return auto

    for m in monitors:
        if m.primary:
            return m
    return monitors[0]


def _auto_monitor(monitors: list[Monitor]) -> Monitor | None:
    """Return the monitor containing the foreground window, if determinable."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        for m in monitors:
            if m.x <= cx < m.x + m.width and m.y <= cy < m.y + m.height:
                return m
    except Exception:
        return None
    return None


def capture(
    mask_rects: list[tuple[int, int, int, int]] | None = None,
) -> tuple["PIL.Image.Image", ScaleInfo]:
    """Capture the active monitor and return a downscaled image plus scale info.

    Grabs the active monitor's physical pixels via ``mss``. If ``mask_rects`` is
    given, each rectangle (physical coordinates ``(x0, y0, x1, y1)``) is filled
    with a solid color **before** downscaling. The image is then downscaled with
    LANCZOS resampling, preserving aspect ratio, so that
    ``image_w * image_h <= config.MAX_PIXELS``; the image is never upscaled.

    The resulting :class:`ScaleInfo` is cached as the last scale (see
    :func:`last_scale`) so input tools can map image coordinates to physical
    pixels.

    Args:
        mask_rects: Optional list of physical-pixel rectangles to blank out
            before downscaling.

    Returns:
        A tuple ``(image, scale_info)`` where ``image`` is a PIL image in image
        space and ``scale_info`` describes the image<->physical mapping.
    """
    global _last_scale, _last_monitor

    from PIL import Image

    mon = current_monitor()
    image = _grab_monitor(mon, mask_rects)
    phys_w, phys_h = image.width, image.height

    # Downscale preserving aspect ratio so image_w * image_h <= MAX_PIXELS.
    image_w, image_h = _fit_dimensions(phys_w, phys_h, config.MAX_PIXELS)
    if (image_w, image_h) != (phys_w, phys_h):
        image = image.resize((image_w, image_h), Image.LANCZOS)

    scale = ScaleInfo(
        image_w=image_w,
        image_h=image_h,
        phys_w=phys_w,
        phys_h=phys_h,
        origin_x=mon.x,
        origin_y=mon.y,
        scale_x=phys_w / image_w,
        scale_y=phys_h / image_h,
    )
    _last_scale = scale
    _last_monitor = mon
    return image, scale


def _grab_monitor(
    mon: Monitor, mask_rects: list[tuple[int, int, int, int]] | None = None
) -> "PIL.Image.Image":
    """Grab one monitor at full resolution and apply physical-coord masks."""
    from PIL import Image

    sct = _get_sct()
    raw = sct.grab(
        {"left": mon.x, "top": mon.y, "width": mon.width, "height": mon.height}
    )
    # mss returns BGRA; PIL can construct directly from the raw buffer.
    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    # Masks arrive in virtual-desktop physical coords; translate to this
    # monitor's local origin and clamp to its bounds.
    if mask_rects:
        from PIL import ImageDraw

        w, h = image.width, image.height
        draw = ImageDraw.Draw(image)
        for x0, y0, x1, y1 in mask_rects:
            lx0 = max(0, min(w, x0 - mon.x))
            ly0 = max(0, min(h, y0 - mon.y))
            lx1 = max(0, min(w, x1 - mon.x))
            ly1 = max(0, min(h, y1 - mon.y))
            if lx1 > lx0 and ly1 > ly0:
                draw.rectangle([lx0, ly0, lx1 - 1, ly1 - 1], fill=(0, 0, 0))
    return image


def last_capture_monitor() -> Monitor | None:
    """Return the monitor used by the most recent :func:`capture`, or None."""
    return _last_monitor


def describe_monitor(mon: Monitor) -> str:
    """Human/AI-friendly one-liner: ``"\\\\.\\DISPLAY2" (primary, 2560x1600)``."""
    tag = ", primary" if mon.primary else ""
    return f'"{mon.name}" ({mon.width}x{mon.height}{tag})'


def monitor_for_hwnd(hwnd: int) -> Monitor | None:
    """Return the :class:`Monitor` hosting ``hwnd``, or ``None``.

    Uses ``MonitorFromWindow`` + ``GetMonitorInfoW`` (physical pixels — the
    process is per-monitor DPI aware) and matches the rect against
    :func:`list_monitors` entries.
    """
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        for m in list_monitors():
            if (
                m.x == r.left
                and m.y == r.top
                and m.width == (r.right - r.left)
                and m.height == (r.bottom - r.top)
            ):
                return m
    except Exception:
        return None
    return None


def cross_monitor_hint(hwnd: int) -> str:
    """Hint appended to launch results when the window is on another monitor.

    Multi-monitor only (returns ``''`` on single-monitor systems). With a real
    ``hwnd`` the hint is precise (names the window's monitor) and empty when the
    window is already on the capture monitor — under 'auto' selection the next
    screenshot follows the freshly focused window, so no hint fires. Without an
    ``hwnd`` (unverifiable UWP/shell launches) a generic warning is returned,
    mirroring the official computer-use behavior.
    """
    try:
        if len(list_monitors()) < 2:
            return ""
        if hwnd:
            tm = monitor_for_hwnd(hwnd)
            cur = current_monitor()
            if tm is not None and tm.name != cur.name:
                return (
                    f" Note: its window is on monitor {describe_monitor(tm)}, "
                    f'while your captures are currently on "{cur.name}" — call '
                    f'switch_display("{tm.name}") to see it.'
                )
            return ""
        return (
            " If it is not visible in the next screenshot, it may have opened "
            "on a different monitor — use switch_display to check."
        )
    except Exception:
        return ""


def capture_overview(
    mask_rects: list[tuple[int, int, int, int]] | None = None,
) -> tuple["PIL.Image.Image", str]:
    """Composite ALL monitors into one image laid out per the virtual desktop.

    Orientation aid ("which screen is that window on?"): every monitor is
    grabbed, downscaled with a single uniform factor, and pasted at its scaled
    virtual-desktop position, so relative sizes and arrangement are truthful.
    Each panel gets a name label. Deliberately does NOT touch
    :func:`last_scale` — coordinates in the overview are not clickable.

    Returns:
        ``(image, note)`` where ``note`` spells out the layout and warns that
        the overview is not a click surface.
    """
    from PIL import Image, ImageDraw

    mons = list_monitors()
    if not mons:
        raise RuntimeError("no monitors detected")

    vx0 = min(m.x for m in mons)
    vy0 = min(m.y for m in mons)
    vx1 = max(m.x + m.width for m in mons)
    vy1 = max(m.y + m.height for m in mons)
    vw, vh = max(1, vx1 - vx0), max(1, vy1 - vy0)

    cw, ch = _fit_dimensions(vw, vh, config.MAX_PIXELS)
    f = min(cw / vw, ch / vh)

    canvas = Image.new("RGB", (max(1, int(vw * f)), max(1, int(vh * f))), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    for m in mons:
        img = _grab_monitor(m, mask_rects)
        tw = max(1, int(m.width * f))
        th = max(1, int(m.height * f))
        img = img.resize((tw, th), Image.LANCZOS)
        px = int((m.x - vx0) * f)
        py = int((m.y - vy0) * f)
        canvas.paste(img, (px, py))
        label = f"{m.name}  {m.width}x{m.height}" + ("  PRIMARY" if m.primary else "")
        draw.rectangle([px, py, px + 10 + 7 * len(label), py + 18], fill=(0, 0, 0))
        draw.text((px + 5, py + 3), label, fill=(255, 200, 80))

    note = (
        "Overview of all monitors, laid out as arranged on the virtual desktop: "
        + "; ".join(f"{describe_monitor(m)} at ({m.x},{m.y})" for m in mons)
        + ". Coordinates in this image are NOT clickable — pick a monitor, call "
        'switch_display("<name>"), then screenshot to interact.'
    )
    return canvas, note


def _fit_dimensions(w: int, h: int, max_pixels: int) -> tuple[int, int]:
    """Return AR-preserved ``(width, height)`` with ``width*height <= max_pixels``.

    Never upscales: if ``w*h`` already fits, the dimensions are returned
    unchanged. Uses ``floor`` on the scaled dimensions, which mathematically
    guarantees the pixel budget is respected (``floor(w*f)*floor(h*f) <=
    w*f*h*f == max_pixels``).
    """
    if w <= 0 or h <= 0:
        return max(1, w), max(1, h)
    if w * h <= max_pixels:
        return w, h
    f = math.sqrt(max_pixels / (w * h))
    return max(1, int(w * f)), max(1, int(h * f))


def last_scale() -> ScaleInfo | None:
    """Return the :class:`ScaleInfo` from the most recent :func:`capture`.

    Returns:
        The cached scale info, or ``None`` if no capture has happened yet.
    """
    return _last_scale


def image_to_physical(
    x: float, y: float, scale: ScaleInfo | None = None
) -> tuple[int, int]:
    """Map an image-space coordinate to a physical-pixel coordinate.

    Args:
        x: Image-space x coordinate.
        y: Image-space y coordinate.
        scale: Scale info to use; defaults to :func:`last_scale` when ``None``.

    Returns:
        ``(origin_x + round(x * scale_x), origin_y + round(y * scale_y))``.

    Raises:
        RuntimeError: If ``scale`` is ``None`` and no capture has happened yet.
    """
    if scale is None:
        scale = _last_scale
    if scale is None:
        raise RuntimeError(
            "no scale available; take a screenshot before mapping coordinates"
        )
    px = scale.origin_x + round(x * scale.scale_x)
    py = scale.origin_y + round(y * scale.scale_y)
    return px, py


def physical_to_image(
    px: int, py: int, scale: ScaleInfo | None = None
) -> tuple[int, int]:
    """Map a physical-pixel coordinate to an image-space coordinate.

    Inverse of :func:`image_to_physical`; used by ``cursor_position``.

    Args:
        px: Physical x coordinate.
        py: Physical y coordinate.
        scale: Scale info to use; defaults to :func:`last_scale` when ``None``.

    Returns:
        ``(round((px - origin_x) / scale_x), round((py - origin_y) / scale_y))``.

    Raises:
        RuntimeError: If ``scale`` is ``None`` and no capture has happened yet.
    """
    if scale is None:
        scale = _last_scale
    if scale is None:
        raise RuntimeError(
            "no scale available; take a screenshot before mapping coordinates"
        )
    ix = round((px - scale.origin_x) / scale.scale_x)
    iy = round((py - scale.origin_y) / scale.scale_y)
    return ix, iy
