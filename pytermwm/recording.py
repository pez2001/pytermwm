"""Session recording in the asciicast v2 format (https://docs.asciinema.org/manual/asciicast/v2/) and its replay.

``record`` writes what a window displays (output only unless input capture is asked for: that captures passwords too).
``cast`` windows replay any asciicast v2 file, including ones made by asciinema.
"""
from __future__ import annotations

import codecs
import json
import math
import os
import time
from typing import List, Optional, Tuple

from .commands import CommandError
from .window import Window


class CastRecorder:
    def __init__(self, path: str, cols: int, rows: int, title: str = "", capture_input: bool = False,
                 env: Optional[dict] = None):
        self.path = path
        self.capture_input = capture_input
        self.t0 = time.time()
        self.events = 0
        self.closed = False
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)   # may hold passwords (-i)
        self.f = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        self.error = None
        header = {"version": 2, "width": int(cols), "height": int(rows), "timestamp": int(self.t0)}
        if title:
            header["title"] = title
        if env:
            header["env"] = env
        self.f.write(json.dumps(header, ensure_ascii=False) + "\n")
        self.f.flush()

    def _write(self, kind: str, data: str):
        if self.closed:
            return
        try:
            self.f.write(json.dumps([round(time.time() - self.t0, 6), kind, data], ensure_ascii=False) + "\n")
            self.f.flush()
            self.events += 1
        except (OSError, ValueError) as e:       # disk full, file removed ...: stop recording, never disturb the window
            self.error = str(e)
            self.close()

    def output(self, text: str):
        if text:
            self._write("o", text)

    def input(self, data: bytes):
        if self.capture_input and data:
            self._write("i", data.decode("utf-8", "replace"))

    def resize(self, cols: int, rows: int):
        self._write("r", "%dx%d" % (cols, rows))

    def marker(self, label: str = ""):
        self._write("m", label)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.f.close()
            except OSError:
                pass

    def describe(self) -> dict:
        return {"path": self.path, "events": self.events, "seconds": round(time.time() - self.t0, 1),
                "input": self.capture_input, "error": self.error}


def default_path(session: str, window) -> str:
    from . import protocol as P
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (window.name or ("w%d" % window.id)))
    return os.path.join(P.state_dir(), "recordings", "%s-%s-%s.cast" % (session or "session", label, stamp))


def start(wm, window, path: Optional[str] = None, capture_input: bool = False, overwrite: bool = False) -> CastRecorder:
    if getattr(window, "recorder", None) is not None:
        raise CommandError("window %s is already being recorded to %s" % (window.id, window.recorder.path))
    path = os.path.abspath(os.path.expanduser(path)) if path else default_path(getattr(wm, "session_name", ""), window)
    if os.path.exists(path) and not overwrite:
        raise CommandError("%s already exists (use record -f to overwrite it)" % path)
    cols, rows = window.screen.cols, window.screen.rows
    rec = CastRecorder(path, cols, rows, title=window.title, capture_input=capture_input,
                       env={"TERM": "xterm-256color", "SHELL": ""})
    window.recorder = rec
    # start from what is on screen now so the recording is self-contained
    snap = window.text()
    if snap.strip():
        rec.output("\x1b[H\x1b[2J" + snap.replace("\n", "\r\n"))
    return rec


def stop(window) -> Optional[dict]:
    rec = getattr(window, "recorder", None)
    if rec is None:
        return None
    window.recorder = None
    info = rec.describe()
    rec.close()
    return info


# ------------------------------------------------------------------------------------------------ reading / replay
class CastError(Exception):
    pass


def read_cast(path: str) -> Tuple[dict, List[Tuple[float, str, str]]]:
    """Parse an asciicast v2 file into (header, [(time, kind, data)])."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise CastError("cannot read %s: %s" % (path, e))
    if not lines:
        raise CastError("%s is empty" % path)
    try:
        header = json.loads(lines[0])
    except ValueError:
        raise CastError("%s: the first line is not a JSON header" % path)
    if not isinstance(header, dict) or header.get("version") != 2:
        raise CastError("%s: only asciicast v2 files are supported" % path)
    events = []
    last = 0.0
    for n, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        try:
            t, kind, data = json.loads(line)
            t = float(t)
        except (ValueError, TypeError):
            raise CastError("%s: line %d is not an event" % (path, n))
        if not math.isfinite(t):
            raise CastError("%s: line %d has an invalid time" % (path, n))
        t = max(t, last)                   # keep the timeline monotonic
        last = t
        events.append((t, str(kind), str(data)))
    for key in ("width", "height"):
        try:
            header[key] = int(header.get(key) or 0)
        except (ValueError, TypeError, OverflowError):
            raise CastError("%s: header %r is not a number" % (path, key))
    if "idle_time_limit" in header:
        try:
            header["idle_time_limit"] = float(header["idle_time_limit"])
        except (ValueError, TypeError):
            raise CastError("%s: header 'idle_time_limit' is not a number" % path)
    return header, events


MAX_COLS, MAX_ROWS = 1000, 500


def clamp_size(cols, rows) -> Tuple[int, int]:
    return max(1, min(int(cols), MAX_COLS)), max(1, min(int(rows), MAX_ROWS))


def compress_idle(events, limit: float):
    """Cap gaps between events at ``limit`` seconds (players do this so long pauses do not stall a replay)."""
    if not limit or limit <= 0:
        return list(events)
    out, shift, prev = [], 0.0, 0.0
    for t, k, d in events:
        gap = t - prev
        if gap > limit:
            shift += gap - limit
        prev = t
        out.append((t - shift, k, d))
    return out


class CastWindow(Window):
    """Plays an asciicast file.  Keys: Space pause, + / - speed, r restart, Right/Left skip 5 s, q close."""
    kind = "cast"
    emits_output = False               # replayed text must not trigger rules or activity marks
    def __init__(self, wid, title="", rows=24, cols=80, wm=None, path="", speed=1.0, idle=2.0, loop=False, autoplay=True,
                 **opts):
        opts.setdefault("history", 0)
        opts.setdefault("readonly", True)
        super().__init__(wid, title, rows, cols, **opts)
        self._wm = wm
        self.path = os.path.expanduser(str(path))
        header, events = read_cast(self.path)
        self.header = header
        try:
            limit = float((idle if idle is not None else header.get("idle_time_limit", 2.0)) or 0)
        except (ValueError, TypeError):
            raise CastError("idle must be a number")
        if not math.isfinite(limit):
            limit = 0.0
        self.events = compress_idle([e for e in events if e[1] in ("o", "r")], limit)
        self.duration = self.events[-1][0] if self.events else 0.0
        self.speed = max(0.1, float(speed or 1.0))
        self.loop = bool(loop)
        self.paused = not autoplay
        self.pos = 0
        self.clock = 0.0                   # position in the recording, seconds
        self._last = None
        self.base_title = title or header.get("title") or os.path.basename(self.path)
        self.source = None
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.tty_size = clamp_size(header.get("width") or cols, header.get("height") or rows)
        self.set_vsize(self.tty_size)
        self._retitle(force=True)

    # ------------------------------------------------------------ playback
    def restart(self):
        self.screen.feed("\x1bc")
        self.set_vsize(self.tty_size)
        self.pos = 0
        self.clock = 0.0
        self._last = None
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")

    def _apply(self, kind: str, data: str):
        if kind == "o":
            self.feed_text(data)
        elif kind == "r":
            try:
                c, _, r = data.partition("x")
                self.set_vsize(clamp_size(c, r))
            except (ValueError, OverflowError):
                pass

    def seek(self, seconds: float):
        seconds = max(0.0, min(seconds, self.duration))
        if seconds < self.clock:
            self.restart()
        self.clock = seconds
        self._advance()

    def _advance(self):
        while self.pos < len(self.events) and self.events[self.pos][0] <= self.clock:
            _, kind, data = self.events[self.pos]
            self._apply(kind, data)
            self.pos += 1

    def on_tick(self, now: float):
        if self._last is None:
            self._last = now
        dt = now - self._last
        self._last = now
        if not self.paused and self.pos < len(self.events):
            self.clock += dt * self.speed
            self._advance()
        elif not self.paused and self.loop and self.pos >= len(self.events):
            self.restart()
        self._retitle()

    def _retitle(self, force=False):
        state = "paused" if self.paused else ("done" if self.pos >= len(self.events) else "playing")
        t = "%s [%s %d/%ds x%g]" % (self.base_title, state, int(self.clock), int(self.duration), self.speed)
        if force or t != self.title_text:
            self.title_text = t
            if self._wm:
                self._wm.dirty = True

    def handle_key(self, key, raw):
        if key in ("Space", " ", "p"):
            self.paused = not self.paused
        elif key in ("+", "="):
            self.speed = min(32.0, self.speed * 2)
        elif key in ("-", "_"):
            self.speed = max(0.25, self.speed / 2)
        elif key == "r":
            self.restart()
        elif key == "Right":
            self.seek(self.clock + 5)
        elif key == "Left":
            self.seek(self.clock - 5)
        elif key == "q" and self._wm:
            self._wm.close_window(self.id)
        else:
            return False
        self._retitle(force=True)
        return True

    def write_input(self, data: bytes):
        pass

    def describe(self) -> dict:
        d = super().describe()
        d["cast"] = {"path": self.path, "duration": self.duration, "position": round(self.clock, 1), "speed": self.speed,
                     "paused": self.paused}
        return d


def register(wm):
    def factory(wm_, wid, spec, rows, cols, opts):
        for k in ("path",):
            if not spec.get(k):
                raise CommandError("cast windows need path: FILE.cast")
        try:
            return CastWindow(wid, spec.get("title") or "", rows, cols, wm=wm_, name=spec.get("name"), path=spec["path"],
                              speed=spec.get("speed", 1.0), idle=spec.get("idle", 2.0), loop=spec.get("loop", False),
                              autoplay=spec.get("autoplay", True), **opts)
        except CastError as e:
            raise CommandError(str(e))
        except (ValueError, TypeError, OverflowError) as e:
            raise CommandError("cannot play %s: %s" % (spec["path"], e))
    wm.register_window_kind("cast", factory)
