"""Status line: pluggable segments; the command prompt shares the same row."""
from __future__ import annotations

import os
import socket
import time
from typing import Callable, Dict, List, Optional, Tuple, Union

from .ansi import str_width
from .charts import bar, spark
from .colors import BOLD, DIM, REVERSE, blend
from .draw import Canvas, truncate
from .sysinfo import SAMPLER, human_bytes, human_rate, human_time

DEFAULT_STATUSLINE = {
    "position": "bottom",
    "left": ["desktops", "layout", "mode"],
    "center": ["title"],
    "right": ["pv", "status", "cpu", "mem", "disk", "time"],
}


class Segment:
    __slots__ = ("text", "style", "click", "prio")

    def __init__(self, text: str, style: Union[str, tuple] = "normal", click: Optional[str] = None, prio: int = 5):
        self.text, self.style, self.click, self.prio = text, style, click, prio


SEGMENTS: Dict[str, Callable] = {}


def segment(name: str):
    """Decorator registering a status line segment function ``fn(wm, opts) -> Segment | list | None``."""
    def deco(fn):
        SEGMENTS[name] = fn
        return fn
    return deco


def _pct_style(p: float) -> str:
    return "err" if p >= 90 else "warn" if p >= 70 else "normal"


@segment("desktops")
def seg_desktops(wm, o):
    out = []
    for i, d in enumerate(wm.desktops):
        act = any(wm.windows[w].activity for w in d.windows if w in wm.windows) if i != wm.cur else False
        label = "%d:%s" % (i + 1, d.name) if o.get("names", True) else str(i + 1)
        n = len(d.windows)
        if o.get("counts", False) and n:
            label += "(%d)" % n
        if act:
            label += "*"
        out.append(Segment(label, "accent" if i == wm.cur else ("warn" if act else "dim"), click="desktop %d" % (i + 1), prio=9))
    return out


@segment("layout")
def seg_layout(wm, o):
    d = wm.desk
    s = d.layout
    if d.zoom is not None:
        s += " [zoom]"
    return Segment(s, "normal", click="layout next", prio=4)


@segment("mode")
def seg_mode(wm, o):
    if wm.keymap.mode != "normal":
        return Segment("-- %s --" % wm.keymap.mode.upper(), "warn", prio=10)
    if wm.keymap.prefix_active:
        return Segment("PREFIX", "warn", prio=10)
    return None


@segment("title")
def seg_title(wm, o):
    w = wm.focused
    if not w:
        return None
    t = w.title
    return Segment(t, "normal", prio=3)


@segment("windows")
def seg_windows(wm, o):
    out = []
    for wid in wm.desk.windows:
        w = wm.windows.get(wid)
        if not w:
            continue
        out.append(Segment("%d:%s" % (wid, truncate(w.title, 16)), "accent" if wid == wm.desk.focus else "dim",
                           click="focus %d" % wid, prio=5))
    return out


@segment("time")
def seg_time(wm, o):
    return Segment(time.strftime(o.get("format", "%H:%M")), "normal", prio=8)


@segment("date")
def seg_date(wm, o):
    return Segment(time.strftime(o.get("format", "%a %d %b")), "dim", prio=2)


@segment("cpu")
def seg_cpu(wm, o):
    total, _cores = SAMPLER.cpu()
    hist = wm.history_for("cpu", total, 12)
    txt = "CPU %2.0f%%" % total
    if o.get("spark", True):
        txt += " " + spark(hist, 8, 0, 100, glyphs=wm.chart_glyphs())
    return Segment(txt, _pct_style(total), prio=6)


@segment("mem")
def seg_mem(wm, o):
    m = SAMPLER.mem()
    if not m["total"]:
        return None
    p = 100.0 * m["used"] / m["total"]
    return Segment("MEM %2.0f%%" % p, _pct_style(p), prio=5)


@segment("disk")
def seg_disk(wm, o):
    path = o.get("path", "/")
    total, used, free = SAMPLER.disk(path)
    if not total:
        return None
    p = 100.0 * used / total
    return Segment("%s %s free" % (o.get("label", path), human_bytes(free, 0)), "err" if p >= 95 else "warn" if p >= 85 else "normal", prio=4)


@segment("load")
def seg_load(wm, o):
    l = SAMPLER.load()
    return Segment("load %.2f" % l[0], "normal", prio=3)


@segment("uptime")
def seg_uptime(wm, o):
    return Segment("up " + human_time(SAMPLER.uptime()), "dim", prio=1)


@segment("net")
def seg_net(wm, o):
    rx, tx = SAMPLER.net()
    return Segment("↓%s ↑%s" % (human_rate(rx), human_rate(tx)), "normal", prio=2)


@segment("hostname")
def seg_hostname(wm, o):
    return Segment(socket.gethostname(), "dim", prio=2)


@segment("user")
def seg_user(wm, o):
    return Segment(os.environ.get("USER") or os.environ.get("LOGNAME") or "user", "dim", prio=1)


@segment("session")
def seg_session(wm, o):
    return Segment("[%s]" % wm.session_name, "dim", prio=2)


@segment("text")
def seg_text(wm, o):
    return Segment(str(o.get("text", "")), o.get("style", "normal"), prio=o.get("prio", 5))


@segment("windowcount")
def seg_wcount(wm, o):
    return Segment("%d win" % len(wm.windows), "dim", prio=1)


@segment("status")
def seg_status(wm, o):
    """Free form status items set with ``status-set`` / API.  ``key`` selects one item."""
    now = time.time()
    keys = [o["key"]] if o.get("key") else list(wm.status_items)
    out = []
    for k in keys:
        it = wm.status_items.get(k)
        if not it:
            continue
        if it.get("expires") and it["expires"] < now:
            continue
        label = it.get("label")
        txt = str(it["value"]) if (label in (None, "") and not o.get("show_key")) else "%s %s" % (label if label else k, it["value"])
        out.append(Segment(txt, it.get("style", "normal"), prio=7))
    return out


@segment("pv")
def seg_pv(wm, o):
    out = []
    now = time.time()
    for name, t in list(wm.progress.items()):
        if t.get("done") and now - t.get("done_time", now) > 5:
            wm.progress.pop(name, None)
            continue
        total = t.get("total") or 0
        cur = t.get("current", 0)
        w = int(o.get("width", 10))
        if total:
            frac = cur / total
            txt = "%s %s %3.0f%%" % (t.get("label") or name, bar(frac, w, glyphs=wm.chart_glyphs()), frac * 100)
        else:
            txt = "%s %s" % (t.get("label") or name, human_bytes(cur))
        if t.get("rate"):
            txt += " " + human_rate(t["rate"])
        if total and t.get("rate") and not t.get("done"):
            txt += " eta " + human_time((total - cur) / max(1.0, t["rate"]))
        out.append(Segment(txt, "ok" if t.get("done") else "accent", prio=9))
    return out


@segment("progress")
def seg_progress(wm, o):
    name = o.get("name")
    t = wm.progress.get(name) if name else None
    if not t:
        return None
    total = t.get("total") or 0
    frac = (t.get("current", 0) / total) if total else 0
    return Segment("%s %s %3.0f%%" % (t.get("label") or name, bar(frac, int(o.get("width", 10)), glyphs=wm.chart_glyphs()), frac * 100), "accent", prio=8)


class StatusLine:
    def __init__(self, wm):
        self.wm = wm
        self.config = dict(DEFAULT_STATUSLINE)
        self.hits: List[Tuple[int, int, str]] = []

    def set_config(self, cfg: Optional[dict]):
        c = dict(DEFAULT_STATUSLINE)
        if cfg:
            for k in ("position", "left", "center", "right"):
                if k in cfg:
                    c[k] = cfg[k]
            c["height"] = 1
        self.config = c

    @property
    def position(self) -> str:
        return self.config.get("position", "bottom")

    @property
    def height(self) -> int:
        return 0 if self.position == "off" else 1

    def _collect(self, zone) -> List[Segment]:
        out: List[Segment] = []
        for item in self.config.get(zone) or []:
            opts = {}
            if isinstance(item, dict):
                opts = dict(item)
                name = opts.pop("segment", None) or ("text" if "text" in opts else None)
            else:
                name = str(item)
            if not name:
                continue
            if isinstance(item, dict) and "text" in item and "segment" not in item:
                fn = SEGMENTS["text"]
            else:
                fn = SEGMENTS.get(name)
            if fn is None:
                out.append(Segment("?%s" % name, "err"))
                continue
            try:
                res = fn(self.wm, opts)
            except Exception as e:      # a broken plugin segment must not kill the bar
                self.wm.log.warning("status segment %s failed: %s", name, e)
                res = Segment("!%s" % name, "err")
            if res is None:
                continue
            if isinstance(res, Segment):
                out.append(res)
            else:
                out.extend(r for r in res if r is not None)
        return out

    def style(self, s: Union[str, tuple], th) -> tuple:
        if isinstance(s, tuple):
            return s
        bg = th.c("status_bg")
        if s == "accent":
            return (th.c("status_accent_fg"), th.c("status_accent_bg"), BOLD)
        if s == "dim":
            return (th.c("status_dim_fg"), bg, 0)
        if s == "ok":
            return (th.c("status_ok_fg"), bg, BOLD)
        if s == "warn":
            return (th.c("status_warn_fg"), bg, BOLD)
        if s == "err":
            return (th.c("status_err_fg"), bg, BOLD)
        return (th.c("status_fg"), bg, 0)

    def render(self, width: int) -> Tuple[List[tuple], Optional[int]]:
        """Return (row cells, cursor x or None)."""
        wm = self.wm
        th = wm.theme
        base = (th.c("status_fg"), th.c("status_bg"), 0)
        cv = Canvas(width, 1, base[0], base[1])
        self.hits = []
        if wm.prompt.active:
            return self._render_prompt(cv, width)
        left = self._collect("left")
        center = self._collect("center")
        right = self._collect("right")
        msg = wm.current_message()
        prefix = th.o("status_prefix")
        style = th.o("status_style")
        sep = th.o("status_sep")
        if msg:
            # a message wider than the line is cut to fit: fit() below drops whole segments, and would otherwise drop
            # the message too and leave the status line empty
            room = width - str_width(prefix) - 2            # the text is drawn as " text " or "[text]"
            left = [Segment(truncate(msg[0], max(1, room)), msg[1], prio=20)] + [s for s in left if s.style == "accent"]

        def text_of(s: Segment) -> str:
            t = s.text
            if style == "brackets" and s.style != "accent":
                return "[%s]" % t
            if style == "pill":
                return "▌%s▐" % t if s.style == "accent" else " %s " % t
            return " %s " % t

        def width_of(ss: List[Segment]) -> int:
            return sum(str_width(text_of(s)) for s in ss) + (0 if style in ("pill",) else 0)

        # drop low priority segments until things fit
        def fit(ss_left, ss_center, ss_right):
            pw = str_width(prefix)
            while pw + width_of(ss_left) + width_of(ss_center) + width_of(ss_right) > width:
                pool = [(s.prio, i, z) for z, lst in (("l", ss_left), ("c", ss_center), ("r", ss_right)) for i, s in enumerate(lst)]
                if not pool:
                    break
                lowest = min(pool)
                z = lowest[2]
                lst = {"l": ss_left, "c": ss_center, "r": ss_right}[z]
                if len(lst) == 0:
                    break
                lst.pop(lowest[1])
            return ss_left, ss_center, ss_right

        left, center, right = fit(left, center, right)
        x = 0
        if prefix:
            x += cv.put(0, 0, prefix, th.c("status_ok_fg"), base[1], BOLD)

        def draw(ss: List[Segment], x0: int) -> int:
            x = x0
            for s in ss:
                st = self.style(s.style, th)
                t = text_of(s)
                w = cv.put(x, 0, t, st[0], st[1], st[2])
                if s.click:
                    self.hits.append((x, x + w, s.click))
                x += w
            return x

        x = draw(left, x)
        rw = width_of(right)
        rx = max(x, width - rw)
        draw(right, rx)
        cw = width_of(center)
        cx = x + max(0, (rx - x - cw) // 2)
        if cx + cw <= rx:
            draw(center, cx)
        return cv.cells[0], None

    def _render_prompt(self, cv: Canvas, width: int):
        wm = self.wm
        th = wm.theme
        pr = wm.prompt
        fg, bg = th.c("prompt_fg"), th.c("prompt_bg")
        cv.fill(0, 0, width, 1, " ", fg, bg)
        mode = pr.effective_mode
        label = pr.label if pr.mode != "auto" else (":" if mode == "command" else "❯")
        x = cv.put(0, 0, " %s " % label, th.c("status_accent_fg"), th.c("status_accent_bg"), BOLD)
        text = pr.editor.text
        avail = width - x - 1
        pos = pr.editor.pos
        start = 0
        if pos > avail - 1:
            start = pos - (avail - 1)
        shown = text[start:start + avail]
        cv.put(x + 1, 0, shown, fg, bg)
        cursor_x = x + 1 + (pos - start)
        if pr.suggestion and pos >= len(text):
            cv.put(cursor_x, 0, pr.suggestion, th.c("status_dim_fg"), bg, max_w=max(0, width - cursor_x))
        if pr.completions:
            hint = "  ".join(c for c in pr.completions[:8])
            cv.put(max(cursor_x + 2, width - str_width(hint) - 1), 0, truncate(hint, max(0, width - cursor_x - 3)),
                   th.c("status_dim_fg"), bg)
        return cv.cells[0], cursor_x
