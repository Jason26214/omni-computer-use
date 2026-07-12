"""Entry point for ``python -m computer_use_omni`` and the console script.

Sets per-monitor DPI awareness as early as possible (before any capture or
coordinate math happens), then starts the FastMCP server over stdio.
"""

from __future__ import annotations


def main() -> None:
    """Set DPI awareness and run the MCP server over stdio."""
    # Establish DPI awareness before importing/using anything that measures the
    # screen. Guard against failure so a missing/older OS API never crashes boot.
    try:
        from . import screen

        screen.set_dpi_awareness()
    except Exception:
        # DPI awareness is best-effort; proceed even if it could not be set.
        pass

    from . import server

    try:
        server.app.run()
    finally:
        # Always tear down the CDC-style visuals (stop the overlay, restore the
        # terminal) on exit, even if app.run() raised. Idempotent with the
        # atexit handler registered on session activation.
        try:
            server._cleanup_session()
        except Exception:
            pass


if __name__ == "__main__":
    main()
