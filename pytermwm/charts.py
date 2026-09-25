"""Text mode charts and diagrams (sparklines, bars, braille line charts, gauges, diagrams).

All functions return plain unicode strings (or lists of strings); colors are added
by callers using ANSI helpers such as :func:`ansi_color`.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SPARK = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉█"
VBLOCKS = " ▁▂▃▄▅▆▇█"

# Fallback ramps for terminals whose font is missing the fine 1/8-cell Unicode block elements above (the Linux virtual
# console's built-in font, and often PuTTY's, are the common cases -- see `charts:` in the config docs).  "blocks"
# keeps the classic code page 437 shades, which are close to universal; "ascii" adds no 8-bit characters at all.
SPARK_SETS = {
    "unicode": SPARK,
    "blocks": " ░▒▓█",
    "ascii": " .:-=+*#%@",
}
GLYPH_SETS = tuple(SPARK_SETS)


def _spark_ramp(glyphs: str) -> str:
    try:
        return SPARK_SETS[glyphs]
    except KeyError:
        raise ValueError("unknown glyph set %r (choose from %s)" % (glyphs, ", ".join(GLYPH_SETS)))


def ansi_color(text: str, fg=None, bg=None, bold=False) -> str:
    """Wrap ``text`` in SGR codes. fg/bg: int 0..255 or (r,g,b) or None."""
    from .colors import color_params
    codes = []
    if bold:
        codes.append("1")
    if fg is not None:
        codes.append(color_params(fg, False))
    if bg is not None:
        codes.append(color_params(bg, True))
    if not codes:
        return text
    return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text)


def _scale(v, lo, hi):
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def spark(values: Sequence[float], width: Optional[int] = None, lo: Optional[float] = None,
          hi: Optional[float] = None, glyphs: str = "unicode") -> str:
    ramp = _spark_ramp(glyphs)
    vals = list(values)
    if width is not None:
        vals = vals[-width:] if width > 0 else []
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    levels = len(ramp)
    out = []
    for v in vals:
        f = _scale(v, lo, hi)
        out.append(ramp[min(levels - 1, int(f * levels))] if v is not None else " ")
    s = "".join(out)
    if width is not None and len(s) < width:
        s = " " * (width - len(s)) + s
    return s


def bar(frac: float, width: int, fill: str = "█", empty: str = "░", fine: bool = True, glyphs: str = "unicode") -> str:
    """Horizontal bar.  With the default glyphs and fill/empty it has 1/8 cell precision; ``glyphs`` other than
    "unicode" (see `SPARK_SETS`) turns that off, since the partial-cell characters it needs are the ones that are
    often missing (the Linux console, sometimes PuTTY -- see `charts:` in the config docs)."""
    if glyphs not in GLYPH_SETS:
        raise ValueError("unknown glyph set %r (choose from %s)" % (glyphs, ", ".join(GLYPH_SETS)))
    frac = max(0.0, min(1.0, frac))
    if width <= 0:
        return ""
    if glyphs == "ascii" and fill == "█":
        fill, empty = "#", "-"
    fine = fine and glyphs == "unicode"
    if not fine or fill != "█":
        n = int(round(frac * width))
        return fill * n + empty * (width - n)
    total = frac * width
    full = int(total)
    rem = total - full
    s = "█" * full
    if full < width:
        idx = int(rem * 8)
        s += EIGHTHS[idx] if idx > 0 else ""
    return s + empty * (width - len(s))


def gauge(pct: float, width: int, label: str = "", lo_warn: float = 70, hi_warn: float = 90) -> str:
    """Bar with the percentage in the middle e.g. ``[█████░░░ 62%]``."""
    txt = "%3.0f%%" % pct
    inner = max(1, width - 2)
    b = bar(pct / 100.0, inner)
    if len(txt) + 2 <= inner:
        pos = (inner - len(txt)) // 2
        b = b[:pos] + txt + b[pos + len(txt):]
    return ("%s " % label if label else "") + "[" + b + "]"


def column_chart(values: Sequence[float], height: int, lo: Optional[float] = None,
                 hi: Optional[float] = None, glyphs: str = "unicode") -> List[str]:
    """Vertical bar chart: returns ``height`` strings (top row first)."""
    vals = list(values)
    if not vals or height <= 0:
        return [""] * max(0, height)
    fine = glyphs == "unicode"
    fill = "#" if glyphs == "ascii" else "█"
    lo = 0 if lo is None else lo
    hi = max(vals) if hi is None else hi
    rows = []
    levels = [_scale(v, lo, hi) * height for v in vals]
    for r in range(height - 1, -1, -1):
        line = ""
        for lv in levels:
            rem = lv - r
            if rem >= 1 or (rem > 0 and not fine):     # a non-empty but sub-cell column rounds up without fine glyphs
                line += fill
            elif rem <= 0:
                line += " "
            else:
                line += VBLOCKS[max(1, int(rem * 8))]
        rows.append(line)
    return rows


def hbar_chart(items: Sequence[Tuple[str, float]], width: int, unit: str = "", sort: bool = False) -> List[str]:
    items = list(items)
    if sort:
        items.sort(key=lambda kv: -kv[1])
    if not items:
        return []
    lw = max(len(str(k)) for k, _ in items)
    vals = ["%g%s" % (round(v, 2), unit) for _, v in items]
    vw = max(len(v) for v in vals)
    bw = max(1, width - lw - vw - 3)
    top = max((v for _, v in items), default=1) or 1
    out = []
    for (k, v), vs in zip(items, vals):
        out.append("%s %s %s" % (str(k).ljust(lw), bar(v / top, bw), vs.rjust(vw)))
    return out


# braille dot bit positions for a 2x4 cell
_BR = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def braille_line(values: Sequence[float], width: int, height: int, lo: Optional[float] = None,
                 hi: Optional[float] = None, fill: bool = False) -> List[str]:
    """Line (or area) chart using braille characters. width/height in character cells."""
    vals = list(values)
    if width <= 0 or height <= 0:
        return []
    px_w, px_h = width * 2, height * 4
    grid = [[0] * width for _ in range(height)]
    if vals:
        lo = min(vals) if lo is None else lo
        hi = max(vals) if hi is None else hi
        if hi == lo:
            hi = lo + 1
        # resample to px_w points
        n = len(vals)
        pts = []
        for x in range(px_w):
            src = x * (n - 1) / (px_w - 1) if n > 1 and px_w > 1 else 0
            i0 = int(math.floor(src))
            i1 = min(n - 1, i0 + 1)
            t = src - i0
            v = vals[i0] * (1 - t) + vals[i1] * t
            pts.append(int(round((1 - _scale(v, lo, hi)) * (px_h - 1))))

        def setp(px, py):
            if 0 <= px < px_w and 0 <= py < px_h:
                grid[py // 4][px // 2] |= _BR[py % 4][px % 2]

        prev = None
        for x, y in enumerate(pts):
            if prev is not None:
                a, b = sorted((prev, y))
                for yy in range(a, b + 1):
                    setp(x, yy)
            setp(x, y)
            if fill:
                for yy in range(y, px_h):
                    setp(x, yy)
            prev = y
    return ["".join(chr(0x2800 + c) for c in row) for row in grid]


def line_chart(values: Sequence[float], width: int, height: int, lo: Optional[float] = None,
               hi: Optional[float] = None, axis: bool = True, fmt: str = "%.0f", fill: bool = False) -> List[str]:
    vals = list(values)
    lo_v = (min(vals) if vals else 0) if lo is None else lo
    hi_v = (max(vals) if vals else 1) if hi is None else hi
    if not axis:
        return braille_line(vals, width, height, lo_v, hi_v, fill)
    labels = [fmt % hi_v, fmt % lo_v]
    lw = max(len(l) for l in labels)
    body = braille_line(vals, max(1, width - lw - 1), height, lo_v, hi_v, fill)
    out = []
    for i, row in enumerate(body):
        if i == 0:
            lab = labels[0]
        elif i == len(body) - 1:
            lab = labels[1]
        else:
            lab = ""
        out.append(lab.rjust(lw) + "┤" + row if lab else " " * lw + "│" + row)
    return out


def histogram(values: Sequence[float], bins: int = 10, width: int = 40) -> List[str]:
    vals = list(values)
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - lo) / (hi - lo) * bins))] += 1
    labels = ["%g" % round(lo + (hi - lo) * i / bins, 2) for i in range(bins)]
    return hbar_chart(list(zip(labels, counts)), width)


def stacked_bar(parts: Sequence[Tuple[str, float]], width: int, glyphs: str = "█▓▒░▚▞") -> Tuple[str, List[str]]:
    total = sum(v for _, v in parts) or 1.0
    bar_s = ""
    legend = []
    used = 0
    for i, (k, v) in enumerate(parts):
        n = int(round(v / total * width)) if i < len(parts) - 1 else width - used
        n = max(0, min(n, width - used))
        g = glyphs[i % len(glyphs)]
        bar_s += g * n
        used += n
        legend.append("%s %s %.0f%%" % (g, k, 100.0 * v / total))
    return bar_s, legend


def table(rows: Sequence[Sequence], headers: Optional[Sequence[str]] = None, box: bool = True) -> List[str]:
    rows = [[str(c) for c in r] for r in rows]
    hdr = [str(h) for h in headers] if headers else None
    ncol = max([len(r) for r in rows] + ([len(hdr)] if hdr else [0]))
    widths = [0] * ncol
    for r in ([hdr] if hdr else []) + rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    def fmt(r):
        return "│ " + " │ ".join((r[i] if i < len(r) else "").ljust(widths[i]) for i in range(ncol)) + " │"
    def rule(l, m, r):
        return l + m.join("─" * (w + 2) for w in widths) + r
    out = []
    if box:
        out.append(rule("┌", "┬", "┐"))
    if hdr:
        out.append(fmt(hdr))
        out.append(rule("├", "┼", "┤"))
    for r in rows:
        out.append(fmt(r))
    if box:
        out.append(rule("└", "┴", "┘"))
    return out


def flow_diagram(nodes: Sequence[str], arrow: str = " ──▶ ") -> List[str]:
    """Horizontal chain of boxes."""
    tops, mids, bots = [], [], []
    for n in nodes:
        w = len(n) + 2
        tops.append("┌" + "─" * w + "┐")
        mids.append("│ " + n + " │")
        bots.append("└" + "─" * w + "┘")
    sp = " " * len(arrow)
    return [sp.join(tops), arrow.join(mids), sp.join(bots)]


def tree_diagram(tree: Dict, prefix: str = "") -> List[str]:
    """Render nested dict/list as a tree."""
    lines = []
    items = list(tree.items()) if isinstance(tree, dict) else [(str(x), None) for x in tree]
    for i, (k, v) in enumerate(items):
        last = i == len(items) - 1
        lines.append(prefix + ("└── " if last else "├── ") + str(k))
        if isinstance(v, (dict, list)) and v:
            lines.extend(tree_diagram(v, prefix + ("    " if last else "│   ")))
    return lines


def heat_row(values: Sequence[float], lo: float = 0.0, hi: float = 100.0) -> str:
    """Row of shade characters ('░▒▓█') for a value range."""
    chars = " ░▒▓█"
    return "".join(chars[min(4, int(_scale(v, lo, hi) * 5))] for v in values)


def color_ramp(frac: float, stops=((0, (80, 200, 120)), (0.6, (230, 200, 60)), (1.0, (230, 70, 70)))):
    """Green -> yellow -> red truecolor for a 0..1 value."""
    frac = max(0.0, min(1.0, frac))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if frac <= b:
            t = (frac - a) / (b - a) if b > a else 0
            return tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return stops[-1][1]
