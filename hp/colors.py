"""Colours: hex, names, and the accent a group gets when it names none.

Values in the config stay human: ``color = red``, ``color = #ff5555`` and the
"name/hex" form the brief uses (``red/#ff0000``) all work. Names resolve to
the Dracula palette first, so a dashboard of named colours still looks like one
design, and fall back to the usual CSS names.

A group without ``color`` is not left grey: ``auto()`` hashes its name into
the same palette, so every group keeps its accent across restarts and edits
while new groups spread over the palette instead of piling onto one colour.
"""

from __future__ import annotations

import hashlib
import re

# Dracula (https://draculatheme.com/contribute)
ACCENTS = (
    "#bd93f9",  # purple
    "#ff79c6",  # pink
    "#8be9fd",  # cyan
    "#50fa7b",  # green
    "#ffb86c",  # orange
    "#f1fa8c",  # yellow
    "#ff5555",  # red
    "#6272a4",  # comment
)

PURPLE = ACCENTS[0]

NAMES = {
    # Dracula
    "purple": "#bd93f9",
    "pink": "#ff79c6",
    "cyan": "#8be9fd",
    "green": "#50fa7b",
    "orange": "#ffb86c",
    "yellow": "#f1fa8c",
    "red": "#ff5555",
    "comment": "#6272a4",
    "foreground": "#f8f8f2",
    "background": "#282a36",
    "selection": "#44475a",
    # CSS basics, kept inside the palette where a palette name exists
    "white": "#f8f8f2",
    "black": "#000000",
    "gray": "#44475a",
    "grey": "#44475a",
    "silver": "#c0c0c0",
    "maroon": "#800000",
    "olive": "#808000",
    "navy": "#000080",
    "teal": "#008080",
    "aqua": "#8be9fd",
    "lime": "#00ff00",
    "blue": "#6272a4",
    "magenta": "#ff79c6",
    "fuchsia": "#ff00ff",
    "violet": "#ee82ee",
    "orchid": "#da70d6",
    "plum": "#dda0dd",
    "lavender": "#e6e6fa",
    "indigo": "#4b0082",
    "gold": "#ffd700",
    "crimson": "#dc143c",
    "tomato": "#ff6347",
    "coral": "#ff7f50",
    "salmon": "#fa8072",
    "turquoise": "#40e0d0",
    "skyblue": "#87ceeb",
    "hotpink": "#ff69b4",
    "deeppink": "#ff1493",
}

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGB_RE = re.compile(r"^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})", re.I)


def _expand(digits: str) -> str:
    if len(digits) in (3, 4):                       # #f00 -> #ff0000
        digits = "".join(char * 2 for char in digits)
    return "#" + digits.lower()


def parse(value) -> str | None:
    """``'red'``, ``'#ff0000'``, ``'red/#ff0000'``, ``'rgb(255,0,0)'`` -> hex."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    for part in text.split("/"):
        found = _one(part.strip())
        if found:
            return found
    return None


def _one(text: str) -> str | None:
    if not text:
        return None
    if text.startswith("#"):
        match = HEX_RE.match(text)
        return _expand(match.group(1)) if match else None
    match = RGB_RE.match(text)
    if match:
        red, green, blue = (min(255, int(part)) for part in match.groups())
        return "#%02x%02x%02x" % (red, green, blue)
    named = NAMES.get(text.casefold())
    if named:
        return named
    match = HEX_RE.match(text)                       # bare "ff0000"
    return _expand(match.group(1)) if match else None


def auto(seed: str) -> str:
    """A stable accent for a name: same group, same colour, forever."""
    digest = hashlib.blake2b((seed or "").strip().casefold().encode("utf-8"), digest_size=4).digest()
    return ACCENTS[int.from_bytes(digest, "big") % len(ACCENTS)]
