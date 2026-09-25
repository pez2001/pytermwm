"""Builtin internal window kinds: text, help, log, status, viewer, dirwatch, chart, debug."""
from __future__ import annotations

import code
import collections
import contextlib
import fnmatch
import io
import json
import os
import re
import subprocess
import threading
import time
import traceback
from typing import Callable, Deque, Dict, List, Optional

from .ansi import str_width
from .charts import bar, ansi_color, braille_line, line_chart, spark, column_chart, gauge, color_ramp, histogram
from .commands import CommandError
from .logs import format_record, ring_of
from .prompt import LineEditor
from .sysinfo import SAMPLER, human_bytes, human_rate, human_time
from .window import InternalWindow, TextWindow

_ANSI = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def visible_len(s: str) -> int:
    return str_width(strip_ansi(s))


def dim(s):
    return "\x1b[2m%s\x1b[22m" % s


def bold(s):
    return "\x1b[1m%s\x1b[22m" % s


# ---------------------------------------------------------------------------- text/help
class HelpWindow(TextWindow):
    kind = "help"

    def __init__(self, wid, title, rows, cols, wm=None, topic="index", **opts):
        super().__init__(wid, title or "help", rows, cols, "", **opts)
        self._wm = wm
        self.topic = topic
        self.load(topic)

    def load(self, topic):
        from .helpdata import get_topic, topic_names
        title, text = get_topic(self._wm, topic)
        self.topic = topic
        self.title_text = "help: %s" % title
        self.lines = text.split("\n")
        self.scroll_y = 0
        self.dirty = True

    def handle_key(self, key, raw):
        from .helpdata import topic_names
        names = topic_names(self._wm)
        if key in ("n", "Right", "Tab"):
            self.load(names[(names.index(self.topic) + 1) % len(names)] if self.topic in names else names[0])
            return True
        if key in ("p", "Left", "S-Tab"):
            self.load(names[(names.index(self.topic) - 1) % len(names)] if self.topic in names else names[0])
            return True
        if key.isdigit() and int(key) < len(names):
            self.load(names[int(key)])
            return True
        return super().handle_key(key, raw)


# ---------------------------------------------------------------------------- log
class LogWindow(InternalWindow):
    kind = "log"
    refresh_interval = 0.5

    def __init__(self, wid, title, rows, cols, wm=None, level="info", pattern="", **opts):
        super().__init__(wid, title or "log", rows, cols, **opts)
        self._wm = wm
        self.level = level
        self.pattern = pattern
        self.off = 0
        self._seq = -1
        self.scroll_y = 0

    def render(self, cols, rows):
        ring = ring_of(self._wm.log) if self._wm else None
        recs = ring.tail(2000, self.level, self.pattern or None) if ring else []
        n = rows - 1
        end = len(recs) - self.off
        start = max(0, end - n)
        lines = [dim("level=%s filter=%r  [d/i/w/e level, / filter, c clear-filter, k/j scroll, q close]" % (self.level, self.pattern))]
        for r in recs[start:end]:
            lines.append(format_record(r))
        return lines

    def handle_key(self, key, raw):
        levels = {"d": "debug", "i": "info", "w": "warning", "e": "error"}
        if key in levels:
            self.level = levels[key]
        elif key in ("k", "Up"):
            self.off += 1
        elif key in ("j", "Down"):
            self.off = max(0, self.off - 1)
        elif key == "PageUp":
            self.off += self.screen.rows - 2
        elif key == "PageDown":
            self.off = max(0, self.off - (self.screen.rows - 2))
        elif key in ("G", "End"):
            self.off = 0
        elif key == "c":
            self.pattern = ""
        elif key == "/":
            self._wm.prompt.open("", "custom", "/", on_submit=self._set_pattern)
        elif key == "q":
            self._wm.close_window(self.id)
        else:
            return False
        self.dirty = True
        return True

    def _set_pattern(self, text):
        self.pattern = text
        self.dirty = True

    def scroll(self, dy=0, dx=0):
        self.off = max(0, self.off + dy)
        self.dirty = True


# ---------------------------------------------------------------------------- status viewer
class StatusWindow(InternalWindow):
    kind = "status"
    refresh_interval = 0.5

    def __init__(self, wid, title, rows, cols, wm=None, **opts):
        super().__init__(wid, title or "status", rows, cols, **opts)
        self._wm = wm

    def render(self, cols, rows):
        wm = self._wm
        now = time.time()
        lines = [bold("Status items")]
        if not wm.status_items:
            lines.append(dim("  (none - set with: status-set KEY VALUE)"))
        for k, it in sorted(wm.status_items.items()):
            age = now - it["time"]
            col = {"ok": 2, "warn": 3, "err": 1, "accent": 6}.get(it.get("style"))
            v = str(it["value"])
            lines.append("  %-16s %s %s" % (k, ansi_color(v, col, bold=bool(col)) if col is not None else v,
                                            dim("(%s ago)" % human_time(age))))
        lines.append("")
        lines.append(bold("Progress"))
        if not wm.progress:
            lines.append(dim("  (none - pipe data through: pytermwm pv)"))
        for name, t in sorted(wm.progress.items()):
            total = t.get("total") or 0
            cur = t.get("current", 0)
            w = max(8, min(30, cols - 40))
            if total:
                frac = cur / total
                lines.append("  %-14s %s %3.0f%% %s/%s %s" % (
                    (t.get("label") or name)[:14], ansi_color(bar(frac, w, glyphs=wm.chart_glyphs()), 2 if t.get("done") else 6),
                    frac * 100, human_bytes(cur), human_bytes(total), human_rate(t["rate"]) if t.get("rate") else ""))
            else:
                lines.append("  %-14s %s %s" % ((t.get("label") or name)[:14], human_bytes(cur),
                                                 human_rate(t["rate"]) if t.get("rate") else ""))
        lines.append("")
        lines.append(bold("Session"))
        lines.append("  windows: %d   desktops: %d   theme: %s   uptime: %s" % (
            len(wm.windows), len(wm.desktops), wm.theme.name, human_time(now - wm.start_time)))
        return lines


# ---------------------------------------------------------------------------- viewer
class ViewerWindow(InternalWindow):
    """stdin / web input viewer: text is pushed in (pipe, HTTP, CLI); view can be paused, filtered, highlighted."""
    kind = "viewer"
    refresh_interval = None

    def __init__(self, wid, title, rows, cols, wm=None, max_lines=10000, **opts):
        super().__init__(wid, title or "viewer", rows, cols, **opts)
        self._wm = wm
        self.buf: Deque[str] = collections.deque(maxlen=max_lines)
        self._partial = ""
        self.paused = False
        self.follow = True
        self.wrap = True
        self.numbers = False
        self.filter_re: Optional[re.Pattern] = None
        self.filter_text = ""
        self.invert = False
        self.highlight: List[re.Pattern] = []
        self.off = 0                     # lines from bottom
        self.total = 0
        self.dropped_while_paused = 0
        self._pause_total: Optional[int] = None
        self.source_name = ""

    # -- data in
    def feed_data(self, text: str):
        text = text.replace("\r\n", "\n")
        data = self._partial + text
        parts = data.split("\n")
        self._partial = parts.pop()
        for p in parts:
            self.buf.append(p)
            self.total += 1
            if self.paused:
                self.dropped_while_paused += 1
        self.last_output = time.time()
        self.output_bytes += len(text)
        if self._wm:
            self._wm.window_output(self, text, "out", text.encode())
        if not self.paused:
            if self.off and not self.follow:
                self.off += len(parts)
            self.dirty = True

    def feed_bytes(self, data: bytes, stream: str = "out"):
        self.feed_data(data.decode("utf-8", "replace"))

    def feed_text(self, text, stream="out", nbytes=None, raw=None):
        self.feed_data(text)

    def write_input(self, data: bytes):
        # keyboard input into a viewer means: treat as pushing text (useful for scripts / send-keys)
        self.feed_data(data.decode("utf-8", "replace"))

    # -- config
    def set_filter(self, pattern: str = "", invert: bool = False):
        self.filter_text = pattern
        self.invert = invert
        if pattern:
            try:
                self.filter_re = re.compile(pattern, re.I)
            except re.error:
                self.filter_re = re.compile(re.escape(pattern), re.I)
        else:
            self.filter_re = None
        self.dirty = True

    def set_highlight(self, patterns: List[str]):
        self.highlight = []
        for p in patterns:
            try:
                self.highlight.append(re.compile(p, re.I))
            except re.error:
                self.highlight.append(re.compile(re.escape(p), re.I))
        self.dirty = True

    def clear(self):
        self.buf.clear()
        self.off = 0
        self.dirty = True

    def option(self, name: str, value: str):
        v = str(value).lower() in ("1", "true", "yes", "on", "toggle")
        if name == "pause":
            self.paused = (not self.paused) if value == "toggle" else v
            if self.paused:
                self._pause_total = self.total       # the view is frozen at this line
            else:
                self._pause_total = None
                self.dropped_while_paused = 0
        elif name == "follow":
            self.follow = (not self.follow) if value == "toggle" else v
            if self.follow:
                self.off = 0
        elif name == "wrap":
            self.wrap = (not self.wrap) if value == "toggle" else v
        elif name == "numbers":
            self.numbers = (not self.numbers) if value == "toggle" else v
        elif name == "filter":
            self.set_filter("" if value in ("", "off", "none") else value)
        elif name == "exclude":
            self.set_filter(value, invert=True)
        elif name == "highlight":
            self.set_highlight([value] if value not in ("", "off") else [])
        elif name == "clear":
            self.clear()
        else:
            raise CommandError("unknown viewer option: %s" % name)
        self.dirty = True

    # -- rendering
    def _visible_lines(self):
        out = []
        for i, l in enumerate(self.buf):
            if self.filter_re is not None:
                m = self.filter_re.search(strip_ansi(l))
                if bool(m) == self.invert:
                    continue
            no = self.total - len(self.buf) + i + 1
            if self.paused and self._pause_total is not None and no > self._pause_total:
                continue
            out.append((no, l))
        return out

    def render(self, cols, rows):
        lines = self._visible_lines()
        body_rows = rows - 1
        text_w = cols - (len(str(self.total)) + 1 if self.numbers else 0)
        out_rows: List[str] = []
        end = len(lines) - self.off
        i = end - 1
        acc: List[List[str]] = []
        used = 0
        while i >= 0 and used < body_rows:
            no, l = lines[i]
            seg = self._render_line(l, text_w)
            if self.numbers:
                seg = [(dim(str(no).rjust(len(str(self.total)))) + " " if k == 0 else " " * (len(str(self.total)) + 1)) + s
                       for k, s in enumerate(seg)]
            acc.append(seg)
            used += len(seg)
            i -= 1
        rows_out = []
        for seg in reversed(acc):
            rows_out.extend(seg)
        rows_out = rows_out[-body_rows:]
        flags = []
        if self.paused:
            flags.append("PAUSED(+%d)" % self.dropped_while_paused)
        if self.filter_re is not None:
            flags.append("%sfilter=%r" % ("!" if self.invert else "", self.filter_text))
        if not self.follow:
            flags.append("nofollow")
        flags.append("%d lines" % len(lines))
        status = dim("[p]ause [f]ollow [/]filter [w]rap [n]um [c]lear | " + " ".join(flags))
        return rows_out + [""] * (body_rows - len(rows_out)) + [status]

    def _render_line(self, line: str, width: int) -> List[str]:
        if self.highlight:
            plain = strip_ansi(line)
            if any(h.search(plain) for h in self.highlight):
                line = "\x1b[7m" + plain + "\x1b[27m"
        vis = visible_len(line)
        if not self.wrap or vis <= width or width <= 0:
            return [line if self.wrap else self._clip(line, width)]
        # wrap on plain text boundaries (keeps escapes intact by re-feeding the line with autowrap)
        return [line]

    @staticmethod
    def _clip(line, width):
        out, w, i = "", 0, 0
        while i < len(line):
            m = _ANSI.match(line, i)
            if m:
                out += m.group()
                i = m.end()
                continue
            cw = str_width(line[i])
            if w + cw > width:
                break
            out += line[i]
            w += cw
            i += 1
        return out + "\x1b[0m"

    def refresh(self):
        # wrapping is done by the emulator (autowrap) => rows of wrapped lines counted approximately
        super().refresh()

    def handle_key(self, key, raw):
        if key == "p":
            self.option("pause", "toggle")
        elif key == "f":
            self.option("follow", "toggle")
        elif key == "w":
            self.option("wrap", "toggle")
        elif key == "n":
            self.option("numbers", "toggle")
        elif key == "c":
            self.clear()
        elif key == "/":
            self._wm.prompt.open(self.filter_text, "custom", "/", on_submit=lambda t: self.set_filter(t))
        elif key == "!":
            self._wm.prompt.open("", "custom", "!", on_submit=lambda t: self.set_filter(t, invert=True))
        elif key in ("k", "Up"):
            self.off += 1
            self.follow = False
        elif key in ("j", "Down"):
            self.off = max(0, self.off - 1)
        elif key == "PageUp":
            self.off += self.screen.rows - 2
            self.follow = False
        elif key == "PageDown":
            self.off = max(0, self.off - (self.screen.rows - 2))
        elif key in ("G", "End"):
            self.off = 0
            self.follow = True
        elif key in ("g", "Home"):
            self.off = max(0, len(self._visible_lines()) - (self.screen.rows - 1))
            self.follow = False
        elif key == "q":
            self._wm.close_window(self.id)
        else:
            return False
        self.dirty = True
        return True

    def scroll(self, dy=0, dx=0):
        self.off = max(0, self.off + dy)
        if dy > 0:
            self.follow = False
        self.dirty = True

    def text(self, history=False):
        return "\n".join(strip_ansi(l) for _n, l in self._visible_lines())


# ---------------------------------------------------------------------------- directory watcher
class DirWatchWindow(InternalWindow):
    kind = "dirwatch"
    refresh_interval = 0.5

    def __init__(self, wid, title, rows, cols, wm=None, path=".", recursive=True, interval=1.0, ignore=None, **opts):
        path = os.path.abspath(os.path.expanduser(path))
        super().__init__(wid, title or "watch %s" % path, rows, cols, **opts)
        self._wm = wm
        self.path = path
        self.recursive = recursive
        self.interval = interval
        self.ignore = list(ignore or [".git", "__pycache__", "*.pyc", "*.swp"])
        self.events: Deque[tuple] = collections.deque(maxlen=1000)
        self.snapshot: Dict[str, tuple] = {}
        self._last_scan = 0.0
        self.counts = {"created": 0, "modified": 0, "deleted": 0}
        self.snapshot = self._scan()
        self.scanned = True

    def _ignored(self, name):
        return any(fnmatch.fnmatch(name, p) for p in self.ignore)

    def _scan(self) -> Dict[str, tuple]:
        snap = {}
        base = self.path
        try:
            if os.path.isfile(base):
                st = os.stat(base)
                return {base: (st.st_mtime, st.st_size, False)}
            stack = [base]
            n = 0
            while stack and n < 20000:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            if self._ignored(e.name):
                                continue
                            try:
                                st = e.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            isdir = e.is_dir(follow_symlinks=False)
                            snap[e.path] = (st.st_mtime, st.st_size, isdir)
                            n += 1
                            if isdir and self.recursive:
                                stack.append(e.path)
                except OSError:
                    continue
        except OSError:
            pass
        return snap

    def poll(self):
        new = self._scan()
        old = self.snapshot
        now = time.time()
        changes = []
        for p, v in new.items():
            if p not in old:
                changes.append(("created", p, v))
            elif old[p][:2] != v[:2]:
                changes.append(("modified", p, v))
        for p in old:
            if p not in new:
                changes.append(("deleted", p, old[p]))
        for kind, p, v in sorted(changes, key=lambda c: c[1]):
            self.events.append((now, kind, os.path.relpath(p, self.path) if p != self.path else p, v[1]))
            self.counts[kind] += 1
            if self._wm:
                self._wm.emit("dirwatch", window=self, kind=kind, path=p)
                self._wm.window_output(self, "%s %s\n" % (kind, p), "out", ("%s %s\n" % (kind, p)).encode())
        self.snapshot = new
        if changes:
            self.last_output = now
            self.dirty = True

    def on_tick(self, now):
        if now - self._last_scan >= self.interval:
            self._last_scan = now
            self.poll()
        super().on_tick(now)

    def render(self, cols, rows):
        files = [v for v in self.snapshot.values() if not v[2]]
        total = sum(v[1] for v in files)
        lines = [bold(self.path), dim("%d files, %d dirs, %s   +%d ~%d -%d" % (
            len(files), len(self.snapshot) - len(files), human_bytes(total),
            self.counts["created"], self.counts["modified"], self.counts["deleted"]))]
        colors = {"created": 2, "modified": 3, "deleted": 1}
        n = rows - len(lines)
        for t, kind, p, size in list(self.events)[-n:]:
            lines.append("%s %s %s %s" % (dim(time.strftime("%H:%M:%S", time.localtime(t))),
                                          ansi_color("%-8s" % kind, colors[kind]), p, dim(human_bytes(size))))
        if not self.events:
            lines.append(dim("  waiting for changes..."))
        return lines


# ---------------------------------------------------------------------------- charts
class ChartWindow(InternalWindow):
    """Chart of a metric over time. source: cpu|mem|load|net_rx|net_tx|disk | {status: key} | cmd | file | push."""
    kind = "chart"
    refresh_interval = 0.5

    def __init__(self, wid, title, rows, cols, wm=None, source="cpu", chart="line", interval=1.0, cmd=None,
                 path=None, lo=None, hi=None, unit="", color=None, **opts):
        super().__init__(wid, title or "chart %s" % source, rows, cols, **opts)
        self._wm = wm
        self.metric = source
        self.chart = chart
        self.interval = float(interval)
        self.cmd = cmd
        self.path = path
        self.lo, self.hi = lo, hi
        self.unit = unit
        self.data: Deque[float] = collections.deque(maxlen=1200)
        self.color = color
        self._last = 0.0
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        if source == "cmd" and cmd:
            self._start_thread()

    def push(self, v: float):
        with self._lock:
            self.data.append(float(v))
        self.dirty = True

    def _start_thread(self):
        def run():
            while not self._stop:
                try:
                    out = subprocess.run(self.cmd, shell=True, capture_output=True, text=True, timeout=10).stdout
                    m = re.findall(r"-?\d+(?:\.\d+)?", out)
                    if m:
                        self.push(float(m[-1]))
                except Exception:
                    pass
                for _ in range(int(self.interval * 10) or 1):
                    if self._stop:
                        return
                    time.sleep(0.1)
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        super().close()

    def sample(self):
        s = self.metric
        if s == "cpu":
            return SAMPLER.cpu()[0]
        if s == "mem":
            m = SAMPLER.mem()
            return 100.0 * m["used"] / m["total"] if m["total"] else 0
        if s == "load":
            return SAMPLER.load()[0]
        if s == "net_rx":
            return SAMPLER.net()[0] / 1024.0
        if s == "net_tx":
            return SAMPLER.net()[1] / 1024.0
        if s == "disk":
            t, u, f = SAMPLER.disk(self.path or "/")
            return 100.0 * u / t if t else 0
        if s == "status" and self._wm:
            it = self._wm.status_items.get(self.path or "")
            if it:
                try:
                    return float(it["value"])
                except (TypeError, ValueError):
                    return None
        if s == "file" and self.path:
            try:
                with open(os.path.expanduser(self.path), encoding="utf-8") as f:
                    txt = f.read()[-4000:]
                m = re.findall(r"-?\d+(?:\.\d+)?", txt)
                return float(m[-1]) if m else None
            except OSError:
                return None
        return None

    def on_tick(self, now):
        if self.metric not in ("push", "cmd") and now - self._last >= self.interval:
            self._last = now
            v = self.sample()
            if v is not None:
                self.push(v)
        super().on_tick(now)

    def render(self, cols, rows):
        with self._lock:
            vals = list(self.data)
        cur = vals[-1] if vals else 0
        lo = self.lo if self.lo is not None else (0 if self.metric in ("cpu", "mem", "disk") else None)
        hi = self.hi if self.hi is not None else (100 if self.metric in ("cpu", "mem", "disk") else None)
        head = "%s  now %.1f%s  min %.1f  max %.1f  avg %.1f" % (
            self.metric, cur, self.unit or ("%" if self.metric in ("cpu", "mem", "disk") else ""),
            min(vals) if vals else 0, max(vals) if vals else 0, sum(vals) / len(vals) if vals else 0)
        lines = [bold(head)]
        h = rows - 1
        col = self.color
        glyphs = self._wm.chart_glyphs() if self._wm else "unicode"
        if self.chart == "gauge":
            lines.append(gauge(cur, max(10, cols - 4)))            # the percentage text always makes the level clear
        elif self.chart == "spark":
            lines.append(spark(vals, cols, lo, hi, glyphs=glyphs))
        elif self.chart == "bar":
            for r in column_chart(vals[-cols:], h, lo, hi, glyphs=glyphs):
                lines.append(r)
        elif self.chart == "hist":
            lines.extend(histogram(vals, min(h, 10), max(10, cols - 14)))
        else:
            ch = line_chart(vals[-cols * 2:], cols, h, lo, hi, axis=True, fill=(self.chart == "area"))
            for r in ch:
                if col is not None:
                    r = ansi_color(r, col)
                else:
                    r = ansi_color(r, color_ramp(cur / 100.0) if hi == 100 else (100, 200, 255))
                lines.append(r)
        return lines


# ---------------------------------------------------------------------------- debug console
class DebugWindow(InternalWindow):
    """Python console + event tracer + state inspector."""
    kind = "debug"
    refresh_interval = 0.5
    MODES = ("console", "events", "state")

    def __init__(self, wid, title, rows, cols, wm=None, mode="console", **opts):
        super().__init__(wid, title or "debug", rows, cols, **opts)
        self._wm = wm
        self.mode = mode if mode in self.MODES else "console"
        self.output: List[str] = ["pytermwm debug console - `wm` is the WindowManager; Tab switches view."]
        self.editor = LineEditor("")
        self.interp = code.InteractiveInterpreter({"wm": wm, "__name__": "__pytermwm__"})
        self.more = False
        self._buffer: List[str] = []
        self.off = 0
        self.history: List[str] = []
        self.editor.history = self.history
        if wm:
            wm.trace_events = True

    def close(self):
        if self._wm and not any(isinstance(w, DebugWindow) and w is not self for w in self._wm.windows.values()):
            self._wm.trace_events = False
        super().close()

    def _run(self, line: str):
        self.output.append(("... " if self.more else ">>> ") + line)
        self._buffer.append(line)
        src = "\n".join(self._buffer)
        self.interp.locals["w"] = self._wm.focused if self._wm else None
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            try:
                more = self.interp.runsource(src, "<console>")
            except SystemExit:
                more = False
                buf_err.write("SystemExit ignored\n")
        self.more = more
        if not more:
            self._buffer = []
        for chunk in (buf_out.getvalue(), buf_err.getvalue()):
            if chunk:
                self.output.extend(chunk.rstrip("\n").split("\n"))
        del self.output[:-2000]

    def render(self, cols, rows):
        head = " ".join((bold("[%s]" % m) if m == self.mode else dim(m)) for m in self.MODES)
        lines = [head + dim("   (Tab switches view)")]
        body = rows - 1
        if self.mode == "console":
            out = self.output[-(body - 1):] if body > 1 else []
            lines.extend(out)
            lines.extend([""] * (body - 1 - len(out)))
            lines.append(("... " if self.more else ">>> ") + self.editor.text)
        elif self.mode == "events":
            tr = self._wm.trace if self._wm else []
            sel = tr[-(body + self.off):len(tr) - self.off if self.off else None]
            for e in sel[-body:]:
                lines.append("%s %-16s %s" % (dim(time.strftime("%H:%M:%S", time.localtime(e["time"]))), e["event"],
                                               " ".join("%s=%s" % kv for kv in e["data"].items())))
        else:
            st = json.dumps(self._wm.state(), indent=1, default=str).split("\n") if self._wm else []
            lines.extend(st[self.off:self.off + body])
        return lines

    def handle_key(self, key, raw):
        if key == "Tab":
            self.mode = self.MODES[(self.MODES.index(self.mode) + 1) % len(self.MODES)]
            self.off = 0
        elif key == "S-Tab":
            self.mode = self.MODES[(self.MODES.index(self.mode) - 1) % len(self.MODES)]
            self.off = 0
        elif self.mode == "console":
            r = self.editor.handle(key)
            if r == "submit":
                line = self.editor.text
                self.editor.set("")
                if line.strip() and (not self.history or self.history[-1] != line):
                    self.history.append(line)
                self._run(line)
            elif r == "cancel":
                self.editor.set("")
                self._buffer = []
                self.more = False
        else:
            if key in ("k", "Up"):
                self.off += 1
            elif key in ("j", "Down"):
                self.off = max(0, self.off - 1)
            elif key == "PageUp":
                self.off += 10
            elif key == "PageDown":
                self.off = max(0, self.off - 10)
            elif key == "q":
                self._wm.close_window(self.id)
            else:
                return False
        self.dirty = True
        return True

    def write_input(self, data: bytes):
        # text sent with send-keys is typed into the console line
        for ch in data.decode("utf-8", "replace"):
            if ch in "\r\n":
                self.handle_key("Enter", b"\r")
            else:
                self.editor.insert(ch)
        self.dirty = True


# ---------------------------------------------------------------------------- factories
def register_all(wm):
    def text_factory(wm_, wid, spec, rows, cols, opts):
        return TextWindow(wid, spec.get("title") or "text", rows, cols, spec.get("text", ""), name=spec.get("name"), **opts)

    def make(cls, **extra_keys):
        def factory(wm_, wid, spec, rows, cols, opts):
            kw = {k: spec[k] for k in extra_keys if k in spec}
            for k in extra_keys:
                if k not in kw and extra_keys[k] is not None:
                    kw[k] = extra_keys[k]
            return cls(wid, spec.get("title") or "", rows, cols, wm=wm_, name=spec.get("name"), **kw, **opts)
        return factory

    wm.register_window_kind("text", text_factory)
    wm.register_window_kind("help", make(HelpWindow, topic="index"))
    wm.register_window_kind("log", make(LogWindow, level="info", pattern=""))
    wm.register_window_kind("status", make(StatusWindow))
    wm.register_window_kind("viewer", make(ViewerWindow, max_lines=10000))
    wm.register_window_kind("dirwatch", make(DirWatchWindow, path=".", recursive=True, interval=1.0, ignore=None))
    wm.register_window_kind("chart", make(ChartWindow, source="cpu", chart="line", interval=1.0, cmd=None, path=None,
                                          lo=None, hi=None, unit="", color=None))
    wm.register_window_kind("debug", make(DebugWindow, mode="console"))
