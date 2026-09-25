"""Test helpers: headless window manager + virtual client that decodes frames."""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pytermwm.ansi import Screen
from pytermwm.keys import KeyParser
from pytermwm.render import Compositor, FrameWriter
from pytermwm.wm import WindowManager


def make_wm(cols=100, rows=30, config=None, **kw):
    wm = WindowManager(cols, rows, config=config, **kw)
    return wm


def pump(wm, seconds=0.5, until=None, step=0.02):
    """Run the IO plumbing of the WM by hand for a while (like the server loop)."""
    import select
    end = time.time() + seconds
    while time.time() < end:
        fds = {}
        for fd, w, stream in wm.iter_read_fds():
            fds[fd] = (w, stream)
        r = []
        if fds:
            r, _, _ = select.select(list(fds), [], [], step)
        else:
            time.sleep(step)
        for fd in r:
            w, stream = fds[fd]
            wm.source_readable(w, fd, stream)
        wm.poll_sources()
        wm.tick(time.time())
        if until is not None and until():
            return True
    return until() if until else True


def screen_of_frame(wm, cols=None, rows=None):
    """Render the WM and decode the ANSI through a Screen (what a real terminal would show)."""
    cols, rows = cols or wm.cols, rows or wm.rows
    frame = Compositor(wm).compose(cols, rows)
    fw = FrameWriter(24)
    out = fw.write(frame)
    scr = Screen(rows, cols, 0)
    scr.feed(out)
    return scr, frame


def type_keys(wm, data: bytes):
    p = getattr(wm, "_test_parser", None)
    if p is None:
        p = wm._test_parser = KeyParser()
    evs = p.feed(data) + p.flush()
    wm.process_events(evs)
