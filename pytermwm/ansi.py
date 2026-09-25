"""VT100/xterm compatible terminal emulator core.

``Screen`` is a grid of cells that consumes text containing ANSI/VT escape
sequences (``Screen.feed``) and keeps a scrollback history.  A cell is a tuple
``(char, fg, bg, flags)`` (see :mod:`pytermwm.colors`).
"""
from __future__ import annotations

import re
import unicodedata
from collections import deque
from functools import lru_cache
from itertools import islice
from typing import Callable, Deque, List, Optional, Tuple

from .colors import (BOLD, DIM, ITALIC, UNDERLINE, BLINK, REVERSE, HIDDEN, STRIKE,
                     WIDE, TAIL)

Cell = Tuple[str, object, object, int]
BLANK: Cell = (" ", None, None, 0)

_PRINT = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")

DEC_SPECIAL = {
    "`": "◆", "a": "▒", "b": "␉", "c": "␌", "d": "␍", "e": "␊",
    "f": "°", "g": "±", "h": "␤", "i": "␋", "j": "┘", "k": "┐",
    "l": "┌", "m": "└", "n": "┼", "o": "⎺", "p": "⎻", "q": "─",
    "r": "⎼", "s": "⎽", "t": "├", "u": "┤", "v": "┴", "w": "┬",
    "x": "│", "y": "≤", "z": "≥", "{": "π", "|": "≠", "}": "£",
    "~": "·", "+": "→", ",": "←", "-": "↑", ".": "↓", "0": "█",
}


@lru_cache(maxsize=65536)
def char_width(ch: str) -> int:
    """Terminal cell width of a single character (0, 1 or 2)."""
    o = ord(ch)
    if o < 0x300:
        return 1
    if unicodedata.combining(ch):
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me") or o in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) or 0xFE00 <= o <= 0xFE0F:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def str_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def blank_line(cols: int, bg=None) -> List[Cell]:
    return [(" ", None, bg, 0)] * cols


_G, _ESC, _ESCI, _CSI, _OSC, _STR, _OSCE = range(7)


class Screen:
    """A terminal screen with scrollback."""

    def __init__(self, rows: int = 24, cols: int = 80, history: int = 1000):
        self.rows = max(1, rows)
        self.cols = max(1, cols)
        self.history_limit = history
        self.history: Deque[Tuple[Cell, ...]] = deque(maxlen=history if history > 0 else 0)
        self.lines: List[List[Cell]] = [blank_line(self.cols) for _ in range(self.rows)]
        self.x = 0
        self.y = 0
        self.wrap_pending = False
        self.fg = None
        self.bg = None
        self.flags = 0
        self.top = 0
        self.bottom = self.rows - 1
        # modes
        self.autowrap = True
        self.force_no_wrap = False     # window overflow=clip
        self.origin = False
        self.insert = False
        self.newline_mode = False
        self.cursor_visible = True
        self.app_cursor = False
        self.app_keypad = False
        self.bracketed_paste = False
        self.mouse_mode = 0
        self.mouse_sgr = False
        self.focus_events = False
        self.reverse_video = False
        self.alt_active = False
        self._saved_main = None
        self._saved_cursor = None
        self.tabs = set(range(8, self.cols, 8))
        self.charsets = ["B", "B"]
        self.gl = 0
        self.title = ""
        self.cwd: Optional[str] = None
        self.responses: List[str] = []
        self.bell_count = 0
        self.version = 0
        self.last_line_feeds = 0   # count of scrolled lines (for scripting/"changed")
        self.hist_pushed = 0       # lines ever pushed into the history: gives every line a stable id (selection)
        self.on_title: Optional[Callable[[str], None]] = None
        self.on_resize_request: Optional[Callable[[int, int], None]] = None
        self.on_bell: Optional[Callable[[], None]] = None
        self.on_wrap: Optional[Callable[[], None]] = None                  # diagnostics: a real (non-pending) autowrap just happened
        self.on_unknown_csi: Optional[Callable[[str, list], None]] = None  # diagnostics: an unrecognised CSI final byte was ignored
        self._st = _G
        self._buf = ""
        self._last_char = " "

    # ------------------------------------------------------------------ helpers
    def _bcell(self) -> Cell:
        return (" ", None, self.bg, 0)

    def _blank_row(self) -> List[Cell]:
        return [(" ", None, self.bg, 0)] * self.cols

    def reset(self):
        h = self.history_limit
        keep_cb = (self.on_title, self.on_resize_request, self.on_bell, self.on_wrap, self.on_unknown_csi)
        limit_ver = self.version + 1
        Screen.__init__(self, self.rows, self.cols, h)
        self.on_title, self.on_resize_request, self.on_bell, self.on_wrap, self.on_unknown_csi = keep_cb
        self.version = limit_ver

    def set_history_limit(self, n: int):
        self.history_limit = n
        old = list(self.history)
        self.history = deque(old[-n:] if n > 0 else [], maxlen=n if n > 0 else 0)

    # ------------------------------------------------------------------ feeding
    def feed(self, data: str):
        if not data:
            return
        self.version += 1
        i, n = 0, len(data)
        while i < n:
            st = self._st
            if st == _G:
                m = _PRINT.match(data, i)
                if m:
                    self._put_text(m.group())
                    i = m.end()
                    continue
                c = data[i]
                i += 1
                self._control(c)
            elif st == _CSI:
                c = data[i]
                i += 1
                o = ord(c)
                if 0x40 <= o <= 0x7E:
                    self._st = _G
                    buf, self._buf = self._buf, ""
                    self._csi(buf, c)
                elif o < 0x20:
                    if c == "\x1b":
                        self._st = _ESC
                        self._buf = ""
                    elif c in ("\x18", "\x1a"):
                        self._st = _G
                        self._buf = ""
                    else:
                        self._control(c)
                elif o == 0x7F:
                    pass
                else:
                    self._buf += c
                    if len(self._buf) > 256:
                        self._st = _G
                        self._buf = ""
            elif st == _ESC:
                c = data[i]
                i += 1
                self._esc(c)
            elif st == _ESCI:
                c = data[i]
                i += 1
                self._esc_inter(self._buf, c)
                self._st = _G
                self._buf = ""
            elif st in (_OSC, _STR):
                c = data[i]
                i += 1
                if c == "\x07" and st == _OSC:
                    self._osc(self._buf)
                    self._st = _G
                    self._buf = ""
                elif c == "\x1b":
                    self._st = _OSCE
                elif c in ("\x18", "\x1a"):
                    self._st = _G
                    self._buf = ""
                else:
                    self._buf += c
                    if len(self._buf) > 65536:
                        self._st = _G
                        self._buf = ""
            elif st == _OSCE:
                c = data[i]
                i += 1
                if c == "\\":
                    if self._buf[:1].isdigit() or self._buf.startswith(("0;", "2;")):
                        self._osc(self._buf)
                    self._st = _G
                    self._buf = ""
                else:
                    # not ST: abandon string, treat as new escape
                    self._st = _ESC
                    self._buf = ""
                    self._esc(c)

    # ---------------------------------------------------------------- controls
    def _control(self, c: str):
        if c == "\n" or c == "\x0b" or c == "\x0c":
            self._linefeed()
            if self.newline_mode:
                self.x = 0
        elif c == "\r":
            self.x = 0
            self.wrap_pending = False
        elif c == "\x08":
            if self.x > 0:
                self.x -= 1
            self.wrap_pending = False
        elif c == "\t":
            self._tab()
        elif c == "\x07":
            self.bell_count += 1
            if self.on_bell:
                self.on_bell()
        elif c == "\x1b":
            self._st = _ESC
            self._buf = ""
        elif c == "\x0e":
            self.gl = 1
        elif c == "\x0f":
            self.gl = 0
        # everything else ignored

    def _tab(self):
        self.wrap_pending = False
        x = self.x + 1
        while x < self.cols - 1 and x not in self.tabs:
            x += 1
        self.x = min(x, self.cols - 1)

    def _linefeed(self):
        self.wrap_pending = False
        if self.y == self.bottom:
            self.scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _reverse_index(self):
        self.wrap_pending = False
        if self.y == self.top:
            self.scroll_down(1)
        elif self.y > 0:
            self.y -= 1

    # ---------------------------------------------------------------- scrolling
    def scroll_up(self, n: int = 1):
        n = min(n, self.bottom - self.top + 1)
        for _ in range(n):
            line = self.lines.pop(self.top)
            if self.top == 0 and not self.alt_active and self.history.maxlen:
                self.history.append(tuple(line))
                self.hist_pushed += 1
            self.last_line_feeds += 1
            self.lines.insert(self.bottom, self._blank_row())

    def scroll_down(self, n: int = 1):
        n = min(n, self.bottom - self.top + 1)
        for _ in range(n):
            self.lines.pop(self.bottom)
            self.lines.insert(self.top, self._blank_row())

    # -------------------------------------------------------------- put text
    def _put_text(self, text: str):
        cs = self.charsets[self.gl]
        if cs == "0":
            text = "".join(DEC_SPECIAL.get(c, c) for c in text)
        for ch in text:
            self._put_char(ch)
        self._last_char = text[-1]

    def _put_char(self, ch: str):
        cols = self.cols
        w = 1 if ch < "̀" else char_width(ch)
        if w == 0:
            px = self.x if self.wrap_pending else self.x - 1
            if px < 0:
                return
            line = self.lines[self.y]
            if line[px][3] & TAIL and px > 0:
                px -= 1
            c = line[px]
            line[px] = (c[0] + ch, c[1], c[2], c[3])
            return
        wrap_ok = self.autowrap and not self.force_no_wrap
        if self.wrap_pending:
            if wrap_ok:
                self.x = 0
                self._linefeed()
                if self.on_wrap:
                    self.on_wrap()
            self.wrap_pending = False
        if w == 2 and self.x >= cols - 1:
            if cols < 2:
                w = 1
            elif wrap_ok:
                self._set_cell(self.y, self.x, " ")
                self.x = 0
                self._linefeed()
                if self.on_wrap:
                    self.on_wrap()
            else:
                return
        line = self.lines[self.y]
        x = self.x
        if self.insert:
            for _ in range(w):
                line.pop()
                line.insert(x, self._bcell())
        # repair wide chars being overwritten
        old = line[x]
        if old[3] & TAIL and x > 0:
            p = line[x - 1]
            line[x - 1] = (" ", p[1], p[2], p[3] & ~(WIDE | TAIL))
        if old[3] & WIDE and x + 1 < cols:
            t = line[x + 1]
            line[x + 1] = (" ", t[1], t[2], t[3] & ~(WIDE | TAIL))
        fl = self.flags
        if w == 2:
            line[x] = (ch, self.fg, self.bg, fl | WIDE)
            nx = x + 1
            o2 = line[nx]
            if o2[3] & WIDE and nx + 1 < cols:
                t = line[nx + 1]
                line[nx + 1] = (" ", t[1], t[2], t[3] & ~(WIDE | TAIL))
            line[nx] = ("", self.fg, self.bg, fl | TAIL)
        else:
            line[x] = (ch, self.fg, self.bg, fl)
        x += w
        if x >= cols:
            self.x = cols - 1
            self.wrap_pending = wrap_ok
        else:
            self.x = x

    def _set_cell(self, y, x, ch):
        self.lines[y][x] = (ch, self.fg, self.bg, self.flags)

    # ---------------------------------------------------------------- ESC
    def _esc(self, c: str):
        self._st = _G
        if c == "[":
            self._st = _CSI
            self._buf = ""
        elif c == "]":
            self._st = _OSC
            self._buf = ""
        elif c in "PX^_":
            self._st = _STR
            self._buf = ""
        elif c in "()*+-./":
            self._st = _ESCI
            self._buf = c
        elif c == "#":
            self._st = _ESCI
            self._buf = "#"
        elif c == "7":
            self.save_cursor()
        elif c == "8":
            self.restore_cursor()
        elif c == "D":
            self._linefeed()
        elif c == "E":
            self.x = 0
            self._linefeed()
        elif c == "M":
            self._reverse_index()
        elif c == "H":
            self.tabs.add(self.x)
        elif c == "c":
            self.reset()
        elif c == "=":
            self.app_keypad = True
        elif c == ">":
            self.app_keypad = False
        elif c == "\x1b":
            self._st = _ESC
        elif c == "\\":
            pass
        elif c < " ":
            self._control(c)

    def _esc_inter(self, inter: str, c: str):
        if inter in "()":
            self.charsets[0 if inter == "(" else 1] = c
        elif inter == "#" and c == "8":
            for y in range(self.rows):
                self.lines[y] = [("E", None, None, 0)] * self.cols

    # ---------------------------------------------------------------- OSC
    def _osc(self, s: str):
        if not s:
            return
        cmd, _, arg = s.partition(";")
        if cmd in ("0", "2"):
            self.title = arg
            if self.on_title:
                self.on_title(arg)
        elif cmd == "1":
            pass
        elif cmd == "7":
            m = re.match(r"file://[^/]*(/.*)", arg)
            if m:
                self.cwd = m.group(1)

    # ---------------------------------------------------------------- CSI
    def _params(self, buf: str, default: int = 0):
        """Parse CSI parameter string -> (private marker, list of ints)."""
        private = ""
        if buf and buf[0] in "?<=>":
            private, buf = buf[0], buf[1:]
        inter = ""
        while buf and buf[-1] in " !\"#$%&'()*+,-./":
            inter = buf[-1] + inter
            buf = buf[:-1]
        params = []
        if buf:
            for p in buf.split(";"):
                if ":" in p:
                    params.append([min(int(x[:9]), 1 << 30) if x.isdigit() else 0 for x in p.split(":")])
                else:
                    params.append(min(int(p[:9]), 1 << 30) if p.isdigit() else default)
        return private, inter, params

    def _csi(self, buf: str, final: str):
        private, inter, params = self._params(buf)
        p = params

        def arg(i, default=1):
            if i < len(p):
                v = p[i]
                if isinstance(v, list):
                    v = v[0]
                return min(v, 65535) if v else default
            return default

        f = final
        if inter:
            if f == "q" or f == "p":
                return
        if private:
            if f in "hl":
                self._modes(private, p, f == "h")
            elif f == "n" and private == "?":
                if arg(0, 0) == 6:
                    self.responses.append("\x1b[?%d;%dR" % (self.y + 1, self.x + 1))
            elif f == "c" and private == ">":
                self.responses.append("\x1b[>0;95;0c")
            return
        if f == "m":
            self._sgr(p)
        elif f == "A":
            self._cursor_to(self.y - arg(0), None, rel=True, clamp_top=True)
        elif f == "B" or f == "e":
            self._cursor_to(self.y + arg(0), None, rel=True, clamp_top=True)
        elif f == "C" or f == "a":
            self.x = min(self.cols - 1, self.x + arg(0))
            self.wrap_pending = False
        elif f == "D":
            self.x = max(0, self.x - arg(0))
            self.wrap_pending = False
        elif f == "E":
            self._cursor_to(self.y + arg(0), 0, rel=True, clamp_top=True)
        elif f == "F":
            self._cursor_to(self.y - arg(0), 0, rel=True, clamp_top=True)
        elif f == "G" or f == "`":
            self.x = max(0, min(self.cols - 1, arg(0) - 1))
            self.wrap_pending = False
        elif f == "H" or f == "f":
            self._cursor_to(arg(0) - 1, arg(1) - 1)
        elif f == "d":
            self._cursor_to(arg(0) - 1, self.x, keep_x=True)
        elif f == "I":
            for _ in range(min(arg(0), self.cols)):
                self._tab()
        elif f == "Z":
            for _ in range(min(arg(0), self.cols)):
                x = self.x - 1
                while x > 0 and x not in self.tabs:
                    x -= 1
                self.x = max(0, x)
        elif f == "J":
            self._erase_display(arg(0, 0))
        elif f == "K":
            self._erase_line(arg(0, 0))
        elif f == "L":
            self._insert_lines(arg(0))
        elif f == "M":
            self._delete_lines(arg(0))
        elif f == "P":
            self._delete_chars(arg(0))
        elif f == "@":
            self._insert_chars(arg(0))
        elif f == "X":
            n = arg(0)
            line = self.lines[self.y]
            for xx in range(self.x, min(self.cols, self.x + n)):
                line[xx] = self._bcell()
        elif f == "S":
            self.scroll_up(arg(0))
        elif f == "T":
            self.scroll_down(arg(0))
        elif f == "r":
            t = arg(0) - 1
            b = arg(1, self.rows) - 1
            if 0 <= t < b < self.rows:
                self.top, self.bottom = t, b
                self._cursor_to(0, 0)
            elif not p:
                self.top, self.bottom = 0, self.rows - 1
                self._cursor_to(0, 0)
        elif f == "s":
            self.save_cursor()
        elif f == "u":
            self.restore_cursor()
        elif f == "n":
            a = arg(0, 0)
            if a == 5:
                self.responses.append("\x1b[0n")
            elif a == 6:
                y = self.y - (self.top if self.origin else 0)
                self.responses.append("\x1b[%d;%dR" % (y + 1, self.x + 1))
        elif f == "c":
            if arg(0, 0) == 0:
                self.responses.append("\x1b[?62;22c")
        elif f == "b":
            self._put_text(self._last_char * min(arg(0), self.rows * self.cols))     # REP: one screenful at most
        elif f == "g":
            a = arg(0, 0)
            if a == 0:
                self.tabs.discard(self.x)
            elif a == 3:
                self.tabs.clear()
        elif f == "h" or f == "l":
            for v in p:
                if v == 4:
                    self.insert = (f == "h")
                elif v == 20:
                    self.newline_mode = (f == "h")
        elif f == "t":
            a = arg(0, 0)
            if a == 8 and len(p) >= 3:
                if self.on_resize_request:
                    self.on_resize_request(arg(1, self.rows), arg(2, self.cols))
            elif a == 18:
                self.responses.append("\x1b[8;%d;%dt" % (self.rows, self.cols))
            elif a == 14:
                self.responses.append("\x1b[4;%d;%dt" % (self.rows * 16, self.cols * 8))
        else:
            if self.on_unknown_csi:
                self.on_unknown_csi(f, p)

    def _modes(self, private: str, params, on: bool):
        if private != "?":
            return
        for v in params:
            if isinstance(v, list):
                v = v[0]
            if v == 1:
                self.app_cursor = on
            elif v == 3:
                pass
            elif v == 5:
                self.reverse_video = on
            elif v == 6:
                self.origin = on
                self._cursor_to(0, 0)
            elif v == 7:
                self.autowrap = on
            elif v == 25:
                self.cursor_visible = on
            elif v in (9, 1000, 1002, 1003):
                self.mouse_mode = v if on else 0
            elif v == 1006:
                self.mouse_sgr = on
            elif v == 1004:
                self.focus_events = on
            elif v == 2004:
                self.bracketed_paste = on
            elif v in (47, 1047):
                self._alt_screen(on, save_cursor=False)
            elif v == 1048:
                if on:
                    self.save_cursor()
                else:
                    self.restore_cursor()
            elif v == 1049:
                self._alt_screen(on, save_cursor=True)

    def _alt_screen(self, on: bool, save_cursor: bool):
        if on and not self.alt_active:
            if save_cursor:
                self.save_cursor()
            self._saved_main = (self.lines, self.x, self.y, self.top, self.bottom, self.wrap_pending)
            self.lines = [blank_line(self.cols) for _ in range(self.rows)]
            self.top, self.bottom = 0, self.rows - 1
            self.alt_active = True
        elif not on and self.alt_active:
            lines, x, y, top, bottom, wp = self._saved_main
            # resize may have happened while alternate screen was active
            self.lines = self._fit_lines(lines, self.rows, self.cols)
            self.x, self.y = min(x, self.cols - 1), min(y, self.rows - 1)
            self.top, self.bottom = 0, self.rows - 1
            self.wrap_pending = False
            self._saved_main = None
            self.alt_active = False
            if save_cursor:
                self.restore_cursor()

    @staticmethod
    def _fit_lines(lines, rows, cols):
        out = []
        for l in lines[:rows]:
            if len(l) < cols:
                l = list(l) + [BLANK] * (cols - len(l))
            elif len(l) > cols:
                l = list(l[:cols])
            out.append(list(l))
        while len(out) < rows:
            out.append(blank_line(cols))
        return out

    def save_cursor(self):
        self._saved_cursor = (self.x, self.y, self.fg, self.bg, self.flags,
                              self.origin, list(self.charsets), self.gl, self.wrap_pending)

    def restore_cursor(self):
        if self._saved_cursor is None:
            self.x = self.y = 0
            return
        (self.x, self.y, self.fg, self.bg, self.flags, self.origin,
         cs, self.gl, self.wrap_pending) = self._saved_cursor
        self.charsets = list(cs)
        self.x = min(self.x, self.cols - 1)
        self.y = min(self.y, self.rows - 1)

    def _cursor_to(self, y, x, rel=False, clamp_top=False, keep_x=False):
        if rel and clamp_top:
            # relative movement is limited by the scroll region if inside it
            if self.top <= self.y <= self.bottom:
                y = max(self.top, min(self.bottom, y))
            else:
                y = max(0, min(self.rows - 1, y))
        elif self.origin and not rel:
            y = max(self.top, min(self.bottom, y + self.top))
        else:
            y = max(0, min(self.rows - 1, y))
        self.y = y
        if x is not None:
            self.x = max(0, min(self.cols - 1, x))
        self.wrap_pending = False

    # ---------------------------------------------------------------- erase/insert/delete
    def _erase_display(self, mode: int):
        if mode == 0:
            self._erase_line(0)
            for y in range(self.y + 1, self.rows):
                self.lines[y] = self._blank_row()
        elif mode == 1:
            self._erase_line(1)
            for y in range(0, self.y):
                self.lines[y] = self._blank_row()
        elif mode == 2:
            for y in range(self.rows):
                self.lines[y] = self._blank_row()
        elif mode == 3:
            self.history.clear()

    def _erase_line(self, mode: int):
        line = self.lines[self.y]
        b = self._bcell()
        if mode == 0:
            for x in range(self.x, self.cols):
                line[x] = b
        elif mode == 1:
            for x in range(0, min(self.x + 1, self.cols)):
                line[x] = b
        elif mode == 2:
            self.lines[self.y] = self._blank_row()
        self._fix_wide(self.y)

    def _fix_wide(self, y: int):
        line = self.lines[y]
        for x, c in enumerate(line):
            if c[3] & WIDE and (x + 1 >= self.cols or not line[x + 1][3] & TAIL):
                line[x] = (" ", c[1], c[2], c[3] & ~WIDE)
            elif c[3] & TAIL and (x == 0 or not line[x - 1][3] & WIDE):
                line[x] = (" ", c[1], c[2], c[3] & ~TAIL)

    def _insert_lines(self, n: int):
        if not (self.top <= self.y <= self.bottom):
            return
        n = min(n, self.bottom - self.y + 1)
        for _ in range(n):
            self.lines.pop(self.bottom)
            self.lines.insert(self.y, self._blank_row())
        self.x = 0
        self.wrap_pending = False

    def _delete_lines(self, n: int):
        if not (self.top <= self.y <= self.bottom):
            return
        n = min(n, self.bottom - self.y + 1)
        for _ in range(n):
            self.lines.pop(self.y)
            self.lines.insert(self.bottom, self._blank_row())
        self.x = 0
        self.wrap_pending = False

    def _delete_chars(self, n: int):
        line = self.lines[self.y]
        n = min(n, self.cols - self.x)
        del line[self.x:self.x + n]
        line.extend([self._bcell()] * n)
        self._fix_wide(self.y)

    def _insert_chars(self, n: int):
        line = self.lines[self.y]
        n = min(n, self.cols - self.x)
        del line[self.cols - n:]
        line[self.x:self.x] = [self._bcell()] * n
        self._fix_wide(self.y)

    # ---------------------------------------------------------------- SGR
    def _sgr(self, params):
        if not params:
            params = [0]
        i = 0
        n = len(params)
        while i < n:
            v = params[i]
            sub = None
            if isinstance(v, list):
                sub, v = v, v[0]
            i += 1
            if v == 0:
                self.fg = self.bg = None
                self.flags = 0
            elif v == 1:
                self.flags |= BOLD
            elif v == 2:
                self.flags |= DIM
            elif v == 3:
                self.flags |= ITALIC
            elif v == 4:
                if sub is not None and len(sub) > 1 and sub[1] == 0:
                    self.flags &= ~UNDERLINE
                else:
                    self.flags |= UNDERLINE
            elif v in (5, 6):
                self.flags |= BLINK
            elif v == 7:
                self.flags |= REVERSE
            elif v == 8:
                self.flags |= HIDDEN
            elif v == 9:
                self.flags |= STRIKE
            elif v == 21:
                self.flags |= UNDERLINE
            elif v == 22:
                self.flags &= ~(BOLD | DIM)
            elif v == 23:
                self.flags &= ~ITALIC
            elif v == 24:
                self.flags &= ~UNDERLINE
            elif v == 25:
                self.flags &= ~BLINK
            elif v == 27:
                self.flags &= ~REVERSE
            elif v == 28:
                self.flags &= ~HIDDEN
            elif v == 29:
                self.flags &= ~STRIKE
            elif 30 <= v <= 37:
                self.fg = v - 30
            elif 40 <= v <= 47:
                self.bg = v - 40
            elif 90 <= v <= 97:
                self.fg = v - 90 + 8
            elif 100 <= v <= 107:
                self.bg = v - 100 + 8
            elif v == 39:
                self.fg = None
            elif v == 49:
                self.bg = None
            elif v in (38, 48, 58):
                color = None
                if sub is not None and len(sub) > 1:
                    # colon form 38:5:n or 38:2::r:g:b or 38:2:r:g:b
                    if sub[1] == 5 and len(sub) >= 3:
                        color = min(255, sub[2])
                    elif sub[1] == 2:
                        rgb = sub[-3:]
                        if len(rgb) == 3:
                            color = tuple(min(255, c) for c in rgb)
                else:
                    mode = params[i] if i < n else None
                    if isinstance(mode, list):
                        mode = mode[0]
                    if mode == 5 and i + 1 < n:
                        c = params[i + 1]
                        color = min(255, c if isinstance(c, int) else c[0])
                        i += 2
                    elif mode == 2 and i + 3 < n:
                        vals = [x if isinstance(x, int) else x[0] for x in params[i + 1:i + 4]]
                        color = tuple(min(255, c) for c in vals)
                        i += 4
                    else:
                        i = n
                if v == 38:
                    self.fg = color
                elif v == 48:
                    self.bg = color

    # ---------------------------------------------------------------- resize
    def resize(self, rows: int, cols: int):
        rows, cols = max(1, rows), max(1, cols)
        if rows == self.rows and cols == self.cols:
            return
        self.version += 1
        if rows < self.rows:
            excess = self.rows - rows
            while excess and len(self.lines) - 1 > self.y and self._is_blank(self.lines[-1]):
                self.lines.pop()
                excess -= 1
            while excess:
                line = self.lines.pop(0)
                if not self.alt_active and self.history.maxlen:
                    self.history.append(tuple(line))
                    self.hist_pushed += 1
                self.y = max(0, self.y - 1)
                excess -= 1
        elif rows > self.rows:
            add = rows - self.rows
            while add and self.history and not self.alt_active:
                self.lines.insert(0, list(self.history.pop()))
                self.hist_pushed -= 1
                self.y += 1
                add -= 1
            while add:
                self.lines.append(blank_line(self.cols))
                add -= 1
        self.rows = rows
        self.lines = self._fit_lines(self.lines, rows, self.cols if cols == self.cols else cols)
        if self._saved_main is not None:
            pass  # fitted lazily when leaving the alt screen
        self.cols = cols
        self.tabs = set(range(8, cols, 8))
        self.top, self.bottom = 0, rows - 1
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.wrap_pending = False
        for y in range(rows):
            self._fix_wide(y)

    @staticmethod
    def _is_blank(line) -> bool:
        return all(c[0] == " " and c[2] is None and not c[3] for c in line)

    # ---------------------------------------------------------------- views / text
    def total_lines(self) -> int:
        return len(self.history) + self.rows

    def view(self, rows: int, offset: int = 0) -> List[List[Cell]]:
        """Return ``rows`` lines. ``offset`` = how many lines scrolled back from the bottom."""
        h = len(self.history)
        total = h + self.rows
        end = total - offset
        start = end - rows
        out: List[List[Cell]] = []
        if start < 0:
            pad = -start
            out.extend(blank_line(self.cols) for _ in range(pad))
            start = 0
        # history part
        if start < h:
            hist_needed = min(end, h) - start
            hl = list(islice(self.history, start, start + hist_needed))
            out.extend(list(l) for l in hl)
            start += hist_needed
        if start < end:
            out.extend(self.lines[start - h:end - h])
        return out

    @staticmethod
    def line_text(line) -> str:
        return "".join(c[0] for c in line if not c[3] & TAIL).rstrip()

    def row_text(self, y: int) -> str:
        return self.line_text(self.lines[y])

    def text(self, history: bool = False, strip_trailing_blank: bool = True) -> str:
        rows = []
        if history:
            rows.extend(self.line_text(l) for l in self.history)
        rows.extend(self.line_text(l) for l in self.lines)
        if strip_trailing_blank:
            while rows and not rows[-1]:
                rows.pop()
        return "\n".join(rows)

    def take_responses(self) -> str:
        r = "".join(self.responses)
        self.responses.clear()
        return r


def cells_from_text(text: str, fg=None, bg=None, flags=0) -> List[Cell]:
    """Convert plain text to cells (handles wide chars)."""
    out: List[Cell] = []
    for ch in text:
        w = char_width(ch)
        if w == 0:
            if out:
                c = out[-1]
                out[-1] = (c[0] + ch, c[1], c[2], c[3])
            continue
        if w == 2:
            out.append((ch, fg, bg, flags | WIDE))
            out.append(("", fg, bg, flags | TAIL))
        else:
            out.append((ch, fg, bg, flags))
    return out
