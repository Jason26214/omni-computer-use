"""Session allowlist and permission logic (CDC-shaped payloads).

Tracks which applications the current session may drive, plus clipboard and
system-key-combo grant flags. In CCC autogrant mode, ``request_access`` resolves
each requested app and grants it automatically (tier ``'full'``); unresolved
names are reported as ``notInstalled``.

Return payloads mirror the shapes Claude Desktop's computer-use MCP emits so the
CLI experience matches 1:1.

The dataclass and method **signatures** are the public contract; method bodies
raise :class:`NotImplementedError`. ``ALLOWLIST`` is the module-level singleton.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Granted:
    """A single granted application entry.

    Attributes:
        display: Human-friendly display name.
        bundle_id: Synthetic stable identifier (CDC-style ``bundleId``).
        exe: Lowercased process basename used for foreground/allowlist matching.
        granted_at: Grant time in epoch milliseconds.
        tier: Access tier, e.g. ``'full'``, ``'click'``, ``'read'``.
    """

    display: str
    bundle_id: str
    exe: str
    granted_at: int
    tier: str


class Allowlist:
    """Mutable per-session allowlist of apps and capability flags."""

    apps: list[Granted]
    clipboard_read: bool
    clipboard_write: bool
    system_key_combos: bool

    def __init__(self) -> None:
        """Initialize an empty allowlist with all capability flags off."""
        self.apps = []
        self.clipboard_read = False
        self.clipboard_write = False
        self.system_key_combos = False

    def request_access(
        self,
        apps: list[str],
        reason: str,
        clipboardRead: bool = False,
        clipboardWrite: bool = False,
        systemKeyCombos: bool = False,
    ) -> dict:
        """Grant access to apps (autogrant) and merge capability flags.

        For each requested name, calls :func:`omni_computer_use.apps.resolve_app`.
        Resolved apps are added as :class:`Granted` with ``tier='full'`` and
        ``granted_at=epoch_ms()``; unresolved names are collected under
        ``notInstalled``. The clipboard / system-key-combo flags are OR-merged
        into the existing session flags.

        Args:
            apps: Requested application names.
            reason: Human-readable reason for the request (for logging/UX).
            clipboardRead: Request clipboard read capability.
            clipboardWrite: Request clipboard write capability.
            systemKeyCombos: Request system key-combo capability.

        Returns:
            A CDC-shaped dict::

                {
                    "granted": [
                        {"bundleId", "displayName", "grantedAt", "tier"}, ...
                    ],
                    "denied": [],
                    "notInstalled": {  # only when some names did not resolve
                        "apps": [{"requestedName", "didYouMean": []}, ...],
                        "guidance": str,
                    },
                    "screenshotFiltering": "mask" | "none",  # honest: reflects config.MASKING
                }
        """
        # Lazy import to avoid an import cycle (apps -> permissions is possible).
        from omni_computer_use import apps as _apps
        from omni_computer_use import config as _config

        granted_entries: list[dict] = []
        not_installed: list[dict] = []

        for name in apps:
            info = _apps.resolve_app(name)
            if info is None:
                not_installed.append({"requestedName": name, "didYouMean": []})
                continue

            now = epoch_ms()
            entry = Granted(
                display=info.display,
                bundle_id=info.bundle_id,
                exe=info.exe.lower(),
                granted_at=now,
                tier="full",
            )
            # Replace any existing grant for the same app rather than duplicate.
            self.apps = [g for g in self.apps if g.bundle_id != entry.bundle_id]
            self.apps.append(entry)
            granted_entries.append(
                {
                    "bundleId": entry.bundle_id,
                    "displayName": entry.display,
                    "grantedAt": entry.granted_at,
                    "tier": entry.tier,
                }
            )

        # OR-merge the capability flags into the existing session flags.
        self.clipboard_read = self.clipboard_read or bool(clipboardRead)
        self.clipboard_write = self.clipboard_write or bool(clipboardWrite)
        self.system_key_combos = self.system_key_combos or bool(systemKeyCombos)

        result: dict = {
            "granted": granted_entries,
            "denied": [],
            # Report honestly: "mask" only when masking is actually enabled;
            # "none" when screenshots are returned unfiltered (the default).
            "screenshotFiltering": "mask" if _config.MASKING else "none",
        }
        if not_installed:
            result["notInstalled"] = {
                "apps": not_installed,
                "guidance": (
                    "Some requested applications could not be found on this "
                    "machine. Check the spelling or install the application, "
                    "then call request_access again."
                ),
            }
        return result

    def list_granted(self) -> dict:
        """Return the current grants in CDC-shaped form.

        Returns:
            A dict::

                {
                    "allowedApps": [
                        {"bundleId", "displayName", "grantedAt", "tier"}, ...
                    ],
                    "grantFlags": {
                        "clipboardRead": bool,
                        "clipboardWrite": bool,
                        "systemKeyCombos": bool,
                    },
                }
        """
        return {
            "allowedApps": [
                {
                    "bundleId": g.bundle_id,
                    "displayName": g.display,
                    "grantedAt": g.granted_at,
                    "tier": g.tier,
                }
                for g in self.apps
            ],
            "grantFlags": {
                "clipboardRead": self.clipboard_read,
                "clipboardWrite": self.clipboard_write,
                "systemKeyCombos": self.system_key_combos,
            },
        }

    def allowed_exes(self) -> set[str]:
        """Return the set of allowlisted process basenames (lowercased).

        Returns:
            The lowercased ``exe`` of every granted app.
        """
        return {g.exe.lower() for g in self.apps if g.exe}

    def is_allowed(self, exe_basename: str) -> bool:
        """Return whether a process basename is allowlisted.

        Args:
            exe_basename: Lowercased process basename to test.

        Returns:
            ``True`` if the basename matches a granted app.
        """
        if not exe_basename:
            return False
        return exe_basename.lower() in self.allowed_exes()

    def is_app_allowed(self, info: "object") -> bool:
        """Return whether a resolved app (AppInfo) is granted in this session.

        Matches by ``bundle_id`` first (stable across UWP apps that have no known
        ``exe`` at grant time, e.g. Calculator/Settings), then falls back to the
        ``exe`` basename. This lets ``open_application`` launch a freshly granted
        UWP app even though its grant carries an empty ``exe``.

        Args:
            info: An :class:`omni_computer_use.apps.AppInfo`-like object exposing
                ``bundle_id`` and ``exe`` attributes.

        Returns:
            ``True`` if a granted app matches by bundle id or exe basename.
        """
        bundle_id = getattr(info, "bundle_id", "") or ""
        if bundle_id and any(g.bundle_id == bundle_id for g in self.apps):
            return True
        exe = (getattr(info, "exe", "") or "").lower()
        return self.is_allowed(exe)

    def is_empty(self) -> bool:
        """Return whether no apps have been granted yet.

        Returns:
            ``True`` if the allowlist contains no apps.
        """
        return len(self.apps) == 0

    def clear(self) -> None:
        """Revoke all grants and reset capability flags.

        Used by ``deactivate`` to end a computer-use session without killing the
        process: with an empty allowlist, input actions are gated off until a
        fresh :meth:`request_access`.
        """
        self.apps = []
        self.clipboard_read = False
        self.clipboard_write = False
        self.system_key_combos = False


#: Module-level singleton allowlist for the running server session.
ALLOWLIST = Allowlist()


def epoch_ms() -> int:
    """Return the current time in epoch milliseconds.

    Returns:
        ``int(time.time() * 1000)``.
    """
    return int(time.time() * 1000)
