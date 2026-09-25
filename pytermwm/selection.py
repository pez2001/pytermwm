"""Text selection and the paste buffer.

A selection lives in the window manager (not in the terminal emulating pytermwm), so it stays inside one window, follows
the scrollback and looks the same in the tty client and the web UI.  Positions are ``(line_id, column)`` where
``line_id`` is stable while output scrolls (see ``Screen.hist_pushed``).

Ways to make one: dragging with the mouse (copy on release, PuTTY style), double click (word), triple click (line),
Alt+drag (rectangle), or the keyboard copy mode.  Whatever is copied goes into the paste buffer and, as OSC 52, to
terminals that support it.  ``copy-view`` (see wm) is the fallback for terminals that do not, PuTTY for one.
"""
from __future__ import annotations

import base64
from typing import List, Optional, Tuple

from .colors import REVERSE, TAIL, WIDE

Pos = Tuple[int, int]                       # (line_id, column)

DEFAULT_WORD_CHARS = "_-.~/%+=:@#?&"
MAX_OSC52_BYTES = 100000


# ------------------------------------------------------------------------------------------------ buffer geometry
def base_id(scr) -> int:
    """Line id of the oldest line still in the buffer (history first, then the visible lines)."""
    return scr.hist_pushed - len(scr.history)


def line_count(scr) -> int:
    return len(scr.history) + scr.rows


def index_of(scr, lid: int) -> Optional[int]:
    idx = lid - base_id(scr)
    return idx if 0 <= idx < line_count(scr) else None


def line_at(scr, lid: int):
    idx = index_of(scr, lid)
    if idx is None:
        return None
    h = len(scr.history)
    return scr.history[idx] if idx < h else scr.lines[idx - h]


def view_first_id(w, vh: int) -> int:
    """Line id shown on the first row of the window's viewport."""
    scr = w.screen
    return base_id(scr) + line_count(scr) - w.view_offset() - vh


def clamp_pos(scr, pos: Pos) -> Pos:
    lo = base_id(scr)
    hi = lo + line_count(scr) - 1
    return max(lo, min(hi, pos[0])), max(0, min(scr.cols - 1, pos[1]))


def cell_to_pos(w, vx: int, vy: int, vh: int) -> Pos:
    """Viewport cell -> selection position."""
    scr = w.screen
    return clamp_pos(scr, (view_first_id(w, vh) + vy, w.scroll_x + vx))


# ------------------------------------------------------------------------------------------------ words
def _klass(ch: str, word_chars: str) -> int:
    if ch == " " or ch == "":
        return 0
    if ch.isalnum() or ch in word_chars:
        return 1
    return 2


def word_span(scr, pos: Pos, word_chars: str = DEFAULT_WORD_CHARS) -> Tuple[Pos, Pos]:
    line = line_at(scr, pos[0])
    if line is None:
        return pos, pos
    col = min(pos[1], len(line) - 1)
    if line[col][3] & TAIL and col > 0:
        col -= 1
    k = _klass(line[col][0], word_chars)
    a = col
    while a > 0 and _klass(line[a - 1][0] if not line[a - 1][3] & TAIL else line[a - 2][0], word_chars) == k:
        a -= 1
    b = col
    while b + 1 < len(line) and (line[b + 1][3] & TAIL or _klass(line[b + 1][0], word_chars) == k):
        b += 1
    return (pos[0], a), (pos[0], b)


def line_span(scr, pos: Pos) -> Tuple[Pos, Pos]:
    return (pos[0], 0), (pos[0], scr.cols - 1)


# ------------------------------------------------------------------------------------------------ the selection
class Selection:
    """``unit`` is what dragging extends by: "char", "word" or "line".  ``rect`` selects a column block."""

    def __init__(self, wid: int, scr, anchor: Pos, unit: str = "char", rect: bool = False, word_chars: str = DEFAULT_WORD_CHARS):
        self.wid = wid
        self.unit = unit
        self.rect = rect
        self.word_chars = word_chars
        self.anchor = anchor
        self.head = anchor
        self.dragging = False
        self.visible = True                   # False for a bare click: nothing is selected until the mouse moves
        self.copy_mode = False                # keyboard mode: head is a visible cursor
        self.selecting = True                 # in copy mode: False until `v`
        self.alt = scr.alt_active
        self.size = (scr.rows, scr.cols)
        self._a_span = self._span(scr, anchor)

    # ---------------------------------------------------------------- validity
    def valid(self, wm) -> bool:
        w = wm.windows.get(self.wid)
        if w is None:
            return False
        scr = w.screen
        return scr.alt_active == self.alt and (scr.rows, scr.cols) == self.size

    # ---------------------------------------------------------------- range
    def _span(self, scr, pos: Pos) -> Tuple[Pos, Pos]:
        if self.unit == "word":
            return word_span(scr, pos, self.word_chars)
        if self.unit == "line":
            return line_span(scr, pos)
        return pos, pos

    def bounds(self, scr) -> Tuple[Pos, Pos]:
        """Ordered (start, end) positions, end inclusive."""
        if self.copy_mode and not self.selecting:
            return self.head, self.head
        h0, h1 = self._span(scr, self.head)
        a0, a1 = self._a_span
        if self.unit == "char":
            a0 = a1 = self.anchor
        if self.rect:
            lo = (min(a0[0], h0[0]), min(a0[1], h0[1], a1[1], h1[1]))
            hi = (max(a1[0], h1[0]), max(a0[1], h0[1], a1[1], h1[1]))
            return lo, hi
        pts = sorted([a0, a1, h0, h1])
        if self.unit == "char":
            pts = sorted([self.anchor, self.head])
            return pts[0], pts[-1]
        return pts[0], pts[-1]

    def columns_on(self, scr, lid: int, first: Pos, last: Pos) -> Optional[Tuple[int, int]]:
        if lid < first[0] or lid > last[0]:
            return None
        if self.rect:
            return first[1], last[1]
        c0 = first[1] if lid == first[0] else 0
        c1 = last[1] if lid == last[0] else scr.cols - 1
        return c0, c1

    # ---------------------------------------------------------------- content
    def text(self, scr) -> str:
        if not self.visible or (self.copy_mode and not self.selecting):
            return ""
        first, last = self.bounds(scr)
        out: List[str] = []
        for lid in range(first[0], last[0] + 1):
            line = line_at(scr, lid)
            cols = self.columns_on(scr, lid, first, last)
            if line is None or cols is None:
                continue
            c0, c1 = cols
            if c0 < len(line) and line[c0][3] & TAIL and c0 > 0:
                c0 -= 1
            if c1 < len(line) and line[c1][3] & WIDE:
                c1 += 1
            seg = line[c0:c1 + 1]
            out.append("".join((c[0] or " ") for c in seg if not c[3] & TAIL).rstrip())
        text = "\n".join(out)
        return text.rstrip("\n") if not self.rect else text

    def is_empty(self, scr) -> bool:
        return not self.text(scr).strip("\n ")

    # ---------------------------------------------------------------- drawing
    def apply(self, w, lines, vw: int, vh: int):
        """Return ``lines`` (the visible rows of ``w``) with the selection drawn in reverse video."""
        if not self.visible:
            return lines
        scr = w.screen
        first, last = self.bounds(scr)
        top = view_first_id(w, vh)
        sx = w.scroll_x
        out = list(lines)
        for r in range(min(vh, len(out))):
            cols = self.columns_on(scr, top + r, first, last)
            if cols is None:
                continue
            c0, c1 = cols[0] - sx, cols[1] - sx
            if c1 < 0 or c0 >= vw:
                continue
            c0, c1 = max(0, c0), min(vw - 1, c1)
            row = list(out[r])
            for x in range(c0, min(c1 + 1, len(row))):
                c = row[x]
                row[x] = (c[0], c[1], c[2], c[3] ^ REVERSE)
            out[r] = row
        return out


# ------------------------------------------------------------------------------------------------ clipboard helpers
def osc52(text: str) -> Optional[str]:
    """The escape sequence that puts ``text`` on the terminal's clipboard, or None when it is too large."""
    data = text.encode("utf-8", "replace")
    if len(data) > MAX_OSC52_BYTES:
        return None
    return "\x1b]52;c;%s\x07" % base64.b64encode(data).decode("ascii")
