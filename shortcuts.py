"""Keyboard and mouse shortcuts, shared by Windows, macOS and Linux.

A shortcut is stored in config.json as one short string:

    mouse:x2            the second mouse side button (Forward)
    mouse:x1            the first side button (Back)
    mouse:middle        the middle mouse button (wheel click)
    f8                  a function key pressed on its own
    ctrl+alt+j          a combination

The module is deliberately free of platform imports so it can be unit tested
anywhere. The trigger backends translate native events into these tokens.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")
MODIFIERS = frozenset(MODIFIER_ORDER)

MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "win": "cmd",
    "windows": "cmd",
}

# Keys that have no printable character of their own.
NAMED_KEYS = {
    "space": "Space",
    "tab": "Tab",
    "enter": "Enter",
    "return": "Enter",
    "esc": "Esc",
    "escape": "Esc",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page Up",
    "pagedown": "Page Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "capslock": "Caps Lock",
    "pause": "Pause",
    "scrolllock": "Scroll Lock",
    "printscreen": "Print Screen",
    "menu": "Menu",
}

MOUSE_BUTTONS = {
    "x1": "Side button 1 (Back)",
    "x2": "Side button 2 (Forward)",
    "middle": "Middle click",
}

# Bare printable keys would hijack typing, so only these may be used alone.
SAFE_BARE_KEYS = frozenset(
    {f"f{number}" for number in range(1, 25)}
    | {"pause", "scrolllock", "printscreen", "menu"}
)


def _platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def modifier_label(token: str, system: str | None = None) -> str:
    """Human name for a modifier, which differs per platform."""
    system = system or _platform()
    if token == "cmd":
        return {"windows": "Win", "macos": "Cmd"}.get(system, "Super")
    return {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}.get(token, token)


def is_modifier(token: str) -> bool:
    return token in MODIFIERS


def _canonical_key(token: str) -> str:
    """Return a canonical key name or raise ValueError."""
    token = token.strip().lower()
    if token in MODIFIER_ALIASES:
        return MODIFIER_ALIASES[token]
    if token in NAMED_KEYS:
        return token
    if len(token) == 1 and (token.isalnum()):
        return token
    if token.startswith("f") and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return f"f{number}"
    raise ValueError(f"unknown key: {token!r}")


@dataclass(frozen=True)
class Shortcut:
    """A mouse button or a key/combination, in canonical form."""

    kind: str  # "mouse" or "keyboard"
    keys: tuple[str, ...] = ()  # modifiers first, main key last
    button: str = ""  # for kind == "mouse"

    # ---------------------------------------------------------------- shape --
    @property
    def main(self) -> str:
        if self.kind == "mouse":
            return self.button
        return self.keys[-1] if self.keys else ""

    @property
    def is_mouse(self) -> bool:
        return self.kind == "mouse"

    def canonical(self) -> str:
        if self.kind == "mouse":
            return f"mouse:{self.button}"
        return "+".join(self.keys)

    def __str__(self) -> str:  # config.json value
        return self.canonical()

    # ------------------------------------------------------------- display --
    def display(self, system: str | None = None) -> str:
        system = system or _platform()
        if self.kind == "mouse":
            return MOUSE_BUTTONS.get(self.button, self.button)
        parts = [modifier_label(key, system) for key in self.keys[:-1]]
        parts.append(self.key_label(self.keys[-1]))
        return "+".join(parts)

    @staticmethod
    def key_label(token: str) -> str:
        if token in NAMED_KEYS:
            return NAMED_KEYS[token]
        if token.startswith("f") and token[1:].isdigit():
            return token.upper()
        return token.upper()

    # ------------------------------------------------------------ matching --
    def matches(self, pressed: frozenset[str] | set[str]) -> bool:
        """True when the exact set of held keys is this shortcut.

        Exact matching means Ctrl+Alt+J does not fire while Ctrl+J is held, and
        typing the letter normally (no modifiers) never triggers anything.
        """
        return self.kind == "keyboard" and frozenset(pressed) == frozenset(self.keys)

    # ---------------------------------------------------------- validation --
    def problem(self) -> str | None:
        """Why this shortcut is unsafe to use, or None when it is fine."""
        if self.kind == "mouse":
            return None if self.button in MOUSE_BUTTONS else "Unknown mouse button."
        if not self.keys:
            return "No key was detected."
        if any(token not in MODIFIERS for token in self.keys[:-1]):
            return "Only modifiers can come before the main key."
        if any(is_modifier(token) for token in self.keys[-1:]):
            return "Add a key after the modifiers."
        if len(self.keys) > 1:
            return None
        if self.keys[0] in SAFE_BARE_KEYS:
            return None
        return "Hold Ctrl or Alt as well, or use a function key."


def parse(text: str) -> Shortcut:
    """Parse a config value such as "mouse:x2" or "ctrl+alt+j"."""
    value = (text or "").strip().lower()
    if not value:
        raise ValueError("empty shortcut")

    # Legacy config values from version 1.
    if value in ("x1", "x2"):
        return Shortcut("mouse", button=value)

    if value.startswith("mouse:"):
        button = value.split(":", 1)[1].strip()
        if button not in MOUSE_BUTTONS:
            raise ValueError(f"unknown mouse button: {button!r}")
        return Shortcut("mouse", button=button)

    if value.startswith("click:"):
        button = value.split(":", 1)[1].strip()
        if button not in MOUSE_BUTTONS:
            raise ValueError(f"unknown mouse button: {button!r}")
        return Shortcut("mouse", button=button)

    raw = [part for part in value.split("+") if part.strip()]
    if not raw:
        raise ValueError("empty shortcut")

    keys = [_canonical_key(part) for part in raw]
    modifiers = [key for key in keys[:-1] if key in MODIFIERS]
    if len(modifiers) != len(keys) - 1:
        raise ValueError("only modifiers may precede the main key")
    if keys[-1] in MODIFIERS:
        raise ValueError("a shortcut needs a non-modifier key")

    ordered = sorted(modifiers, key=MODIFIER_ORDER.index)
    return Shortcut("keyboard", keys=tuple(ordered) + (keys[-1],))


def build(keys: list[str] | tuple[str, ...]) -> Shortcut:
    """Build a shortcut from a captured set of tokens (order-insensitive)."""
    cleaned = [_canonical_key(token) for token in keys]
    modifiers = sorted({key for key in cleaned if key in MODIFIERS}, key=MODIFIER_ORDER.index)
    main = [key for key in cleaned if key not in MODIFIERS]
    if len(main) != 1:
        raise ValueError("a shortcut needs exactly one main key")
    return Shortcut("keyboard", keys=tuple(modifiers) + (main[0],))


def mouse(button: str) -> Shortcut:
    if button not in MOUSE_BUTTONS:
        raise ValueError(f"unknown mouse button: {button!r}")
    return Shortcut("mouse", button=button)


def try_parse(text: str, fallback: str = "mouse:x2") -> Shortcut:
    try:
        return parse(text)
    except ValueError:
        return parse(fallback)


def describe(text: str) -> str:
    """Best-effort display for a possibly invalid config value."""
    try:
        return parse(text).display()
    except ValueError:
        return str(text)


# --------------------------------------------------------- native helpers ----
# Virtual-key codes (Windows). Kept here so they can be tested on any OS.
_VK_NAMES = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x10: "shift",
    0x11: "ctrl",
    0x12: "alt",
    0x13: "pause",
    0x14: "capslock",
    0x1B: "esc",
    0x20: "space",
    0x21: "pageup",
    0x22: "pagedown",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "printscreen",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "cmd",
    0x5C: "cmd",
    0x5D: "menu",
    0x91: "scrolllock",
    0xA0: "shift",
    0xA1: "shift",
    0xA2: "ctrl",
    0xA3: "ctrl",
    0xA4: "alt",
    0xA5: "alt",
}


def vk_to_token(vk: int) -> str | None:
    """Map a Windows virtual-key code to a shortcut token."""
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x30 <= vk <= 0x39:  # 0-9
        return chr(vk)
    if 0x41 <= vk <= 0x5A:  # A-Z
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:  # F1-F24
        return f"f{vk - 0x6F}"
    return None
