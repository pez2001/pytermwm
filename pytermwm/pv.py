"""Internal pipe viewer: like ``pv`` but wired into the window manager's status line.

    cat big.iso | pytermwm pv -N iso | gzip > big.gz
    pytermwm pv -s 100M -L 5M < /dev/zero > /dev/null

Progress is drawn on stderr (like pv) and, if a session is reachable
(``PYTERMWM_SOCK`` is set inside pytermwm windows, or ``-s NAME`` is given), reported to
the status line and the status viewer window.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, List, Optional

from .charts import bar
from .terminal import detect_glyphs
from .sysinfo import human_bytes, human_rate, human_time


class Reporter:
    def __init__(self, name: str, label: str, session: Optional[str] = None):
        self.name, self.label = name, label
        self.client = None
        try:
            from .protocol import ControlClient
            if os.environ.get("PYTERMWM_SOCK") or session:
                self.client = ControlClient(session=session)
        except Exception:
            self.client = None

    def report(self, current, total, rate, done=False):
        if not self.client:
            return
        try:
            self.client.request({"op": "progress", "name": self.name, "label": self.label, "current": current,
                                 "total": total, "rate": rate, "done": done}, timeout=2)
        except Exception:
            self.client = None

    def close(self):
        if self.client:
            self.client.close()


def format_line(label: str, current: int, total: int, rate: float, elapsed: float, width: int = 30) -> str:
    if total:
        frac = min(1.0, current / total)
        eta = (total - current) / rate if rate > 1 else 0
        return "%s %s %3.0f%% %s/%s %s ETA %s" % (label, bar(frac, width, glyphs=detect_glyphs()), frac * 100, human_bytes(current),
                                                   human_bytes(total), human_rate(rate), human_time(eta))
    return "%s %s in %s (%s)" % (label, human_bytes(current), human_time(elapsed), human_rate(rate))


def parse_size(s: str) -> int:
    s = s.strip().upper().rstrip("B")
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))


def copy_stream(fd_in: int, fd_out: int, total: int = 0, label: str = "pv", name: Optional[str] = None,
                interval: float = 0.25, quiet: bool = False, rate_limit: int = 0, session: Optional[str] = None,
                stderr=sys.stderr, buf_size: int = 65536, on_progress: Optional[Callable] = None) -> int:
    """Copy data, reporting progress.  Returns the number of bytes copied."""
    rep = Reporter(name or "pv-%d" % os.getpid(), label, session)
    start = last = time.time()
    copied = 0
    last_bytes = 0
    rate = 0.0
    try:
        tty = stderr.isatty() if hasattr(stderr, "isatty") else False
    except Exception:
        tty = False
    try:
        while True:
            data = os.read(fd_in, buf_size)
            if not data:
                break
            view = memoryview(data)
            while view:
                n = os.write(fd_out, view)
                view = view[n:]
            copied += len(data)
            now = time.time()
            if rate_limit:
                expected = copied / float(rate_limit)
                lag = expected - (now - start)
                if lag > 0:
                    time.sleep(lag)
                    now = time.time()
            if now - last >= interval:
                inst = (copied - last_bytes) / (now - last)
                rate = inst if rate == 0 else 0.7 * rate + 0.3 * inst
                last, last_bytes = now, copied
                if on_progress:
                    on_progress(copied, total, rate)
                rep.report(copied, total, rate)
                if not quiet and tty:
                    stderr.write("\r\x1b[K" + format_line(label, copied, total, rate, now - start))
                    stderr.flush()
    except BrokenPipeError:
        pass
    finally:
        now = time.time()
        rate = copied / max(0.001, now - start)
        rep.report(copied, total or copied, rate, done=True)
        if not quiet:
            try:
                stderr.write(("\r\x1b[K" if tty else "") + format_line(label, copied, total or copied, rate, now - start) + "\n")
                stderr.flush()
            except Exception:
                pass
        rep.close()
    return copied


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pytermwm pv", description="progress viewer wired into pytermwm")
    ap.add_argument("files", nargs="*", help="input files (default: stdin)")
    ap.add_argument("-s", "--size", help="expected total size (e.g. 100M); auto for regular files")
    ap.add_argument("-N", "--name", help="label shown in the status line")
    ap.add_argument("-i", "--interval", type=float, default=0.25)
    ap.add_argument("-L", "--rate-limit", help="limit throughput (e.g. 5M)")
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr (status line only)")
    ap.add_argument("--session", help="session to report to (default: $PYTERMWM_SOCK)")
    args = ap.parse_args(argv)
    total = parse_size(args.size) if args.size else 0
    limit = parse_size(args.rate_limit) if args.rate_limit else 0
    label = args.name or (os.path.basename(args.files[0]) if args.files else "pv")
    out_fd = sys.stdout.fileno()
    if args.files:
        total = total or sum(os.path.getsize(f) for f in args.files if os.path.isfile(f))
        copied = 0
        for f in args.files:
            with open(f, "rb") as fh:
                copied += copy_stream(fh.fileno(), out_fd, total, label, None, args.interval, args.quiet, limit, args.session)
        return 0
    try:
        st = os.fstat(0)
        import stat as _s
        if not total and _s.S_ISREG(st.st_mode):
            total = st.st_size
    except OSError:
        pass
    copy_stream(0, out_fd, total, label, None, args.interval, args.quiet, limit, args.session)
    return 0
