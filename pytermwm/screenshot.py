"""Screenshots of the whole pytermwm screen (SVG, ANSI or plain text) and whole-screen asciicast v2 recordings.

    screenshot [FILE.svg|.ans|.txt]        the current frame, as a terminal would show it
    record-screen [FILE.cast] / record-screen-stop

The SVG is self-contained (no fonts or scripts to load) so it shows on GitHub, in a browser or in an image viewer. A
screen recording is what the compositor sends a terminal, frame by frame, so ``asciinema play`` (or the ``replay``
command) shows exactly what was on screen -- all windows, borders and the status line, not just one window.
"""
from __future__ import annotations

import json
import os
import time
from typing import List, Optional
from xml.sax.saxutils import escape

from .colors import BOLD, DIM, ITALIC, REVERSE, STRIKE, TAIL, UNDERLINE, WIDE, HIDDEN, blend, sgr, to_rgb
from .render import Compositor, Frame, FrameWriter

DEFAULT_FG = (204, 204, 204)
DEFAULT_BG = (12, 12, 12)
FORMATS = (".svg", ".ans", ".txt")

# block elements drawn as rectangles: font glyphs leave hairline gaps between cells (fractions of a cell: x, y, w, h)
_BLOCKS = {
    "█": (0, 0, 1, 1), "▀": (0, 0, 1, .5), "▄": (0, .5, 1, .5), "▌": (0, 0, .5, 1), "▐": (.5, 0, .5, 1),
    "▖": (0, .5, .5, .5), "▗": (.5, .5, .5, .5), "▘": (0, 0, .5, .5), "▝": (.5, 0, .5, .5),
    "▁": (0, .875, 1, .125), "▂": (0, .75, 1, .25), "▃": (0, .625, 1, .375), "▅": (0, .375, 1, .625),
    "▆": (0, .25, 1, .75), "▇": (0, .125, 1, .875),
}


def _hex(c) -> str:
    return "#%02x%02x%02x" % c


def _style(cell, dfg, dbg):
    """(fg, bg, flags) with defaults, reverse and dim resolved to plain RGB."""
    _ch, fg, bg, fl = cell
    fg, bg = to_rgb(fg) or dfg, to_rgb(bg) or dbg
    if fl & REVERSE:
        fg, bg = bg, fg
    if fl & DIM:
        fg = blend(fg, bg, 0.45)
    if fl & HIDDEN:
        fg = bg
    return fg, bg, fl


def frame_to_svg(frame: Frame, fg=None, bg=None, title: str = "", font_size: float = 14.0, chrome: bool = True) -> str:
    """An SVG picture of a frame; ``chrome`` adds a window frame with a title bar around it."""
    dfg, dbg = to_rgb(fg) or DEFAULT_FG, to_rgb(bg) or DEFAULT_BG
    cw, ch = font_size * 0.6, font_size * 1.25
    pad, bar = (12.0, 30.0) if chrome else (0.0, 0.0)
    ox, oy = pad, pad + bar
    width, height = frame.cols * cw + 2 * pad, frame.rows * ch + 2 * pad + bar
    out: List[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %.1f %.1f">' % (
            round(width), round(height), width, height),
        '<style>text{font-family:"Cascadia Mono","DejaVu Sans Mono",Menlo,Consolas,"Liberation Mono",monospace;'
        'font-size:%.1fpx;white-space:pre}.b{font-weight:bold}.i{font-style:italic}.u{text-decoration:underline}'
        '.s{text-decoration:line-through}</style>' % font_size,
    ]
    if chrome:
        out.append('<rect width="100%%" height="100%%" rx="8" fill="%s"/>' % _hex(blend(dbg, (60, 60, 60), 0.5)))
        for i, dot in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            out.append('<circle cx="%.1f" cy="%.1f" r="6" fill="%s"/>' % (pad + 8 + i * 20, pad + bar / 2 - 4, dot))
        if title:
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" fill="#aaaaaa">%s</text>' % (
                width / 2, pad + bar / 2, escape(title)))
    out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (ox, oy, frame.cols * cw, frame.rows * ch, _hex(dbg)))
    for y, row in enumerate(frame.cells):
        ty = oy + y * ch
        # backgrounds: one rect per run of equal colour
        x = 0
        while x < len(row):
            _fg, b, _fl = _style(row[x], dfg, dbg)
            x2 = x + 1
            while x2 < len(row) and _style(row[x2], dfg, dbg)[1] == b:
                x2 += 1
            if b != dbg:
                out.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s"/>' % (
                    ox + x * cw, ty, (x2 - x) * cw + 0.3, ch + 0.3, _hex(b)))
            x = x2
        # glyphs: block elements as rects, the rest as runs of text of one style (textLength pins them to the grid)
        x = 0
        while x < len(row):
            cell = row[x]
            c, fl = cell[0] or " ", cell[3]
            if fl & TAIL or c == " ":
                x += 1
                continue
            f, _b, _fl = _style(cell, dfg, dbg)
            if c in _BLOCKS:
                bx, by, bw, bh = _BLOCKS[c]
                out.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s"/>' % (
                    ox + (x + bx) * cw, ty + by * ch, bw * cw + 0.3, bh * ch + 0.3, _hex(f)))
                x += 1
                continue
            key = (f, fl & (BOLD | ITALIC | UNDERLINE | STRIKE))
            text, n, x2 = [], 0, x
            while x2 < len(row):
                cc = row[x2]
                if cc[3] & TAIL:
                    x2 += 1
                    continue
                g = cc[0] or " "
                if g in _BLOCKS or (g != " " and (_style(cc, dfg, dbg)[0], cc[3] & (BOLD | ITALIC | UNDERLINE | STRIKE)) != key):
                    break
                text.append(g)
                n += 2 if cc[3] & WIDE else 1
                x2 += 1
            while text and text[-1] == " ":
                text.pop()
                n -= 1
            cls = " ".join(k for k, bit in (("b", BOLD), ("i", ITALIC), ("u", UNDERLINE), ("s", STRIKE)) if key[1] & bit)
            out.append('<text x="%.2f" y="%.2f" fill="%s"%s textLength="%.2f" lengthAdjust="spacingAndGlyphs">%s</text>' % (
                ox + x * cw, ty + ch * 0.78, _hex(f), ' class="%s"' % cls if cls else "", n * cw, escape("".join(text))))
            x = x2
    out.append("</svg>")
    return "\n".join(out) + "\n"


def frame_to_ansi(frame: Frame, depth: int = 24) -> str:
    """The frame as ANSI escapes (``cat file.ans`` shows it in a terminal at least as wide)."""
    lines = []
    for row in frame.cells:
        parts, cur = [], None
        for c in row:
            if c[3] & TAIL:
                continue
            st = (c[1], c[2], c[3] & 0xFF)
            if st != cur:
                parts.append(sgr(st[0], st[1], st[2], depth))
                cur = st
            parts.append(c[0] or " ")
        lines.append("".join(parts) + "\x1b[0m")
    return "\r\n".join(lines) + "\r\n"


def compose(wm) -> Frame:
    return Compositor(wm).compose(wm.cols, wm.rows)


def screenshot(wm, path: str, title: Optional[str] = None) -> str:
    """Write the current screen to ``path`` (format from the extension: .svg, .ans or .txt)."""
    path = os.path.abspath(os.path.expanduser(path))
    ext = os.path.splitext(path)[1].lower()
    if ext not in FORMATS:
        raise ValueError("screenshot: unknown format %r (use %s)" % (ext or path, " ".join(FORMATS)))
    frame = compose(wm)
    th = wm.theme
    if ext == ".svg":
        data = frame_to_svg(frame, th.c("window_fg"), th.c("desktop_bg") or th.c("window_bg"),
                            title if title is not None else "pytermwm - %s" % th.name)
    elif ext == ".ans":
        data = frame_to_ansi(frame)
    else:
        data = frame.text() + "\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
    return path


def default_path(kind: str, ext: str) -> str:
    from . import protocol as P
    return os.path.join(P.state_dir(), kind, "pytermwm-%s%s" % (time.strftime("%Y%m%d-%H%M%S"), ext))


class ScreenRecorder:
    """asciicast v2 of the whole screen: every composed frame, written as the minimal update a terminal needs.

    ``frame(frame, t)`` takes an explicit timestamp (seconds since the start) for scripted, reproducible recordings;
    without one the wall clock is used."""

    def __init__(self, path: str, cols: int, rows: int, title: str = "", depth: int = 24):
        self.path = path
        self.t0 = time.time()
        self.size = (cols, rows)
        self.writer = FrameWriter(depth)
        self.events = 0
        self.last_t = 0.0
        self.closed = False
        self.error = None
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.f = open(path, "w", encoding="utf-8", newline="\n")
        header = {"version": 2, "width": int(cols), "height": int(rows), "timestamp": int(self.t0),
                  "env": {"TERM": "xterm-256color", "SHELL": ""}}
        if title:
            header["title"] = title
        self.f.write(json.dumps(header, ensure_ascii=False) + "\n")

    def _event(self, t: float, kind: str, data: str):
        if self.closed:
            return
        try:
            self.f.write(json.dumps([round(t, 6), kind, data], ensure_ascii=False) + "\n")
            self.events += 1
        except (OSError, ValueError) as e:
            self.error = str(e)
            self.close()

    def frame(self, frame: Frame, t: Optional[float] = None):
        t = (time.time() - self.t0) if t is None else float(t)
        t = self.last_t = max(t, self.last_t)
        if (frame.cols, frame.rows) != self.size:
            self.size = (frame.cols, frame.rows)
            self._event(t, "r", "%dx%d" % self.size)
            self.writer.invalidate()
        data = self.writer.write(frame)
        if data:
            self._event(t, "o", data)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.f.close()
            except OSError:
                pass

    def describe(self) -> dict:
        return {"path": self.path, "events": self.events, "seconds": round(self.last_t, 1), "error": self.error}


def start_recording(wm, path: Optional[str] = None, overwrite: bool = False) -> ScreenRecorder:
    if getattr(wm, "screen_recorder", None) is not None:
        raise ValueError("the screen is already being recorded to %s" % wm.screen_recorder.path)
    path = os.path.abspath(os.path.expanduser(path)) if path else default_path("recordings", ".cast")
    if os.path.exists(path) and not overwrite:
        raise ValueError("%s already exists (use -f to overwrite it)" % path)
    wm.screen_recorder = ScreenRecorder(path, wm.cols, wm.rows, title="pytermwm")
    wm.dirty = True
    return wm.screen_recorder


def stop_recording(wm) -> Optional[dict]:
    rec = getattr(wm, "screen_recorder", None)
    if rec is None:
        return None
    wm.screen_recorder = None
    rec.frame(compose(wm))
    info = rec.describe()
    rec.close()
    return info
