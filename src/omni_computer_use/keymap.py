"""Key-name to Windows virtual-key (VK) mapping, aliases, and chord parsing.

Provides a case-insensitive map from human / xdotool-ish key names to Windows
VK codes, the set of recognized modifier names, and a parser that turns a
``'+'``-joined chord string into an ordered list of VK codes (modifiers first,
main key last).

Naming conventions follow the SPEC:

* ``return``/``enter`` -> ``VK_RETURN``; ``esc``/``escape``; ``tab``; ``space``;
  ``backspace``/``bksp``; ``delete``/``del``; ``home``; ``end``;
  ``pageup``/``pgup``; ``pagedown``/``pgdn``; ``up``/``down``/``left``/``right``;
  ``f1``..``f24``; letters ``a``-``z``; digits ``0``-``9``; common punctuation.
* Modifiers: ``ctrl``/``control``; ``shift``; ``alt``/``option``;
  ``win``/``cmd``/``super``/``meta`` -> ``VK_LWIN``.

All stubs raise :class:`NotImplementedError`. ``KEY_MAP`` and ``MODIFIER_NAMES``
are declared with their types but populated by the real implementation.
"""

from __future__ import annotations

# --- Windows virtual-key code constants -------------------------------------
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_BACK = 0x08
VK_TAB = 0x09
VK_CLEAR = 0x0C
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # ALT
VK_PAUSE = 0x13
VK_CAPITAL = 0x14  # Caps Lock
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22  # Page Down
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C  # Print Screen
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D  # Context-menu / "menu" key
VK_NUMPAD0 = 0x60
VK_NUMPAD1 = 0x61
VK_NUMPAD2 = 0x62
VK_NUMPAD3 = 0x63
VK_NUMPAD4 = 0x64
VK_NUMPAD5 = 0x65
VK_NUMPAD6 = 0x66
VK_NUMPAD7 = 0x67
VK_NUMPAD8 = 0x68
VK_NUMPAD9 = 0x69
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SEPARATOR = 0x6C
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

# OEM punctuation virtual keys (US layout positions).
VK_OEM_1 = 0xBA  # ;:
VK_OEM_PLUS = 0xBB  # =+
VK_OEM_COMMA = 0xBC  # ,<
VK_OEM_MINUS = 0xBD  # -_
VK_OEM_PERIOD = 0xBE  # .>
VK_OEM_2 = 0xBF  # /?
VK_OEM_3 = 0xC0  # `~
VK_OEM_4 = 0xDB  # [{
VK_OEM_5 = 0xDC  # \|
VK_OEM_6 = 0xDD  # ]}
VK_OEM_7 = 0xDE  # '"


def _build_key_map() -> dict[str, int]:
    """Build the canonical name -> VK map (canonical names are lowercase)."""
    m: dict[str, int] = {}

    # Letters a-z -> VK is the uppercase ASCII code.
    for c in range(ord("a"), ord("z") + 1):
        m[chr(c)] = ord(chr(c).upper())

    # Digits 0-9 -> VK is the ASCII code of the digit.
    for d in range(ord("0"), ord("9") + 1):
        m[chr(d)] = d

    # Function keys F1..F24 -> 0x70..0x87.
    for i in range(1, 25):
        m[f"f{i}"] = 0x6F + i

    # Numpad number names.
    for i in range(0, 10):
        m[f"numpad{i}"] = VK_NUMPAD0 + i
        m[f"kp_{i}"] = VK_NUMPAD0 + i

    # Named keys (canonical forms).
    m.update(
        {
            "return": VK_RETURN,
            "tab": VK_TAB,
            "space": VK_SPACE,
            "backspace": VK_BACK,
            "delete": VK_DELETE,
            "insert": VK_INSERT,
            "escape": VK_ESCAPE,
            "home": VK_HOME,
            "end": VK_END,
            "pageup": VK_PRIOR,
            "pagedown": VK_NEXT,
            "up": VK_UP,
            "down": VK_DOWN,
            "left": VK_LEFT,
            "right": VK_RIGHT,
            "capslock": VK_CAPITAL,
            "numlock": VK_NUMLOCK,
            "scrolllock": VK_SCROLL,
            "printscreen": VK_SNAPSHOT,
            "pause": VK_PAUSE,
            "clear": VK_CLEAR,
            "apps": VK_APPS,
            "menu": VK_APPS,
            # Modifiers (canonical).
            "ctrl": VK_CONTROL,
            "shift": VK_SHIFT,
            "alt": VK_MENU,
            "win": VK_LWIN,
            # Punctuation (US layout).
            "semicolon": VK_OEM_1,
            "equal": VK_OEM_PLUS,
            "comma": VK_OEM_COMMA,
            "minus": VK_OEM_MINUS,
            "period": VK_OEM_PERIOD,
            "slash": VK_OEM_2,
            "grave": VK_OEM_3,
            "bracketleft": VK_OEM_4,
            "backslash": VK_OEM_5,
            "bracketright": VK_OEM_6,
            "apostrophe": VK_OEM_7,
            # Numpad operators.
            "multiply": VK_MULTIPLY,
            "add": VK_ADD,
            "subtract": VK_SUBTRACT,
            "decimal": VK_DECIMAL,
            "divide": VK_DIVIDE,
        }
    )

    # Literal punctuation characters map directly to their VK.
    m.update(
        {
            ";": VK_OEM_1,
            "=": VK_OEM_PLUS,
            ",": VK_OEM_COMMA,
            "-": VK_OEM_MINUS,
            ".": VK_OEM_PERIOD,
            "/": VK_OEM_2,
            "`": VK_OEM_3,
            "[": VK_OEM_4,
            "\\": VK_OEM_5,
            "]": VK_OEM_6,
            "'": VK_OEM_7,
        }
    )

    return m


#: Map of normalized key names (lowercase) to Windows virtual-key codes.
KEY_MAP: dict[str, int] = _build_key_map()

#: Set of normalized modifier key names recognized in chords.
MODIFIER_NAMES: set[str] = {"ctrl", "shift", "alt", "win"}


#: Aliases -> canonical key names (all keys lowercase).
_ALIASES: dict[str, str] = {
    # Enter / Return.
    "enter": "return",
    "ret": "return",
    "cr": "return",
    "kp_enter": "return",
    # Escape.
    "esc": "escape",
    # Space.
    "spacebar": "space",
    " ": "space",
    # Backspace.
    "bksp": "backspace",
    "bs": "backspace",
    "back": "backspace",
    # Delete.
    "del": "delete",
    # Insert.
    "ins": "insert",
    # Page navigation.
    "pgup": "pageup",
    "page_up": "pageup",
    "prior": "pageup",
    "pgdn": "pagedown",
    "page_down": "pagedown",
    "next": "pagedown",
    # Arrows.
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    # Lock keys.
    "caps": "capslock",
    "caps_lock": "capslock",
    "num_lock": "numlock",
    "scroll_lock": "scrolllock",
    # Print screen.
    "prtsc": "printscreen",
    "prtscr": "printscreen",
    "print": "printscreen",
    "snapshot": "printscreen",
    "sysrq": "printscreen",
    # Context menu.
    "contextmenu": "apps",
    "context_menu": "apps",
    # Modifiers.
    "control": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "opt": "alt",
    "altgr": "alt",
    "cmd": "win",
    "command": "win",
    "super": "win",
    "meta": "win",
    "windows": "win",
    "lwin": "win",
    # Punctuation names (xdotool style) -> canonical punctuation names.
    "plus": "equal",
    "hyphen": "minus",
    "dash": "minus",
    "underscore": "minus",
    "dot": "period",
    "colon": "semicolon",
    "tilde": "grave",
    "backquote": "grave",
    "quote": "apostrophe",
    "singlequote": "apostrophe",
    "leftbracket": "bracketleft",
    "rightbracket": "bracketright",
    "backslash_key": "backslash",
}


def normalize_key(name: str) -> str:
    """Normalize a key name to its canonical lowercase form.

    Strips surrounding whitespace, lowercases, and resolves aliases (e.g.
    ``'Enter'`` -> ``'return'``, ``'PgDn'`` -> ``'pagedown'``, ``'cmd'`` ->
    ``'win'``) to the canonical name used as a key in :data:`KEY_MAP`.

    Args:
        name: A raw key name.

    Returns:
        The canonical lowercase key name.
    """
    raw = name.strip()
    # Single non-space character: keep case-folded for letters, but preserve
    # literal punctuation as-is (it is stored lowercase in KEY_MAP anyway).
    lowered = raw.lower()

    # Collapse internal separators in multi-word names (e.g. "Page Up").
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    if lowered in KEY_MAP:
        return lowered

    # Try a separator-normalized form ("page up"/"page-up" -> "page_up").
    collapsed = lowered.replace(" ", "_").replace("-", "_")
    if collapsed in _ALIASES:
        return _ALIASES[collapsed]
    if collapsed in KEY_MAP:
        return collapsed

    # And a form with separators removed entirely ("page up" -> "pageup").
    squeezed = lowered.replace(" ", "").replace("-", "").replace("_", "")
    if squeezed in _ALIASES:
        return _ALIASES[squeezed]
    if squeezed in KEY_MAP:
        return squeezed

    return lowered


def to_vk(name: str) -> int:
    """Resolve a key name to its Windows virtual-key code.

    Args:
        name: A raw key name (any case, aliases allowed).

    Returns:
        The Windows VK code for the key.

    Raises:
        KeyError: If the name does not map to a known key.
    """
    canonical = normalize_key(name)
    if canonical in KEY_MAP:
        return KEY_MAP[canonical]

    # Fall back: a single printable character whose uppercase is a letter/digit
    # that Windows can map via VkKeyScan-style ASCII identity (A-Z, 0-9).
    if len(canonical) == 1:
        ch = canonical
        if ch.isalpha():
            return ord(ch.upper())
        if ch.isdigit():
            return ord(ch)

    raise KeyError(f"unknown key name: {name!r}")


def parse_chord(text: str) -> list[int]:
    """Parse a ``'+'``-joined chord into VK codes in press order.

    Splits on ``'+'``; all but the last token are treated as modifiers and the
    last token as the main key. For example ``'ctrl+shift+a'`` yields
    ``[VK_CONTROL, VK_SHIFT, ord('A')]``. A bare key such as ``'Return'`` yields
    a single-element list.

    Args:
        text: A chord string, e.g. ``'ctrl+shift+tab'`` or ``'Escape'``.

    Returns:
        VK codes ordered for pressing: modifiers first, main key last.

    Raises:
        KeyError: If any token does not map to a known key.
        ValueError: If ``text`` is empty.
    """
    if text is None or text.strip() == "":
        raise ValueError("empty chord")

    stripped = text.strip()

    # Sentinel for a literal '+' key, so we can split on the '+' separator
    # without losing a literal '+' that is itself part of the chord. A run of
    # two '+' characters ('++') means "the + separator, then the + key".
    _PLUS = "\x00plus\x00"

    # A leading '+' (e.g. '+') or a doubled '++' both denote a literal plus key.
    normalized = stripped.replace("++", "+" + _PLUS)
    if normalized == "+":
        normalized = _PLUS

    tokens = normalized.split("+")

    # '+' / literal-plus resolves to the '=' key (shift+'=' is the plus glyph).
    vks: list[int] = []
    for tok in tokens:
        tok = tok.strip()
        if tok == "" or tok == _PLUS:
            vks.append(KEY_MAP["="])
        else:
            vks.append(to_vk(tok))
    return vks
