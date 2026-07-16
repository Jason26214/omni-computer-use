# PROJECT: omni-computer-use — Windows computer-use MCP for Claude Code CLI

## GOAL
Replicate, on Windows, the EXACT tool surface and behavior of Claude Desktop's "computer-use" MCP, so the Claude Code CLI (CCC) can drive the desktop (screenshot + mouse/keyboard/scroll/drag/batch + clipboard + app launch + multi-monitor). Must be a faithful 1:1 replica of the tool names, params and semantics.

## TARGET MACHINE (measured ground truth — code MUST stay general, but tune defaults to these)
- Physical primary monitor: 2560x1600. Windows DPI scaling: 150% (logical 1707x1067).
- Claude Desktop downscales its screenshot to ~1388x868 (~1.20 megapixels), aspect-ratio preserved, captured from the PHYSICAL framebuffer.
- Click coordinates are in IMAGE-pixel space of the most recent screenshot; the server scales them back to physical pixels. Measured per-axis scale ~1.84 (= 2560/1388).
- Monitors: originally developed single-monitor; the dev rig now also has a secondary. Measured dual layout: primary = \\.\DISPLAY2 2560x1600 @(0,0) (laptop panel), secondary = \\.\DISPLAY1 2560x1440 @(2560,157) (to the RIGHT, offset down). NOTE the numbering/identity mismatch (DISPLAY1 is NOT the primary) — never assume; code must key off Monitor.primary and real geometry.

## TECH STACK (use exactly this)
- Python (>=3.11), packaged with uv. src layout. Package name: omni_computer_use. Package name (PyPI): omni-computer-use
- MCP framework: official 'mcp' Python SDK, FastMCP API (from mcp.server.fastmcp import FastMCP). Run over stdio.
- Screen capture: mss. Image: pillow. Window/clipboard/foreground: pywin32 (win32gui/win32process/win32api/win32clipboard) + ctypes for SendInput/DPI.
- Input injection: raw Win32 SendInput via ctypes (NOT pyautogui) for reliable absolute coords + Unicode typing.
- dependencies in pyproject: mcp, mss, pillow, pywin32
- Entry: python -m omni_computer_use  (so __main__.py defines main()). Also a console_script 'omni-computer-use'.
- The MCP will be launched by CCC like the user's existing servers: uvx omni-computer-use

## FILE MANIFEST (create ALL of these)
pyproject.toml
README.md
SPEC.md                  (copy of this spec, authoritative contract)
src/omni_computer_use/__init__.py
src/omni_computer_use/__main__.py   (main(): set DPI awareness, run FastMCP stdio)
src/omni_computer_use/config.py     (env-driven config: MAX_PIXELS default 1_200_000; MASKING default 'on'; ENFORCE_FOREGROUND default 'off'; CCC_AUTOGRANT default 'on')
src/omni_computer_use/screen.py     (DPI, monitors, capture, downscale, coordinate scaling, masking apply, multi-monitor notes/hints, display_overview compositing)
src/omni_computer_use/inputs.py     (SendInput mouse: move/click/drag/down/up/scroll, get_cursor_pos) — works in PHYSICAL/virtual-desktop coords
src/omni_computer_use/keymap.py     (key-name->VK map, chord parsing, aliases)
src/omni_computer_use/keyboard.py   (press_chord/hold/type_text/press_keys/release_keys via SendInput) — imports keymap
src/omni_computer_use/apps.py       (resolve_app, launch_and_focus, foreground_process, enumerate_windows, mask_rects_for)
src/omni_computer_use/permissions.py(Allowlist state, request_access logic, list_granted, is_allowed)
src/omni_computer_use/clipboard.py  (read_text/write_text)
src/omni_computer_use/server.py     (FastMCP app, register all 29 tools + dev reload, dispatch) — written in Integrate phase
src/omni_computer_use/batch.py      (computer_batch dispatcher + teach stubs) — written in Integrate phase
src/omni_computer_use/overlay.py    (CDC-style orange edge glow + centered pill; layered topmost click-through windows, WDA-excluded from captures)
src/omni_computer_use/terminal.py   (find/shrink the controlling window — Windows Terminal (CCC) OR the Claude Desktop window (CDC, Electron Chrome_WidgetWin_1/claude.exe, resolved by ancestry); off-screen-hide on capture; kind-aware click-through: z-order drop for the non-layered terminal, WS_EX_TRANSPARENT for the layered Claude window; crash-safety state + startup self-heal)
tests/mcp_client.py      (smoke client) — written in Boot test phase

## MODULE PUBLIC CONTRACT (exact signatures; stubs raise NotImplementedError)

### config.py
MAX_PIXELS: int            # default int(os.environ.get('COMPUTER_USE_MAX_PIXELS', 1_200_000))
MASKING: bool              # default os.environ.get('COMPUTER_USE_MASKING','on') != 'off'
ENFORCE_FOREGROUND: bool   # default os.environ.get('COMPUTER_USE_ENFORCE_FOREGROUND','off') == 'on'
AUTOGRANT: bool            # default os.environ.get('COMPUTER_USE_AUTOGRANT','on') != 'off'

### screen.py
def set_dpi_awareness() -> None        # SetProcessDpiAwarenessContext(-4) Per-Monitor-V2, fallback shcore/user32
@dataclass class Monitor: name:str; index:int; x:int; y:int; width:int; height:int; primary:bool   # x,y,width,height in PHYSICAL px
@dataclass class ScaleInfo: image_w:int; image_h:int; phys_w:int; phys_h:int; origin_x:int; origin_y:int; scale_x:float; scale_y:float
def list_monitors() -> list[Monitor]
def select_monitor(name: str | None) -> None     # 'auto'/None => automatic; else match Monitor.name
def current_monitor() -> Monitor
def capture(mask_rects: list[tuple[int,int,int,int]] | None = None) -> tuple["PIL.Image.Image", ScaleInfo]
    # capture active monitor PHYSICAL pixels via mss; if mask_rects given (PHYSICAL coords x0,y0,x1,y1) fill them solid BEFORE downscale; downscale preserving AR so image_w*image_h <= MAX_PIXELS, never upscale, LANCZOS; return (image, scaleinfo). Also store as last scale.
def last_scale() -> ScaleInfo | None
def image_to_physical(x: float, y: float, scale: ScaleInfo | None = None) -> tuple[int,int]   # uses last_scale if None: origin + round(x*scale_x), origin + round(y*scale_y)
def physical_to_image(px: int, py: int, scale: ScaleInfo | None = None) -> tuple[int,int]      # inverse, for cursor_position
def last_capture_monitor() -> Monitor | None      # monitor used by the most recent capture (feeds the screenshot note)
def describe_monitor(mon: Monitor) -> str         # '"\\.\DISPLAY2" (2560x1600, primary)'
def monitor_for_hwnd(hwnd: int) -> Monitor | None # MonitorFromWindow + GetMonitorInfoW rect matched against list_monitors
def cross_monitor_hint(hwnd: int) -> str          # multi-monitor launch hint: precise (names the window's monitor, empty when same/auto-followed) with an hwnd; generic warning without one; '' on single-monitor
def capture_overview(mask_rects=None) -> tuple["PIL.Image.Image", str]   # all monitors composited per virtual-desktop layout (uniform scale, labeled panels, <= MAX_PIXELS); does NOT touch last_scale; returns (image, not-clickable note)

### inputs.py  (PHYSICAL/virtual-desktop coordinates; uses SendInput absolute over virtual screen normalized 0..65535 with MOUSEEVENTF_VIRTUALDESK|ABSOLUTE)
def move_to(px:int, py:int) -> None
def click(px:int, py:int, button:str='left', count:int=1, modifiers:list[str]|None=None) -> None  # modifiers pressed via keyboard.press_keys/release_keys around the click
def mouse_down(px:int|None=None, py:int|None=None, button:str='left') -> None
def mouse_up(px:int|None=None, py:int|None=None, button:str='left') -> None
def drag(px0:int, py0:int, px1:int, py1:int, button:str='left') -> None
def scroll(px:int, py:int, direction:str, amount:int) -> None   # direction in up/down/left/right; amount = wheel ticks; move cursor there first then wheel
def get_cursor_pos() -> tuple[int,int]    # physical via GetCursorPos (process is DPI aware)

### keymap.py
KEY_MAP: dict[str,int]      # names -> Windows VK codes. Support xdotool-ish + common aliases (case-insensitive): return/enter->VK_RETURN, esc/escape, tab, space, backspace/bksp, delete/del, home,end,pageup/pgup,pagedown/pgdn, up/down/left/right, f1..f24, plus letters a-z digits 0-9, punctuation. Modifiers: ctrl/control, shift, alt/option, win/cmd/super/meta -> VK_LWIN.
MODIFIER_NAMES: set[str]
def normalize_key(name:str) -> str
def to_vk(name:str) -> int
def parse_chord(text:str) -> list[int]    # 'ctrl+shift+a' -> [VK_CONTROL, VK_SHIFT, ord('A')]; split on '+'; last is main key, earlier are modifiers; return VKs in press order

### keyboard.py  (imports keymap)
def press_keys(vks: list[int]) -> None              # key-down each (modifiers), via SendInput keybd
def release_keys(vks: list[int]) -> None            # key-up in reverse
def press_chord(text:str, repeat:int=1) -> None     # parse_chord; hold modifiers, tap main key repeat times, release
def hold(text:str, duration:float) -> None          # press chord down, sleep duration, release
def type_text(text:str) -> None                     # Unicode via SendInput KEYEVENTF_UNICODE (handle surrogate pairs); '\n' -> Return

### apps.py
@dataclass class AppInfo: display:str; exe:str; launch:str; bundle_id:str   # exe = expected foreground process basename lowercased (best-effort); launch = command/target; bundle_id = synthetic id
@dataclass class WinInfo: hwnd:int; rect:tuple[int,int,int,int]; exe:str; visible:bool   # rect PHYSICAL px
def resolve_app(name:str) -> AppInfo | None         # search Start Menu .lnk under ProgramData + AppData, and running processes; case-insensitive contains/best match; for UWP allow shell:AppsFolder
@dataclass class LaunchResult: hwnd:int; pid:int; exited:bool; kind:str   # kind='popen'|'shell-file'|'shell-bare'; exited=True only when a spawned pid died with no window (crash-on-startup)
def launch_and_focus(app: AppInfo) -> LaunchResult  # classic on-disk .exe -> subprocess.Popen with cwd=exe dir + DETACHED + DEVNULL stdio (cwd fixes Tauri/WebView2 relative-resource crashes; DEVNULL stops a console child corrupting JSON-RPC stdout); .lnk/moniker/UWP -> shell (os.startfile/ShellExecute, no pid). Poll ≤~2s for a focusable window (by our pid, else by exe) then SetForegroundWindow + AllowSetForegroundWindow + ShowWindow restore. Returns what actually happened so the caller can report honestly.
def foreground_process() -> str                     # basename(lower) of GetForegroundWindow's process exe; '' if unknown
def enumerate_windows() -> list[WinInfo]            # EnumWindows visible top-level, with PHYSICAL rect via GetWindowRect (DPI aware)
def mask_rects_for(allowed_exes: set[str]) -> list[tuple[int,int,int,int]]   # rects (physical) of visible top-level windows whose exe basename not in allowed_exes; skip desktop/shell

### permissions.py
@dataclass class Granted: display:str; bundle_id:str; exe:str; granted_at:int; tier:str
class Allowlist:
    apps: list[Granted]
    clipboard_read: bool; clipboard_write: bool; system_key_combos: bool
    def request_access(self, apps:list[str], reason:str, clipboardRead=False, clipboardWrite=False, systemKeyCombos=False) -> dict
        # CCC AUTOGRANT mode: for each name call apps.resolve_app; resolved -> add Granted(tier='full', granted_at=epoch_ms); unresolved -> notInstalled. Merge OR the clipboard/system flags. Return CDC-SHAPED dict: {granted:[{bundleId,displayName,grantedAt,tier}], denied:[], notInstalled?:{apps:[{requestedName,didYouMean:[]}],guidance:str}, screenshotFiltering:'mask'}
    def list_granted(self) -> dict   # {allowedApps:[{bundleId,displayName,grantedAt,tier}], grantFlags:{clipboardRead,clipboardWrite,systemKeyCombos}}
    def allowed_exes(self) -> set[str]
    def is_allowed(self, exe_basename:str) -> bool
    def is_empty(self) -> bool
ALLOWLIST = Allowlist()   # module-level singleton
def epoch_ms() -> int     # time.time()*1000 int

### clipboard.py
def read_text() -> str       # win32clipboard
def write_text(s:str) -> None

## THE 29 TOOLS (server.py registers these; tools 1-27 match Claude Desktop, tools 28-29 are omni-specific; + a dev-only `reload`)
Meta/permission:
1 request_access(apps:list[str], reason:str, clipboardRead?:bool, clipboardWrite?:bool, systemKeyCombos?:bool) -> ALLOWLIST.request_access(...)
2 list_granted_applications() -> ALLOWLIST.list_granted()
3 request_teach_access(apps:list[str], reason:str) -> STUB: return {'granted':[...auto...], 'note':'teach mode overlay not implemented in CLI build (phase 2)'}
Capture:
4 screenshot(save_to_disk?:bool) -> error if ALLOWLIST.is_empty(); build mask_rects via apps.mask_rects_for(ALLOWLIST.allowed_exes()) when config.MASKING; screen.capture(mask_rects) -> return MCP image (PNG). If save_to_disk, also write PNG to a temp path and include the path in a text note. MULTI-MONITOR: prepend an event-driven note naming the captured monitor + the other attached monitors (fires on the first multi-monitor capture and whenever the captured monitor changes; silent on same-monitor repeats and single-monitor rigs — official-computer-use parity). Return list of content [note?, text?, image].
5 zoom(region:[x0,y0,x1,y1], save_to_disk?:bool) -> crop last screenshot region in IMAGE space, upscale 2x for detail, return image. Read-only.
6 switch_display(display:str) -> screen.select_monitor(display); return text confirming + monitor list. 'auto' = follow the foreground window's monitor (primary fallback); otherwise a device name as listed in screenshot notes (e.g. '\\.\DISPLAY2').
7 cursor_position() -> physical = inputs.get_cursor_pos(); return screen.physical_to_image(px,py) as {x,y} text.
Mouse (coordinate is IMAGE space; map via screen.image_to_physical):
8 mouse_move(coordinate)
9 left_click(coordinate, text?)         text=modifiers
10 right_click(coordinate, text?)
11 middle_click(coordinate, text?)
12 double_click(coordinate, text?)      count=2
13 triple_click(coordinate, text?)      count=3
14 left_click_drag(coordinate, start_coordinate?)   # start optional => from current cursor
15 left_mouse_down()
16 left_mouse_up()
17 scroll(coordinate, scroll_direction, scroll_amount)
Keyboard:
18 key(text, repeat?)
19 hold_key(text, duration)
20 type(text)
Misc:
21 wait(duration)
22 open_application(app) -> resolve_app; if None error 'not in allowlist / not found'; require it be in allowlist (in CCC autogrant, resolving also implies allowed if previously granted; if not granted, return guidance to call request_access). launch_and_focus, then report honestly on the LaunchResult: exited -> raise 'crashed on startup'; hwnd -> 'Opened "X"'; pid but no window -> 'running, no window yet (wait+screenshot)'; kind=='shell-bare' with nothing -> raise 'could not confirm... pass the full .exe path'; else (UWP/moniker, unverifiable) -> 'Opened "X"'. MULTI-MONITOR: success paths append screen.cross_monitor_hint — with an hwnd, a PRECISE note only when the window's monitor differs from the capture monitor (names the target monitor; silent otherwise, incl. under 'auto' which follows the fresh focus); without an hwnd (UWP/shell), a generic 'may have opened on a different monitor' warning (official parity).
23 read_clipboard() -> require grantFlags.clipboardRead else error; clipboard.read_text()
24 write_clipboard(text) -> require clipboard_write else error; clipboard.write_text()
Batch:
25 computer_batch(actions:list) -> batch.run_batch(actions): execute sequentially, stop on first error, return per-action outputs; screenshot/zoom images interleaved. Coordinates always refer to the full-screen screenshot taken BEFORE the batch.
26 teach_step(explanation, next_preview, actions, anchor?) -> STUB returning {'exited':false,'note':'teach overlay not implemented (phase 2)'} but DO execute the actions (so it degrades to computer_batch-like behavior) then return a screenshot.
27 teach_batch(steps) -> STUB: iterate steps executing actions, return final screenshot + note.
omni-specific (not in CDC):
28 deactivate() -> end the computer-use session WITHOUT killing the process: overlay off + controlling window (terminal or Claude Desktop window) restored to its original size/state (incl. re-maximize if it was maximized) + ALLOWLIST.clear() + mark session inactive (a later request_access re-activates). Counterpart to request_access; mirrors how CDC auto-restores its window when a task ends.
29 display_overview(save_to_disk?:bool) -> batch.do_overview: ONE composite image of ALL monitors laid out per the virtual-desktop arrangement (each panel grabbed full-res, masked, downscaled with a single uniform factor within MAX_PIXELS, pasted at its scaled virtual position, labeled name+resolution+PRIMARY). Same allowlist gate / masking / controlling-window hide as screenshot, but does NOT touch last_scale — coordinates in the overview are NOT clickable (the note says so and points at switch_display + screenshot). Orientation aid the official computer-use lacks ("which screen is that window on?" in one call).

## TOOL GATING (foreground check)
Before each INPUT action (mouse/keyboard/scroll/drag/type/key/hold) and inside computer_batch per action:
- if ALLOWLIST.is_empty(): error like Claude Desktop ("allowlist empty, call request_access").
- compute fg = apps.foreground_process(). If config.ENFORCE_FOREGROUND and fg and not ALLOWLIST.is_allowed(fg): return error "frontmost app '<fg>' is not in the session allowlist; call request_access". If ENFORCE_FOREGROUND is off (DEFAULT for CCC), do NOT block — just proceed (optionally include a note). This is decision #1: pragmatic, permissive by default, structure preserved.
- KEYBOARD SELF-HARM GUARD (unconditional — independent of ENFORCE_FOREGROUND/DEV, added after a near-miss): before key/hold_key/type, if terminal.foreground_is_controlling() (the foreground window IS the controlling window, or — CDC only — shares its owner process / is a Claude main window), raise "keyboard input blocked ... Click the target application first". Synthetic keystrokes go to whatever holds OS focus; without this guard they land in Claude's own control surface (conversation pollution; a trailing Return sends a message or runs a shell command). Mouse actions stay exempt (a click carries its own coordinate and takes focus). Terminal hosts match by EXACT hwnd only (a second Windows Terminal window of the same process is a legitimate target, not the control surface).
screenshot is allowed whenever allowlist non-empty.

## COORDINATE INVARIANTS
- All click/move/scroll/drag/zoom coordinates from tool inputs are IMAGE-space of the most recent screenshot. Convert with screen.image_to_physical using screen.last_scale().
- If no screenshot taken yet, capture one first to establish scale (so first click still works), OR map using a fresh capture's scale.
- inputs.* receive PHYSICAL coords only.

## KEY/CHORD RULES
- Accept '+'-joined chords. Names case-insensitive. 'cmd'/'super'/'win'/'meta' -> Windows key. Primary modifier on Windows is ctrl. Examples that must work: 'Return','Escape','ctrl+a','ctrl+shift+tab','alt+F4','win+d', single chars, 'Page_Down'/'pagedown'.
- type() uses KEYEVENTF_UNICODE so any Unicode (incl Chinese) types correctly regardless of keyboard layout. If config has clipboard_write granted, a clipboard fast-path for long multi-line text is allowed but optional.

## RETURN/ERROR STYLE
- Match Claude Desktop tone: short confirmations ('Moved.', 'Opened "X".'), structured dicts for request_access/list_granted, images for screenshot/zoom. Errors are returned as tool errors (raise) with helpful guidance text.
