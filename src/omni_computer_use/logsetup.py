"""Persistent rotating file log for omni-computer-use.

So that when something fails during normal use, the tool call + full traceback
are recorded for later debugging (the user can just say "it broke" and the log
has the details).

Writes to ``%LOCALAPPDATA%\\omni-computer-use\\logs\\mcp.log`` by default — a
user-writable per-app directory that works for pip / uvx installs too (never
inside site-packages) — override with the ``COMPUTER_USE_LOG_DIR`` environment
variable. Setup is idempotent and never raises — logging must not be able to
break the server.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "omni_computer_use"
_configured = False
_log_path: str | None = None


def _default_log_dir() -> str:
    env = os.environ.get("COMPUTER_USE_LOG_DIR")
    if env:
        return env
    # A user-writable per-app location — works whether the package is installed
    # via pip / uvx (no source tree; must NOT write into site-packages) or run
    # from a source checkout. Windows: %LOCALAPPDATA%\omni-computer-use\logs.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return os.path.join(base, "omni-computer-use", "logs")
    # Non-Windows / no env: fall back to the OS temp dir.
    import tempfile

    return os.path.join(tempfile.gettempdir(), "omni-computer-use", "logs")


def setup() -> str | None:
    """Configure the rotating file logger once. Returns the log path (or None)."""
    global _configured, _log_path
    if _configured:
        return _log_path
    _configured = True
    try:
        log_dir = _default_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "mcp.log")
        lg = logging.getLogger(_LOGGER_NAME)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
            fh = RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            lg.addHandler(fh)
        _log_path = path
        return path
    except Exception as exc:  # logging must never break the server
        try:
            print(f"[logsetup] setup failed: {exc!r}", file=sys.stderr, flush=True)
        except Exception:
            pass
        return None


def logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def get_log_path() -> str | None:
    return _log_path


def info(msg: str) -> None:
    """Log an info line. Never raises."""
    try:
        logger().info(msg)
    except Exception:
        pass


def exception(msg: str) -> None:
    """Log a message plus the current exception traceback. Never raises."""
    try:
        logger().exception(msg)
    except Exception:
        pass
