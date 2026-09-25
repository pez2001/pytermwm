#!/usr/bin/env python3
"""Make the README's screenshots (SVG) and asciicast v2 recordings automatically, from a scripted headless session.

    python3 scripts/make_media.py                 # everything into docs/media/
    python3 scripts/make_media.py --out /tmp/m    # somewhere else
    python3 scripts/make_media.py --only themes   # themes | palette | effect | cast (repeatable)

Nothing is needed but a POSIX shell for the terminal windows. The recordings use a virtual clock (typing speed and
pauses are part of the script), so they come out the same length on a slow or a fast machine; ``asciinema play
docs/media/demo.cast`` or ``pytermwm replay docs/media/demo.cast`` plays one back.
"""
import argparse
import math
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="ptw-media-")
os.environ.update(PYTERMWM_RUNTIME_DIR=os.path.join(_TMP, "run"), PYTERMWM_STATE_DIR=os.path.join(_TMP, "state"),
                  HOME=_TMP, SHELL="/bin/sh", PS1="$ ", TERM="xterm-256color", LANG="C.UTF-8")
os.makedirs(os.environ["PYTERMWM_RUNTIME_DIR"], exist_ok=True)

from pytermwm import screenshot as S  # noqa: E402
from pytermwm.keys import KeyParser  # noqa: E402
from pytermwm.theme import BUILTIN  # noqa: E402
from pytermwm.wm import WindowManager  # noqa: E402

COLS, ROWS = 110, 32
NOTES = """pytermwm - a terminal window manager

  M-Enter      new window
  M-h/j/k/l    move the focus
  M-Space      next layout
  M-p          command palette
  C-b :        prompt in the status line
  C-b d        detach (the session keeps running)

Drive it from the keyboard, the CLI, HTTP
or MCP: one command registry for all."""


# ------------------------------------------------------------------------------------------------ session plumbing
class Director:
    """A headless window manager plus a virtual clock for recordings."""

    def __init__(self, cols=COLS, rows=ROWS):
        self.wm = WindowManager(cols, rows, config={"autosave": 0})
        self.parser = KeyParser()
        self.rec = None
        self.t = 0.0
        self.chart = None
        self.phase = 0.0

    def pump(self, seconds=0.3):
        """Let the windows' processes run for a while (real time), like the server loop does."""
        import select
        wm = self.wm
        end = time.time() + seconds
        while time.time() < end:
            fds = {fd: (w, stream) for fd, w, stream in wm.iter_read_fds()}
            r = select.select(list(fds), [], [], 0.02)[0] if fds else []
            for fd in r:
                w, stream = fds[fd]
                wm.source_readable(w, fd, stream)
            wm.poll_sources()
            wm.tick(time.time())

    def feed_chart(self, n=1):
        for _ in range(n):
            if self.chart is not None and self.chart.id in self.wm.windows:
                self.phase += 0.35
                self.chart.push(50 + 30 * math.sin(self.phase) + 12 * math.sin(self.phase * 2.7))

    def frame(self, hold=0.0):
        """Record the current screen, then let ``hold`` seconds of recording time pass."""
        self.feed_chart()
        if self.rec is not None:
            self.rec.frame(S.compose(self.wm), self.t)
        self.t += hold

    def keys(self, data, pause=0.0):
        evs = self.parser.feed(data.encode() if isinstance(data, str) else data) + self.parser.flush()
        self.wm.process_events(evs)
        self.pump(0.05)
        self.frame(pause)

    def type(self, text, cps=14.0, pause=0.4):
        for ch in text:
            self.keys(ch, 1.0 / cps)
        self.t += pause

    def run(self, line, pause=0.8, settle=0.3):
        self.wm.run_command_line(line, source="script")
        self.pump(settle)
        self.frame(pause)

    def shot(self, path, title=None):
        self.pump(0.2)
        S.screenshot(self.wm, path, title=title)
        print("wrote", os.path.relpath(path, ROOT))

    def close(self):
        if self.rec is not None:
            self.rec.close()
        self.wm.shutdown()


def workspace(d: Director):
    """Three windows: a shell, a chart and a text window, tiled."""
    wm = d.wm
    sh = wm.create_window({"cmd": "/bin/sh", "title": "shell", "cwd": ROOT})
    d.pump(0.3)
    sh.write_input(b"ls docs\r")
    d.chart = wm.create_window({"kind": "chart", "source": "push", "title": "load", "unit": "%", "lo": 0, "hi": 100})
    d.feed_chart(60)
    wm.create_window({"kind": "text", "title": "notes", "text": NOTES})
    wm.focus_window(sh.id)
    d.pump(0.6)
    return sh


# ------------------------------------------------------------------------------------------------ scenes
def themes(out):
    d = Director()
    try:
        workspace(d)
        for name in BUILTIN:
            d.wm.run_command_line("theme " + name, source="script")
            d.shot(os.path.join(out, "theme-%s.svg" % name), "pytermwm - theme %s" % name)
    finally:
        d.close()


def palette(out):
    d = Director()
    try:
        workspace(d)
        d.wm.run_command_line("theme modern", source="script")
        d.keys("\x1bp")                               # M-p
        d.keys("lay")
        d.shot(os.path.join(out, "palette.svg"), "pytermwm - command palette")
    finally:
        d.close()


def effect(out):
    d = Director()
    try:
        wm = d.wm
        wm.run_command_line("theme matrix", source="script")
        wm.run_command_line("effect matrix", source="script")
        wm.create_window({"kind": "text", "title": "follow the white rabbit", "text": NOTES, "floating": True})
        wm.run_command_line("layout float", source="script")
        for _ in range(12):                           # let the rain fill the screen
            d.pump(0.1)
            S.compose(wm)
        d.shot(os.path.join(out, "effect-matrix.svg"), "pytermwm - theme matrix + effect matrix")
    finally:
        d.close()


def cast(out):
    d = Director()
    path = os.path.join(out, "demo.cast")
    try:
        wm = d.wm
        d.rec = S.ScreenRecorder(path, wm.cols, wm.rows, title="pytermwm demo")
        sh = wm.create_window({"cmd": "/bin/sh", "title": "shell", "cwd": ROOT})
        d.pump(0.4)
        d.frame(1.0)
        d.type("ls docs")
        d.keys("\r")
        d.pump(0.3)
        d.frame(1.2)
        d.keys("\x1b\r", 0.6)                        # M-Enter: a second window
        d.pump(0.4)
        d.frame(0.3)
        d.type("echo tiled, floating, docked")
        d.keys("\r")
        d.pump(0.3)
        d.frame(1.0)
        d.chart = wm.create_window({"kind": "chart", "source": "push", "title": "load", "unit": "%", "lo": 0, "hi": 100})
        d.feed_chart(40)
        d.frame(1.0)
        for layout in ("grid", "master", "columns", "tile"):
            d.run("layout " + layout, 0.9)
        d.keys("\x1bp", 0.5)                          # M-p: the command palette
        d.type("theme nes", cps=10, pause=0.6)
        d.keys("\r", 1.4)
        for name in ("amiga", "c64", "dos", "matrix"):
            d.run("theme " + name, 1.2)
        d.run("effect matrix", 0.1)
        d.run("layout float", 0.1)
        for _ in range(30):
            d.pump(0.03)
            d.frame(0.1)
        d.run("effect none", 0.5)
        d.run("layout tile", 0.5)
        d.run("theme default", 1.5)
        sh.write_input(b"")
        d.rec.close()
        print("wrote", os.path.relpath(path, ROOT), "(%d frames, %.0f s)" % (d.rec.events, d.t))
    finally:
        d.close()


SCENES = {"themes": themes, "palette": palette, "effect": effect, "cast": cast}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "media"))
    ap.add_argument("--only", action="append", choices=sorted(SCENES))
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    try:
        for name in a.only or list(SCENES):
            SCENES[name](a.out)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
