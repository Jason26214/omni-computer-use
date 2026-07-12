# computer-use-omni

A faithful **1:1 replica of Anthropic's official `computer-use` tool surface** — shipped as a plain **MCP server** for Windows. It lets the **Claude Code CLI** (or any MCP client) drive the Windows desktop the way Claude Desktop's built-in computer-use does: screenshots, mouse / keyboard / scroll / drag / batch input, clipboard, multi-monitor, and application launch.

Same tool names, same parameters, same coordinate semantics as the desktop tool — an agent that already knows Anthropic's computer-use works here unchanged. Built and verified against Claude Desktop's own `computer-use` as the ground-truth oracle.

> Vision-and-coordinate based, like the official tool (rather than a UI-tree / accessibility approach) — for when you want Anthropic's computer-use paradigm on a Windows CLI.

## Install

Requires Windows and Python 3.11+ (uv / uvx fetch Python for you).

Run standalone:

```powershell
uvx computer-use-omni
```

Add to the Claude Code CLI:

```powershell
claude mcp add computer-use-omni -s user -- uvx computer-use-omni
claude mcp list   # expect: computer-use-omni … ✓ Connected
```

Or in Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "computer-use-omni": { "command": "uvx", "args": ["computer-use-omni"] }
  }
}
```

## Example

On a multi-monitor rig every screenshot self-describes — it names the monitor it captured and what else is attached, so the agent never loses track of which screen it is looking at:

```
This screenshot was taken on monitor "\\.\DISPLAY2" (2560x1600, primary).
Other attached monitors: "\\.\DISPLAY1" (2560x1440). Use switch_display to
capture a different monitor.
```

`open_application` reports what actually happened instead of fire-and-forget:

```
Opened "Calculator".                                  # a real window appeared
launched "…" but its process exited within ~2s        # crashed on startup — reported, not faked
  without showing a window …
```

## Status

- **29 tools** — 27 matching Claude Desktop's computer-use, plus `deactivate` and `display_overview`; a dev `reload` tool makes **30** when `COMPUTER_USE_DEV=on`.
- **16-scenario end-to-end suite** under `tests/` (`scen_*.json`), each run through a fresh MCP process.
- Built and verified on **Windows 11** (2560×1600 @ 150% DPI) against Claude Desktop's computer-use as the oracle; the screenshot downscale matches CDC's ~1.2 MP within rounding.
- **In daily use since 2026-06.**

## Tools

**Matching Anthropic's computer-use (27):** `request_access`, `list_granted_applications`, `request_teach_access`, `screenshot`, `zoom`, `switch_display`, `cursor_position`, `mouse_move`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`, `left_mouse_down`, `left_mouse_up`, `scroll`, `key`, `hold_key`, `type`, `wait`, `open_application`, `read_clipboard`, `write_clipboard`, `computer_batch`, `teach_step`, `teach_batch`.

**omni-specific (2):**

- `deactivate` — end the session without killing the process (glow off, controlling window restored, grants revoked); the counterpart to `request_access`.
- `display_overview` — one composite, labeled image of all monitors laid out per the virtual-desktop arrangement (an orientation aid for "which screen is that window on?"; not a click surface).

All click / move / scroll / drag / zoom coordinates are in the **image-pixel space of the most recent screenshot**; the server maps them back to physical pixels.

## How it works (the parts worth reading)

**Faithful desktop visuals.** While a session is active the server reproduces Claude Desktop's on-screen affordances, pixel-calibrated from reference captures: a static orange edge glow, a centered "Claude is using your computer" pill that flies to the corner, and the controlling window (the Windows Terminal running the CLI, or the Claude Desktop window itself) shrunk flush to the top-right and **parked off-screen during each capture** so screenshots show the true desktop with no black box. Click-through is kind-aware: a non-layered terminal drops to the bottom of the z-order for each synthetic click; the layered Claude Desktop window gets `WS_EX_TRANSPARENT` (the desktop tool's own approach) so clicks pass through to whatever is beneath — the window itself never moves, so the user's real mouse is unaffected.

**Keyboard self-harm guard.** Synthetic keystrokes go to whatever holds OS focus. If the controlling window (the Claude window, or the hosting terminal) is frontmost, `type` / `key` are **blocked** — otherwise the text would land in the agent's own conversation, or run as a shell command with a trailing Return. The guard is unconditional and identifies the control surface by window identity and owning process, while still leaving a second, unrelated terminal window a legitimate target. Mouse actions are exempt (a click carries its own coordinate).

**Multi-monitor.** Screenshots carry an event-driven note naming the captured monitor and flagging when it changed; `open_application` warns when a window opened on a different monitor than captures currently target — precisely, by monitor name, when it has the window handle; `display_overview` returns the all-screens map. The glow and shrink land on the controlling window's own monitor, leaving other displays untouched.

**Honest launching & self-heal.** `open_application` polls for a real window and distinguishes *opened* / *running-no-window-yet* / *crashed-on-startup* / *nothing-launched* instead of always reporting success. A force-killed session's shrunk terminal is restored on the next start from a small state file (guarded against window-handle reuse).

**Hot-reload (dev).** Set `COMPUTER_USE_DEV=on` to add a `reload` tool that `importlib.reload`s the logic modules in-process, so edited code takes effect without restarting the session — handy while developing automation against the server. It is off by default (the clean 29-tool surface).

See **[SPEC.md](SPEC.md)** for the authoritative, tool-by-tool contract and the module architecture.

## Differences from the desktop tool (by design)

The built-in computer use grants apps from the list of *installed* applications and applies a tiered model: browsers are visible but read-only, terminals and IDEs are click-only — no keystrokes, by architecture. Sensible defaults for general desktop use, and they close off the workflow this server was built for: letting the agent launch the app you are *currently building* — a loose `.exe` no install list knows about — click through it, type into it, verify behavior, then go back to the IDE and edit code.

omni grants every approved app at `tier:"full"` — IDEs, terminals, and dev builds included (`open_application` accepts a full `.exe` path). Full power, your responsibility.

A CLI has no permission GUI, so `request_access` **auto-grants** resolvable apps at `tier:"full"` and returns the same JSON shape; foreground gating is permissive by default (it only errors on an empty allowlist). Masking of non-allowlisted windows defaults off (the rect-based masker over-masks). Teach mode is a stub — it executes the step's actions and returns a screenshot, but there is no fullscreen tooltip overlay (a desktop-app feature). Each of these is controlled by the env vars below.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMPUTER_USE_MAX_PIXELS` | `1200000` | Max pixels in a downscaled screenshot (≈ 1.2 MP). |
| `COMPUTER_USE_MASKING` | `off` | Mask non-allowlisted app windows in screenshots. |
| `COMPUTER_USE_ENFORCE_FOREGROUND` | `off` | Block input when the frontmost app isn't allowlisted. |
| `COMPUTER_USE_AUTOGRANT` | `on` | Auto-grant resolvable apps on `request_access`. |
| `COMPUTER_USE_DEV` | `off` | Register the developer `reload` hot-reload tool (on → 30 tools). |
| `COMPUTER_USE_LOG_DIR` | `%LOCALAPPDATA%\computer-use-omni\logs` | Directory for the rotating `mcp.log` (tool calls + tracebacks). |
| `COMPUTER_USE_GLOW` | `on` | Static orange edge glow while a session is active. |
| `COMPUTER_USE_SHRINK_TERMINAL` | `on` | Shrink the controlling window to the top-right corner. |
| `COMPUTER_USE_HIDE_CONTROLLING` | `on` | Park the controlling window off-screen during captures. |
| `COMPUTER_USE_PILL` | `on` | Centered "Claude is using your computer" pill. |
| `COMPUTER_USE_GLOW_COLOR` | `217,119,87` | Glow / pill color (`#D97757`). |
| `COMPUTER_USE_GLOW_ALPHA` | `0.4` | Peak glow opacity at the very edge. |
| `COMPUTER_USE_GLOW_BAND` | `0.05` | Glow band width as a fraction of the smaller screen dimension. |
| `COMPUTER_USE_GLOW_EXCLUDE` | `on` | Exclude the glow / pill from captures. |

## Gotchas (real Windows behavior)

- **IME affects `key` / `hold_key`, not `type`.** `type` injects Unicode directly and bypasses the input method (CJK and emoji work in any layout). `key` / `hold_key` send virtual-key codes that pass **through** the active IME — so with a Chinese IME active, sending the letter `a` opens a pinyin candidate list instead of typing `a`. Use `type` for text; switch the IME to English for letter shortcuts.
- **Elevated apps** (Task Manager, UAC prompts, admin installers) can't be driven — Windows UIPI blocks input from a non-elevated process. Same limitation as the desktop tool.

## Tests

`tests/` holds a 16-scenario end-to-end suite (`scen_*.json`) plus the driver that launches a fresh server and runs a scenario's JSON list of tool calls, interleaving the returned images:

```powershell
uv run python tests/drive.py tests/scen_calc.json     # compute 7×8 by clicks, screenshot, verify
```

`tests/dev/` holds the one-off Win32 probes written during development (glow/pill sampling, z-order and capture-affinity experiments).

## Tech stack

Python ≥ 3.11, packaged with [uv], src layout, hatchling. [`mcp`] Python SDK (FastMCP) over stdio; [`mss`] for capture; [`pillow`] for imaging; [`pywin32`] + `ctypes` for DPI awareness, window / clipboard / foreground access, and raw Win32 `SendInput` mouse/keyboard injection.

## License

MIT © Jason26214

[uv]: https://github.com/astral-sh/uv
[`mcp`]: https://github.com/modelcontextprotocol/python-sdk
[`mss`]: https://github.com/BoboTiG/python-mss
[`pillow`]: https://python-pillow.org/
[`pywin32`]: https://github.com/mhammond/pywin32

<!-- mcp-name: io.github.Jason26214/computer-use-omni -->
