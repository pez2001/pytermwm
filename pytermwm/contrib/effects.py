"""Animated background effects shown behind and between windows: matrix, plasma, starfield, fire, rain, ansi.

    :effect matrix          :effect off          config:  effect: plasma
    :effect ansi ~/art      an ANSI / ASCII art file, or every file of a directory, scrolled if bigger than the screen
"""
from __future__ import annotations

import math
import os
import random
from typing import List

from pytermwm.colors import DIM
from pytermwm.contrib.ansiart import EXTENSIONS, clip_row, list_art, load_art

NAMES = ("matrix", "plasma", "starfield", "fire", "rain", "ansi")
Cell = tuple
BLANK = (" ", None, None, 0)


def _rgb(r, g, b):
    return (max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b))))


class Effect:
    name = ""

    def __init__(self, seed=None, speed: float = 1.0):
        self.rng = random.Random(seed)
        self.speed = float(speed)
        self.t0 = None
        self.last = None

    def dt(self, t):
        if self.last is None:
            self.last = t
            self.t0 = t
        d = max(0.0, min(0.5, t - self.last)) * self.speed
        self.last = t
        return d

    def render(self, cols, rows, t, theme=None) -> List[List[Cell]]:
        raise NotImplementedError


class Matrix(Effect):
    """Falling code: several streams per column with a white head, a fading trail and flickering glyphs.

    Options: ``glyphs`` katakana (default, half-width so every glyph is one cell) | ascii | binary | hex,
    ``color`` green (default) | red | blue | cyan | amber | white | purple | "R,G,B"."""
    name = "matrix"
    GLYPH_SETS = {
        "katakana": "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789:.=*+-<>",
        "ascii": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:.=*+-<>|/\\",
        "binary": "01",
        "hex": "0123456789ABCDEF",
    }
    COLORS = {"green": (0, 255, 70), "red": (255, 40, 40), "blue": (60, 120, 255), "cyan": (0, 230, 230),
              "amber": (255, 176, 0), "white": (235, 235, 235), "purple": (190, 90, 255)}

    def __init__(self, glyphs="katakana", color="green", **kw):
        super().__init__(**kw)
        glyphs = str(glyphs or "katakana").lower()
        if glyphs not in self.GLYPH_SETS:
            raise ValueError("unknown glyph set %r (try: %s)" % (glyphs, ", ".join(self.GLYPH_SETS)))
        self.glyphs = self.GLYPH_SETS[glyphs]
        self.base = self.parse_color(color)
        self.streams: List[List[dict]] = []      # per column: active streams
        self.grid: List[List[str]] = []          # per cell: its current glyph (changes now and then)
        self.size = (0, 0)

    @classmethod
    def parse_color(cls, color):
        color = str(color or "green").strip().lower()
        if color in cls.COLORS:
            return cls.COLORS[color]
        try:
            r, g, b = [int(v) for v in color.replace("#", "").split(",")]
            return _rgb(r, g, b)
        except ValueError:
            raise ValueError("unknown color %r (try: %s or R,G,B)" % (color, ", ".join(cls.COLORS)))

    def _reset(self, cols, rows):
        self.size = (cols, rows)
        self.grid = [[self.rng.choice(self.glyphs) for _ in range(cols)] for _ in range(rows)]
        self.streams = [[] for _ in range(cols)]
        for x in range(cols):                                   # start with a screen that is already raining
            if self.rng.random() < 0.6:
                self.streams[x].append(self._new_stream(rows, spread=True))

    def _new_stream(self, rows, spread=False):
        length = self.rng.randint(max(4, rows // 4), max(6, int(rows * 0.9)))
        y = self.rng.uniform(-length, rows) if spread else self.rng.uniform(-length, 0)
        return {"y": y, "v": self.rng.uniform(5, 16), "len": length}

    def render(self, cols, rows, t, theme=None):
        if self.size != (cols, rows):
            self._reset(cols, rows)
        dt = self.dt(t)
        cells = [[BLANK] * cols for _ in range(rows)]
        br, bg, bb = self.base
        for x in range(cols):
            col_streams = self.streams[x]
            for st in col_streams:
                st["y"] += st["v"] * dt
            col_streams[:] = [st for st in col_streams if st["y"] - st["len"] <= rows]
            if len(col_streams) < 2 and self.rng.random() < 0.02 * max(dt, 0.02) * 30:
                if not col_streams or col_streams[-1]["y"] - col_streams[-1]["len"] > 2:
                    col_streams.append(self._new_stream(rows))
            for st in col_streams:
                head = int(st["y"])
                for k in range(st["len"]):
                    y = head - k
                    if not 0 <= y < rows:
                        continue
                    if self.rng.random() < 0.015 + (0.08 if k == 0 else 0):
                        self.grid[y][x] = self.rng.choice(self.glyphs)
                    if k == 0:
                        color, flag = (min(255, br // 2 + 170), min(255, bg // 2 + 170), min(255, bb // 2 + 170)), 0
                    else:
                        f = (1 - k / st["len"]) ** 1.4
                        color, flag = _rgb(br * (0.12 + 0.88 * f), bg * (0.12 + 0.88 * f), bb * (0.12 + 0.88 * f)), (DIM if f < 0.35 else 0)
                    cells[y][x] = (self.grid[y][x], color, None, flag)
        return cells


class Plasma(Effect):
    name = "plasma"
    SHADES = " ·░▒▓█"

    def render(self, cols, rows, t, theme=None):
        tt = t * self.speed * 0.6
        cells = []
        for y in range(rows):
            row = []
            for x in range(cols):
                v = (math.sin(x / 9.0 + tt) + math.sin(y / 4.0 - tt * 1.3) + math.sin((x + y) / 11.0 + tt * 0.7)
                     + math.sin(math.hypot(x - cols / 2, (y - rows / 2) * 2) / 7.0 - tt)) / 4.0
                h = (v + 1) / 2
                col = _rgb(30 + 60 * math.sin(h * 6.28), 30 + 60 * math.sin(h * 6.28 + 2.1), 60 + 90 * math.sin(h * 6.28 + 4.2))
                row.append((self.SHADES[int(h * (len(self.SHADES) - 1))], col, None, DIM))
            cells.append(row)
        return cells


class Starfield(Effect):
    name = "starfield"

    def __init__(self, count: int = 120, **kw):
        super().__init__(**kw)
        self.stars = [self._new() for _ in range(count)]

    def _new(self):
        return {"x": self.rng.uniform(-1, 1), "y": self.rng.uniform(-1, 1), "z": self.rng.uniform(0.05, 1.0)}

    def render(self, cols, rows, t, theme=None):
        dt = self.dt(t)
        cells = [[BLANK] * cols for _ in range(rows)]
        for i, s in enumerate(self.stars):
            s["z"] -= dt * 0.35
            if s["z"] <= 0.02:
                self.stars[i] = s = self._new()
                s["z"] = 1.0
            px = int(cols / 2 + s["x"] / s["z"] * cols / 4)
            py = int(rows / 2 + s["y"] / s["z"] * rows / 4)
            if 0 <= px < cols and 0 <= py < rows:
                b = 1 - s["z"]
                ch = "·" if b < 0.4 else "•" if b < 0.75 else "✦"
                g = int(90 + 165 * b)
                cells[py][px] = (ch, _rgb(g, g, min(255, g + 30)), None, 0 if b > 0.5 else DIM)
        return cells


class Fire(Effect):
    name = "fire"
    RAMP = " .:-=+*#%@"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.heat: List[List[float]] = []
        self.size = (0, 0)
        self.acc = 0.0

    def render(self, cols, rows, t, theme=None):
        if self.size != (cols, rows):
            self.size = (cols, rows)
            self.heat = [[0.0] * cols for _ in range(rows)]
        self.acc += self.dt(t) * 30
        while self.acc >= 1:
            self.acc -= 1
            h = self.heat
            h[-1] = [self.rng.random() * 1.2 if self.rng.random() < 0.7 else 0.0 for _ in range(cols)]
            for y in range(rows - 2, -1, -1):
                below, row = h[y + 1], h[y]
                for x in range(cols):
                    a = below[x] + below[(x - 1) % cols] + below[(x + 1) % cols] + (h[y + 2][x] if y + 2 < rows else below[x])
                    row[x] = max(0.0, a / 4.0 - 0.03)
        cells = []
        for y in range(rows):
            row = []
            for x in range(cols):
                v = min(1.0, self.heat[y][x])
                if v < 0.05:
                    row.append(BLANK)
                else:
                    col = _rgb(255 * min(1, v * 1.6), 255 * max(0, v * 1.4 - 0.4), 255 * max(0, v * 2 - 1.6))
                    row.append((self.RAMP[int(v * (len(self.RAMP) - 1))], col, None, DIM))
            cells.append(row)
        return cells


class Rain(Effect):
    name = "rain"

    def __init__(self, density: float = 0.03, **kw):
        super().__init__(**kw)
        self.drops: List[list] = []
        self.density = density

    def render(self, cols, rows, t, theme=None):
        dt = self.dt(t)
        for _ in range(int(cols * self.density * dt * 20) + (1 if self.rng.random() < cols * self.density * dt * 20 % 1 else 0)):
            self.drops.append([self.rng.randrange(cols), 0.0, self.rng.uniform(10, 22)])
        cells = [[BLANK] * cols for _ in range(rows)]
        alive = []
        for d in self.drops:
            d[1] += d[2] * dt
            y = int(d[1])
            if y < rows:
                if d[0] < cols:
                    cells[y][d[0]] = ("│" if y < rows - 1 else "·", _rgb(90, 130, 190), None, DIM)
                    if y > 0:
                        cells[y - 1][d[0]] = ("╵", _rgb(60, 90, 140), None, DIM)
                alive.append(d)
            elif d[1] < rows + 2 and 0 <= d[0] < cols:
                cells[rows - 1][d[0]] = ("~", _rgb(90, 130, 190), None, DIM)
        self.drops = alive[-400:]
        return cells


class AnsiBackground(Effect):
    """An ANSI / ASCII art file, or all art files of a directory one after the other, as the background.

    Art that is bigger than the screen pans: down (and across) and, for a single file, back up again.  Options:
    ``path`` file or directory (none: a small built-in sample), ``hold`` seconds per file (15), ``scroll`` cells per second
    when panning (4), ``pause`` seconds to rest at either end (2), ``order`` name | random, ``align`` center | top,
    ``dim`` true to darken it, ``encoding`` auto | cp437 | utf-8 | latin-1, ``ice`` true to show blinking as a bright background
    (default: as the file's SAUCE record says)."""
    name = "ansi"
    SAMPLE = ("\x1b[0;1;36m  \xdc\xdc\xdc\xdc\xdc  \x1b[0;36m\xdc\xdc  \xdc\xdc\x1b[0m\n"
              "\x1b[1;34m \xdb\xdb\xdb\xdb\xdb\xdb\xdb\x1b[0;34m\xb2\xb1\xb0\x1b[1;30m pytermwm\x1b[0m\n"
              "\x1b[0;36m  \xdf\xdf\xdf\xdf\xdf  \x1b[1;36m\xdf\xdf  \xdf\xdf\x1b[0m\n"
              "\x1b[0;37m effect ansi <file-or-directory>\n")

    def __init__(self, path=None, hold=15.0, scroll=4.0, pause=2.0, order="name", align="center", dim=False,
                 encoding="auto", ice=None, **kw):
        super().__init__(**kw)
        self.hold, self.scroll, self.pause = float(hold), float(scroll), float(pause)
        if self.hold < 1 or self.scroll <= 0 or self.pause < 0:
            raise ValueError("ansi: hold must be >= 1, scroll > 0 and pause >= 0")
        self.order = str(order).lower()
        self.align = str(align).lower()
        if self.order not in ("name", "random") or self.align not in ("center", "top"):
            raise ValueError("ansi: order is name|random, align is center|top")
        self.dim, self.encoding, self.ice = bool(dim), str(encoding), ice
        self.idx = 0
        self.clock = 0.0
        self.direction = 1
        self.cache: dict = {}
        self.bad: set = set()                                    # unreadable files: not tried again
        self._key = None
        self._cells: List[list] = []
        self.path = os.path.abspath(os.path.expanduser(str(path))) if path else None
        self.files: List[str] = []
        if self.path:
            if not os.path.exists(self.path):
                raise ValueError("no such file or directory: %s" % self.path)
            self.files = list_art(self.path, self.order)
            if not self.files:
                raise ValueError("no art files (%s) in %s" % (" ".join(EXTENSIONS), self.path))
            self._art_of(self.files[0], first=True)              # report an unreadable single file right away

    # ------------------------------------------------------------------ art
    def _art_of(self, path, first=False):
        if path in self.cache:
            return self.cache[path]
        try:
            art = load_art(path, self.encoding, self.ice)
        except (OSError, ValueError) as e:
            if first:
                raise ValueError("%s: %s" % (os.path.basename(path), e))
            return None
        if len(self.cache) >= 6:
            self.cache.pop(next(iter(self.cache)))
        self.cache[path] = art
        return art

    def _sample(self):
        if "sample" not in self.cache:
            from pytermwm.contrib.ansiart import art_from_bytes
            self.cache["sample"] = art_from_bytes(self.SAMPLE.encode("latin-1"), "cp437")
        return self.cache["sample"]

    def current(self):
        if not self.path:
            return self._sample()
        while self.files:
            p = self.files[self.idx % len(self.files)]
            art = self._art_of(p)
            if art is not None:
                return art
            self.files.remove(p)                                 # vanished or unreadable: skip it
            self.bad.add(p)
            self.cache.pop(p, None)
        return None

    def _advance(self):
        self.clock = 0.0
        if len(self.files) <= 1:
            self.direction = -self.direction                     # one picture: pan down, then back up, and so on
            return
        self.idx += 1
        if self.idx >= len(self.files):                          # end of the list: look at the directory again
            self.idx = 0
            if os.path.isdir(self.path):
                fresh = [f for f in list_art(self.path, self.order) if f not in self.bad]
                if fresh:
                    self.files = fresh

    # ------------------------------------------------------------------ frame
    def render(self, cols, rows, t, theme=None):
        dt = self.dt(t)
        art = self.current()
        if art is None:
            return [[BLANK] * cols for _ in range(rows)]
        self.clock += dt
        dist_x, dist_y = max(0, art.width - cols), max(0, art.height - rows)
        panning = dist_x > 0 or dist_y > 0
        sweep = max(dist_y / self.scroll, dist_x / (self.scroll * 2.0)) if panning else 0.0
        duration = max(self.hold, 2 * self.pause + sweep) if panning else self.hold
        if (panning or len(self.files) > 1) and self.clock >= duration:
            self._advance()
            art = self.current() or art
            dist_x, dist_y = max(0, art.width - cols), max(0, art.height - rows)
            panning = dist_x > 0 or dist_y > 0
            sweep = max(dist_y / self.scroll, dist_x / (self.scroll * 2.0)) if panning else 0.0
        u = min(1.0, max(0.0, (self.clock - self.pause) / sweep)) if panning and sweep > 0 else 0.0
        if self.direction < 0 and len(self.files) <= 1:
            u = 1.0 - u
        ax = -int(round(dist_x * u)) if dist_x else ((cols - art.width) // 2 if self.align == "center" else 0)
        ay = -int(round(dist_y * u)) if dist_y else ((rows - art.height) // 2 if self.align == "center" else 0)
        key = (id(art), cols, rows, ax, ay)
        if key == self._key:
            return self._cells
        blank = [BLANK] * cols
        out = []
        x0, x1 = max(0, -ax), min(art.width, cols - ax)
        left = max(0, ax)
        for y in range(rows):
            sy = y - ay
            if 0 <= sy < art.height and x1 > x0:
                seg = clip_row(art.rows[sy], x0, x1)
                if self.dim:
                    seg = [(c[0], c[1], c[2], c[3] | DIM) for c in seg]
                out.append([BLANK] * left + seg + [BLANK] * max(0, cols - left - len(seg)))
            else:
                out.append(list(blank))
        self._key, self._cells = key, out
        return out


EFFECTS = {"matrix": Matrix, "plasma": Plasma, "starfield": Starfield, "fire": Fire, "rain": Rain, "ansi": AnsiBackground}


_FLOATS = ("hold", "scroll", "pause")
_BOOLS = ("dim", "ice")
_ANSI_KEYS = ("path", "hold", "scroll", "pause", "order", "align", "dim", "encoding", "ice")


def _ansi_options(args, options):
    """`effect ansi [PATH] [key=value ...]` and the config mapping -> constructor options."""
    from pytermwm.commands import CommandError
    opts = dict(options or {})
    rest = list(args)
    if rest and "=" not in rest[0]:
        opts["path"] = rest.pop(0)
    for tok in rest:
        k, sep, v = tok.partition("=")
        if not sep:
            raise CommandError("ansi: expected key=value, got %r" % tok)
        opts[k] = v
    unknown = [k for k in opts if k not in _ANSI_KEYS]
    if unknown:
        raise CommandError("ansi: unknown option %s (options: %s)" % (unknown[0], ", ".join(_ANSI_KEYS)))
    try:
        for k in _FLOATS:
            if k in opts:
                opts[k] = float(opts[k])
        for k in _BOOLS:
            if k in opts and isinstance(opts[k], str):
                opts[k] = opts[k].lower() in ("1", "true", "yes", "on")
    except ValueError as e:
        raise CommandError("ansi: %s" % e)
    return opts


def setup(api):
    wm = api.wm

    def effect_command(wm_, args, options=None):
        from pytermwm.commands import CommandError
        name = args[0] if args else "list"
        if name == "list":
            return ", ".join(NAMES) + (" (active: %s)" % wm_.background.name if wm_.background else "")
        if name in ("off", "none", "stop"):
            wm_.background = None
            wm_.dirty = True
            return "effect off"
        cls = EFFECTS.get(name)
        if cls is None:
            raise CommandError("unknown effect %r (try: %s)" % (name, ", ".join(NAMES)))
        opts = {}
        if name == "matrix":                       # effect matrix [glyphs] [color]
            for key, val in zip(("glyphs", "color"), args[1:3]):
                opts[key] = val
            for key in ("glyphs", "color"):
                if key not in opts and (options or {}).get(key):
                    opts[key] = options[key]
                if key not in opts and api.config.get(key):
                    opts[key] = api.config[key]
        elif name == "ansi":                       # effect ansi [path] [key=value ...]
            opts = _ansi_options(args[1:], options)
        try:
            wm_.background = cls(speed=float(api.config.get("speed", 1.0)), **opts)
        except ValueError as e:
            raise CommandError(str(e))
        wm_.dirty = True
        return "effect " + " ".join([name] + [str(v) for v in opts.values() if not isinstance(v, bool)])

    wm.extra["effects_command"] = effect_command
    api.on_unload(lambda: (wm.extra.pop("effects_command", None), setattr(wm, "background", None), setattr(wm, "dirty", True)))
    api.command("effect", effect_command, usage="effect <%s|off|list> [...]" % "|".join(NAMES), help="Animated background effect; matrix takes a glyph set and a color, ansi a file or directory")
    if api.config.get("start"):
        effect_command(wm, [str(api.config["start"])])
