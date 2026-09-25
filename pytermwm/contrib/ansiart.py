"""Loading of ANSI / ASCII art files (BBS style) into a grid of terminal cells.

Supports ``.ans`` art with cursor movement and colours, DOS code page 437 (block and line drawing characters, the
control-code smileys), SAUCE records (title, width, iCE colours), and plain ``.asc`` / ``.txt`` / ``.nfo`` / ``.diz`` text.
Used by the ``ansi`` background effect.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from pytermwm.ansi import Screen, str_width
from pytermwm.colors import BLINK, BOLD, REVERSE, STRIKE, TAIL, UNDERLINE, WIDE

EXTENSIONS = (".ans", ".ansi", ".asc", ".ascii", ".ice", ".nfo", ".diz", ".txt", ".art")
MAX_BYTES = 2_000_000
MAX_ROWS = 20000
MAX_COLS = 400

log = logging.getLogger("pytermwm.ansiart")


def _debug_enabled() -> bool:
    """PYTERMWM_ANSIART_DEBUG=1 (or any non-off value) turns on the diagnostics in `art_from_bytes`.

    Useful when a piece of BBS ANSI art renders with content in the wrong place: it logs the SAUCE
    record, flags every source line whose own content ran past the right edge and had to autowrap
    onto extra row(s) (the classic cause of "everything below here is shifted"), and any escape
    sequence pytermwm's terminal emulator did not recognise and ignored.
    """
    v = os.environ.get("PYTERMWM_ANSIART_DEBUG", "").strip().lower()
    return v not in ("", "0", "false", "off", "no")


def _feed_with_diagnostics(scr, text: str, sauce: dict, path: str) -> None:
    """Feed ``text`` into ``scr`` one source line at a time (behaviourally identical to a single
    ``scr.feed(text)`` call -- the parser already has to tolerate being fed in arbitrary chunks,
    since that is how real pty output arrives), logging anything that would help explain a picture
    that renders in the wrong place: the SAUCE record, every source line whose own content ran past
    the right edge and had to autowrap onto extra row(s) below it (each one shifts everything that
    follows), and any escape sequence the emulator did not recognise and silently ignored."""
    label = path or "<art>"
    log.info("%s: sauce=%r", label, sauce)
    unknown: List[tuple] = []
    scr.on_unknown_csi = lambda final, params: unknown.append((final, tuple(params)))
    src_lines = text.split("\n")
    for i, line in enumerate(src_lines):
        wraps = [0]
        scr.on_wrap = lambda: wraps.__setitem__(0, wraps[0] + 1)
        before = (scr.y, scr.x)
        scr.feed(line)
        if i < len(src_lines) - 1:
            scr.feed("\n")
        if wraps[0]:
            log.info("%s: source line %d overflowed and autowrapped onto %d extra row(s) "
                      "(cursor was at row %d col %d when it started)",
                      label, i + 1, wraps[0], before[0], before[1])
    scr.on_wrap = None
    scr.on_unknown_csi = None
    if unknown:
        tally: dict = {}
        for final, params in unknown:
            tally[final] = tally.get(final, 0) + 1
        log.info("%s: %d unrecognised escape sequence(s) ignored: %s", label, len(unknown),
                  ", ".join("ESC[%s (x%d)" % (f, n) for f, n in sorted(tally.items())))

# CP437 shows these control codes as pictures; ANSI.SYS-era art relies on it.  Not touched: BEL BS TAB LF CR EOF ESC.
_GLYPHS = "☺☻♥♦♣♠•◘○◙♂♀♪♫☼►◄↕‼¶§▬↨↑↓→←∟↔▲▼"          # codes 1..31
_KEEP = {7, 8, 9, 10, 13, 26, 27}
CONTROL_PICTURES = {i: _GLYPHS[i - 1] for i in range(1, 32) if i not in _KEEP}


@dataclass
class Art:
    rows: List[list]
    width: int
    height: int
    title: str = ""
    ice: bool = False
    path: str = ""
    sauce: dict = field(default_factory=dict)


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_art(path: str, order: str = "name") -> List[str]:
    """The art files of a directory (or the single file ``path``)."""
    import random
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return [path]
    files = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in filenames:
            if fn.lower().endswith(EXTENSIONS) and not fn.startswith("."):
                files.append(os.path.join(dirpath, fn))
    files.sort(key=lambda p: natural_key(os.path.relpath(p, path)))
    if order == "random":
        random.shuffle(files)
    return files


def parse_sauce(data: bytes):
    """Returns (data without SAUCE, sauce info dict).  The comment block and the ^Z end-of-file marker are removed too."""
    info: dict = {}
    if len(data) >= 128 and data[-128:-123] == b"SAUCE":
        rec = data[-128:]
        info = {
            "title": rec[7:42].decode("cp437", "replace").strip(" \x00"),
            "author": rec[42:62].decode("cp437", "replace").strip(" \x00"),
            "group": rec[62:82].decode("cp437", "replace").strip(" \x00"),
            "datatype": rec[94], "filetype": rec[95],
            "tinfo1": int.from_bytes(rec[96:98], "little"), "tinfo2": int.from_bytes(rec[98:100], "little"),
            "flags": rec[105],
        }
        info["ice"] = bool(rec[105] & 1)
        cut = len(data) - 128
        ncom = rec[104]
        if ncom and cut >= 5 + 64 * ncom and data[cut - 64 * ncom - 5:cut - 64 * ncom] == b"COMNT":
            cut -= 64 * ncom + 5
        data = data[:cut]
    eof = data.find(b"\x1a")
    if eof >= 0:
        data = data[:eof]
    return data, info


def decode(data: bytes, encoding: str = "auto") -> str:
    enc = (encoding or "auto").lower()
    if enc in ("utf8", "utf-8"):
        return data.decode("utf-8", "replace")
    if enc in ("latin1", "latin-1"):
        return data.decode("latin-1")
    if enc == "auto":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    text = data.decode("cp437", "replace")
    return text.translate(CONTROL_PICTURES)


def _blank(cell) -> bool:
    return cell[0] == " " and cell[2] is None and not cell[3] & (UNDERLINE | REVERSE | STRIKE)


def load_art(path: str, encoding: str = "auto", ice: Optional[bool] = None, width: Optional[int] = None,
             debug: Optional[bool] = None) -> Art:
    """Read one file.  ``ice``: treat blinking as bright background (default: from the SAUCE record).

    ``debug``: log diagnostics for this file (default: the ``PYTERMWM_ANSIART_DEBUG`` environment variable)."""
    path = os.path.expanduser(path)
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("%s is larger than %d bytes" % (os.path.basename(path), MAX_BYTES))
    return art_from_bytes(data, encoding, ice, width, path, debug)


def art_from_bytes(data: bytes, encoding: str = "auto", ice: Optional[bool] = None, width: Optional[int] = None,
                    path: str = "", debug: Optional[bool] = None) -> Art:
    data, sauce = parse_sauce(data)
    text = decode(data, encoding)
    text = text.replace("\r\n", "\n").replace("\n\r", "\n")
    lines = text.count("\n") + 1
    if width is None:
        if "\x1b" in text:
            t = sauce.get("tinfo1", 0) if sauce.get("datatype") == 1 else 0
            width = t if 1 <= t <= MAX_COLS else 80
        else:
            width = max([str_width(l.expandtabs(8)) for l in text.split("\n")] + [1])
    width = max(1, min(MAX_COLS, width))
    nrows = max(24, min(MAX_ROWS, lines + 2))
    scr = Screen(nrows, width, MAX_ROWS)
    scr.newline_mode = True                       # LF alone returns the carriage, as with ANSI.SYS
    if debug if debug is not None else _debug_enabled():
        _feed_with_diagnostics(scr, text, sauce, path)
    else:
        scr.feed(text)
    rows = [list(r) for r in scr.history] + [list(r) for r in scr.lines]
    while rows and all(_blank(c) for c in rows[-1]):
        rows.pop()
    if not rows:
        rows = [[(" ", None, None, 0)] * width]
    used = 1                                        # cut empty columns on the right so the picture centres (and only pans
    for r in rows:                                  # sideways) by what is really drawn, not by the 80 columns it was drawn in
        for i in range(len(r) - 1, used - 1, -1):
            if not _blank(r[i]):
                used = i + 1
                break
    if used < width:
        rows = [r[:used] for r in rows]
        width = used
    use_ice = sauce.get("ice", False) if ice is None else ice
    rows = [[_normalise(c, use_ice) for c in r] for r in rows[:MAX_ROWS]]
    return Art(rows, width, len(rows), sauce.get("title", ""), use_ice, path, sauce)


def _normalise(cell, ice: bool):
    """DOS colours: default is light grey on black, bold means bright, iCE turns blinking into a bright background."""
    ch, fg, bg, fl = cell
    if fl & TAIL:
        return (ch, fg, 0 if bg is None else bg, fl)
    if fg is None:
        fg = 7
    if bg is None:
        bg = 0
    if fl & BOLD:
        if isinstance(fg, int) and fg < 8:
            fg += 8
        fl &= ~BOLD
    if ice and fl & BLINK:
        if isinstance(bg, int) and bg < 8:
            bg += 8
        fl &= ~BLINK
    return (ch, fg, bg, fl)


def clip_row(row: list, x0: int, x1: int) -> list:
    """Cells x0..x1 of a row without ever showing half of a double width character."""
    out = row[x0:x1]
    if out and out[0][3] & TAIL:
        out[0] = (" ", out[0][1], out[0][2], 0)
    if out and out[-1][3] & WIDE:
        out[-1] = (" ", out[-1][1], out[-1][2], 0)
    return out
