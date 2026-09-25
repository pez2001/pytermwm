"""Small drawing toolkit on top of cell grids."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .ansi import Cell, char_width, str_width
from .colors import WIDE, TAIL, REVERSE, BOLD

Style = Tuple[object, object, int]   # fg, bg, flags


class Canvas:
    def __init__(self, cols: int, rows: int, fg=None, bg=None, ch: str = " "):
        self.cols = cols
        self.rows = rows
        self.cells: List[List[Cell]] = [[(ch, fg, bg, 0)] * cols for _ in range(rows)]

    def fill(self, x, y, w, h, ch=" ", fg=None, bg=None, flags=0):
        cell = (ch, fg, bg, flags)
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.cells[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                row[xx] = cell

    def put(self, x, y, text: str, fg=None, bg=None, flags=0, max_w: Optional[int] = None,
            keep_bg: bool = False) -> int:
        """Write text; returns the number of columns used. keep_bg keeps existing bg if bg is None."""
        if not (0 <= y < self.rows):
            return 0
        row = self.cells[y]
        used = 0
        limit = self.cols if max_w is None else min(self.cols, x + max_w)
        cx = x
        for ch in text:
            w = char_width(ch)
            if w == 0:
                if cx - 1 >= 0 and cx - 1 < self.cols and cx > x:
                    c = row[cx - 1]
                    row[cx - 1] = (c[0] + ch, c[1], c[2], c[3])
                continue
            if cx + w > limit:
                break
            if cx >= 0:
                b = bg
                if b is None and keep_bg:
                    b = row[cx][2]
                self._clear_wide(row, cx)
                if w == 2:
                    self._clear_wide(row, cx + 1)
                    row[cx] = (ch, fg, b, flags | WIDE)
                    row[cx + 1] = ("", fg, b, flags | TAIL)
                else:
                    row[cx] = (ch, fg, b, flags)
            cx += w
            used += w
        return used

    @staticmethod
    def _clear_wide(row, x):
        if x >= len(row):
            return
        c = row[x]
        if c[3] & TAIL and x > 0:
            p = row[x - 1]
            row[x - 1] = (" ", p[1], p[2], p[3] & ~(WIDE | TAIL))
            row[x] = (" ", c[1], c[2], c[3] & ~(WIDE | TAIL))
        elif c[3] & WIDE and x + 1 < len(row):
            t = row[x + 1]
            row[x + 1] = (" ", t[1], t[2], t[3] & ~(WIDE | TAIL))
            row[x] = (" ", c[1], c[2], c[3] & ~(WIDE | TAIL))

    def blit(self, lines: Sequence[Sequence[Cell]], x: int, y: int, clip: Optional[Tuple[int, int, int, int]] = None):
        cx0, cy0, cx1, cy1 = clip if clip else (0, 0, self.cols, self.rows)
        for dy, line in enumerate(lines):
            yy = y + dy
            if yy < max(0, cy0) or yy >= min(self.rows, cy1):
                continue
            row = self.cells[yy]
            x0 = max(x, cx0, 0)
            x1 = min(x + len(line), cx1, self.cols)
            if x0 >= x1:
                continue
            seg = list(line[x0 - x:x1 - x])
            # avoid cutting wide chars at the clip edges
            if seg and seg[0][3] & TAIL:
                seg[0] = (" ", seg[0][1], seg[0][2], seg[0][3] & ~TAIL)
            if seg and seg[-1][3] & WIDE:
                seg[-1] = (" ", seg[-1][1], seg[-1][2], seg[-1][3] & ~WIDE)
            self._clear_wide(row, x0)
            if x1 - 1 >= 0:
                self._clear_wide(row, x1 - 1)
            row[x0:x1] = seg

    def shade(self, x, y, w, h, bg=None, dim=True, fg=None):
        """Darken an area (used for shadows)."""
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.cells[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                c = row[xx]
                nb = bg if bg is not None else c[2]
                nf = fg if fg is not None else c[1]
                row[xx] = (c[0], nf, nb, (c[3] | 2) if dim else c[3])


def truncate(text: str, width: int, ell: str = "…") -> str:
    if width <= 0:
        return ""
    if str_width(text) <= width:
        return text
    out = ""
    w = 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + ell


def pad(text: str, width: int, align: str = "left") -> str:
    w = str_width(text)
    if w >= width:
        return truncate(text, width) if w > width else text
    gap = width - w
    if align == "right":
        return " " * gap + text
    if align == "center":
        l = gap // 2
        return " " * l + text + " " * (gap - l)
    return text + " " * gap


def fuzzy_score(query: str, text: str) -> Optional[int]:
    """Subsequence fuzzy match. Higher is better; None if not matching."""
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    if q in t:
        return 1000 - t.index(q) * 5 - len(t)
    ti = 0
    score = 0
    last = -2
    for ch in q:
        idx = t.find(ch, ti)
        if idx < 0:
            return None
        score += 10
        if idx == last + 1:
            score += 8
        if idx == 0 or t[idx - 1] in " -_/.:":
            score += 6
        last = idx
        ti = idx + 1
    return score - len(t)
