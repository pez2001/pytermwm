"""Color handling: parsing, palettes, downgrading and SGR generation.

Colors are represented as ``None`` (terminal default), an ``int`` 0..255
(indexed palette) or an ``(r, g, b)`` tuple (truecolor).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional, Tuple, Union

Color = Union[None, int, Tuple[int, int, int]]

# cell attribute flags
BOLD = 1
DIM = 2
ITALIC = 4
UNDERLINE = 8
BLINK = 16
REVERSE = 32
HIDDEN = 64
STRIKE = 128
WIDE = 256   # first half of a double width character
TAIL = 512   # placeholder cell following a WIDE cell

FLAG_NAMES = {
    "bold": BOLD, "dim": DIM, "italic": ITALIC, "underline": UNDERLINE,
    "blink": BLINK, "reverse": REVERSE, "hidden": HIDDEN, "strike": STRIKE,
}

NAMED = {
    "black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5,
    "cyan": 6, "white": 7, "gray": 8, "grey": 8,
    "bright_black": 8, "bright_red": 9, "bright_green": 10, "bright_yellow": 11,
    "bright_blue": 12, "bright_magenta": 13, "bright_cyan": 14, "bright_white": 15,
    "orange": (255, 136, 0), "pink": (255, 105, 180), "purple": (128, 0, 128),
    "brown": (139, 69, 19),
}

BASIC16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]
_CUBE = (0, 95, 135, 175, 215, 255)

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB = re.compile(r"^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$")
_IDX = re.compile(r"^(?:color|colour|c|idx|index)[:_]?(\d{1,3})$")


def parse_color(value) -> Color:
    """Parse a user supplied color (name, ``#rrggbb``, ``rgb(..)``, ``color12``, int, list)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid color: %r" % (value,))
    if isinstance(value, int):
        if 0 <= value <= 255:
            return value
        raise ValueError("color index out of range: %r" % value)
    if isinstance(value, (tuple, list)):
        if len(value) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in value):
            return (value[0], value[1], value[2])
        raise ValueError("invalid rgb color: %r" % (value,))
    s = str(value).strip().lower()
    if s in ("", "default", "none", "-"):
        return None
    if s in NAMED:
        v = NAMED[s]
        return v
    m = _IDX.match(s)
    if m:
        n = int(m.group(1))
        if n > 255:
            raise ValueError("color index out of range: %r" % value)
        return n
    m = _HEX.match(s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = _RGB.match(s)
    if m:
        rgb = tuple(int(g) for g in m.groups())
        if all(v <= 255 for v in rgb):
            return rgb  # type: ignore[return-value]
    raise ValueError("invalid color: %r" % (value,))


@lru_cache(maxsize=None)
def index_to_rgb(n: int) -> Tuple[int, int, int]:
    if n < 16:
        return BASIC16[n]
    if n < 232:
        n -= 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    g = 8 + (n - 232) * 10
    return (g, g, g)


def to_rgb(c: Color) -> Optional[Tuple[int, int, int]]:
    if c is None:
        return None
    if isinstance(c, int):
        return index_to_rgb(c)
    return c


def _dist(a, b) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


@lru_cache(maxsize=4096)
def rgb_to_256(rgb: Tuple[int, int, int]) -> int:
    best, bd = 16, 1 << 30
    r, g, b = rgb
    # cube candidate
    ci = [min(range(6), key=lambda i: abs(_CUBE[i] - v)) for v in rgb]
    cube = 16 + 36 * ci[0] + 6 * ci[1] + ci[2]
    cd = _dist(rgb, (_CUBE[ci[0]], _CUBE[ci[1]], _CUBE[ci[2]]))
    best, bd = cube, cd
    # grey ramp candidate
    avg = (r + g + b) // 3
    gi = max(0, min(23, (avg - 8 + 5) // 10))
    gv = 8 + gi * 10
    gd = _dist(rgb, (gv, gv, gv))
    if gd < bd:
        best, bd = 232 + gi, gd
    return best


@lru_cache(maxsize=4096)
def rgb_to_16(rgb: Tuple[int, int, int]) -> int:
    return min(range(16), key=lambda i: _dist(rgb, BASIC16[i]))


def downgrade(c: Color, depth: int) -> Color:
    """Reduce color ``c`` to what a terminal with ``depth`` bits can show.

    depth: 24 truecolor, 8 = 256 colors, 4 = 16 colors, 1 = monochrome.
    """
    if c is None or depth >= 24:
        return c
    if depth <= 1:
        return None
    if depth >= 8:
        return rgb_to_256(c) if isinstance(c, tuple) else c
    # 16 colors
    if isinstance(c, tuple):
        return rgb_to_16(c)
    if c < 16:
        return c
    return rgb_to_16(index_to_rgb(c))


def color_params(c: Color, bg: bool, depth: int = 24) -> str:
    c = downgrade(c, depth)
    if c is None:
        return "49" if bg else "39"
    if isinstance(c, tuple):
        return "%d;2;%d;%d;%d" % (48 if bg else 38, c[0], c[1], c[2])
    if c < 8:
        return str((40 if bg else 30) + c)
    if c < 16:
        return str((100 if bg else 90) + c - 8)
    return "%d;5;%d" % (48 if bg else 38, c)


_flag_codes = ((BOLD, "1"), (DIM, "2"), (ITALIC, "3"), (UNDERLINE, "4"),
               (BLINK, "5"), (REVERSE, "7"), (HIDDEN, "8"), (STRIKE, "9"))


@lru_cache(maxsize=8192)
def sgr(fg: Color, bg: Color, flags: int, depth: int = 24) -> str:
    """Full SGR sequence (starting from reset) for a cell style."""
    parts = ["0"]
    for bit, code in _flag_codes:
        if flags & bit:
            parts.append(code)
    if depth > 1:
        if fg is not None:
            parts.append(color_params(fg, False, depth))
        if bg is not None:
            parts.append(color_params(bg, True, depth))
    return "\x1b[" + ";".join(parts) + "m"


def blend(a: Color, b: Color, t: float) -> Color:
    """Linear blend from a to b (t in 0..1) returning truecolor."""
    ra, rb = to_rgb(a) or (0, 0, 0), to_rgb(b) or (0, 0, 0)
    return tuple(int(ra[i] + (rb[i] - ra[i]) * t) for i in range(3))  # type: ignore[return-value]
