"""Windows and their content sources.

A :class:`Window` owns a :class:`~pytermwm.ansi.Screen`.  Content comes from a
*source* (pty process, pipe process, file/fifo tail) or is pushed/generated
(internal windows such as help, status viewer, charts).
"""
from __future__ import annotations

import errno
import os
import select
import shlex
import signal
import struct
import subprocess
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import compat
from .compat import fcntl, pty, termios, IS_WINDOWS, THREADED_IO, O_BINARY
from .ansi import Screen, blank_line, Cell, str_width, cells_from_text
from .colors import WIDE, TAIL, parse_color, sgr
from .cp437 import make_decoder

# --------------------------------------------------------------------- options
OPTION_SPECS: Dict[str, Tuple[str, object, Optional[tuple]]] = {
    # name: (type, default, choices)
    "border": ("bool", True, None),
    "scrollbar": ("choice", "auto", ("auto", "on", "off")),
    "overflow": ("choice", "wrap", ("wrap", "clip", "ellipsis")),
    "cp437": ("bool", False, None),
    "history": ("int", 2000, None),
    "on_exit": ("choice", "close", ("close", "keep", "restart")),
    "stderr_color": ("color", None, None),
    "shadow": ("bool", False, None),
    "follow": ("bool", True, None),
    "readonly": ("bool", False, None),
    "icon": ("str", "", None),
    "tag": ("str", "", None),
}


def coerce_option(name: str, value):
    if name not in OPTION_SPECS:
        raise KeyError("unknown window option: %s" % name)
    typ, _default, choices = OPTION_SPECS[name]
    if typ == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on", "y"):
            return True
        if s in ("0", "false", "no", "off", "n"):
            return False
        raise ValueError("%s: expected boolean, got %r" % (name, value))
    if typ == "int":
        v = int(value)
        if v < 0:
            raise ValueError("%s must be >= 0" % name)
        return v
    if typ == "choice":
        if isinstance(value, bool):
            value = "on" if value else "off"
        s = str(value).strip().lower()
        if s not in choices:
            raise ValueError("%s: expected one of %s" % (name, ", ".join(choices)))
        return s
    if typ == "color":
        return parse_color(value)
    return str(value)


def set_winsize(fd: int, rows: int, cols: int):
    if fcntl is None:
        return
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


# --------------------------------------------------------------------- sources
class Source:
    """Base class of content sources."""
    tty = False          # output already has terminal line discipline (no LF->CRLF needed)
    alive = True
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    poll_based = False

    def read_fds(self) -> Dict[int, str]:
        return {}

    def read(self, fd: int) -> Optional[bytes]:
        """Read available data. ``None`` on EOF."""
        return b""

    def poll_data(self) -> bytes:
        return b""

    def write(self, data: bytes):
        pass

    wbuf = b""

    @property
    def wants_write(self) -> bool:
        return bool(self.wbuf)

    def write_fd(self) -> Optional[int]:
        return None

    def flush(self):
        pass

    def resize(self, rows: int, cols: int):
        pass

    def check_exit(self) -> bool:
        return not self.alive

    def close(self):
        self.alive = False

    def describe(self) -> dict:
        return {}


def default_shell() -> str:
    return compat.default_shell()


class PtySource(Source):
    tty = True

    def __init__(self, argv: List[str], cwd: Optional[str], env: Optional[dict],
                 rows: int, cols: int):
        self.argv = argv
        self.cwd = cwd
        self.master, slave = pty.openpty()
        set_winsize(self.master, rows, cols)
        e = dict(os.environ)
        e.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "PYTERMWM": "1"})
        e.pop("TERM_PROGRAM", None)
        if env:
            e.update({str(k): str(v) for k, v in env.items()})
        e["LINES"] = str(rows)
        e["COLUMNS"] = str(cols)

        def _ctty():
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            self.proc = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd or None, env=e,
                start_new_session=True, close_fds=True, preexec_fn=_ctty)
        except Exception:
            os.close(self.master)
            os.close(slave)
            raise
        os.close(slave)
        os.set_blocking(self.master, False)
        self.pid = self.proc.pid
        self.alive = True
        self.wbuf = b""
        self._eof = False

    def read_fds(self):
        return {self.master: "out"} if not self._eof else {}

    def read(self, fd):
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return b""
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                self._eof = True
                return None
            raise
        if not data:
            self._eof = True
            return None
        return data

    def write(self, data: bytes):
        if not self.alive or self._eof:
            return
        self.wbuf += data
        self.flush()

    def write_fd(self):
        return self.master if not self._eof else None

    def flush(self):
        while self.wbuf:
            try:
                n = os.write(self.master, self.wbuf)
            except BlockingIOError:
                return
            except OSError:
                self.wbuf = b""
                return
            self.wbuf = self.wbuf[n:]

    def resize(self, rows, cols):
        set_winsize(self.master, rows, cols)
        try:
            compat.signal_group(self.proc.pid, compat.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def check_exit(self) -> bool:
        rc = self.proc.poll()
        if rc is not None and self.alive:
            if self._eof or True:
                self.exit_code = rc
                # drain remaining output first (caller reads until EOF)
                if self._eof:
                    self.alive = False
        return not self.alive

    def finish(self):
        """Called once EOF has been seen: reap the child."""
        try:
            self.exit_code = self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.poll()
            self.exit_code = self.proc.returncode
        self.alive = False

    def cwd_live(self) -> Optional[str]:
        try:
            return os.readlink("/proc/%d/cwd" % self.proc.pid)
        except OSError:
            return self.cwd

    def close(self):
        if self.proc.poll() is None:
            try:
                compat.signal_group(self.proc.pid, compat.SIGHUP)
            except (OSError, ProcessLookupError):
                pass
            try:
                self.proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                try:
                    compat.signal_group(self.proc.pid, compat.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    self.proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        try:
            os.close(self.master)
        except OSError:
            pass
        self.alive = False
        self._eof = True

    def describe(self):
        return {"pid": self.pid, "argv": self.argv}


class PipeSource(Source):
    """Process connected through pipes (separate stdout / stderr, no tty)."""

    def __init__(self, argv, cwd=None, env=None, shell=False):
        e = dict(os.environ)
        e["PYTERMWM"] = "1"
        if env:
            e.update({str(k): str(v) for k, v in env.items()})
        self.argv = argv
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=cwd or None, env=e,
                                     shell=shell, bufsize=0, **compat.new_session_kwargs())
        self.pid = self.proc.pid
        for f in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
            os.set_blocking(f.fileno(), False)
        self._open = {self.proc.stdout.fileno(): "out", self.proc.stderr.fileno(): "err"}
        self.wbuf = b""
        self.alive = True
        self._stdin_closed = False

    def read_fds(self):
        return dict(self._open)

    def read(self, fd):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return b""
        except OSError:
            data = b""
        if not data:
            self._open.pop(fd, None)
            if not self._open:
                return None
            return b""
        return data

    def write(self, data: bytes):
        if self._stdin_closed:
            return
        self.wbuf += data
        self.flush()

    def close_stdin(self):
        self.flush()
        if not self._stdin_closed and not self.wbuf:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self._stdin_closed = True

    def write_fd(self):
        return None if self._stdin_closed else self.proc.stdin.fileno()

    def flush(self):
        while self.wbuf and not self._stdin_closed:
            try:
                n = os.write(self.proc.stdin.fileno(), self.wbuf)
            except BlockingIOError:
                return
            except OSError:
                self.wbuf = b""
                return
            self.wbuf = self.wbuf[n:]

    def finish(self):
        try:
            self.exit_code = self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.exit_code = self.proc.poll()
        self.alive = False

    def check_exit(self):
        return not self.alive

    def close(self):
        if self.proc.poll() is None:
            try:
                compat.signal_group(self.proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                self.proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                try:
                    compat.signal_group(self.proc.pid, compat.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        for f in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
            try:
                f.close()
            except OSError:
                pass
        self.alive = False

    def describe(self):
        return {"pid": self.pid, "argv": self.argv}


class FileSource(Source):
    """Follow a file (``tail -f`` semantics) or read a FIFO."""

    def __init__(self, path: str, tail_bytes: int = 64 * 1024, follow: bool = True):
        self.path = os.path.expanduser(path)
        self.follow = follow
        self.fd: Optional[int] = None
        self._is_fifo = False
        self._inode = None
        self._pos = 0
        self._tail = tail_bytes
        self.alive = True
        self._pending = b""
        self._open()

    def _open(self):
        st = os.stat(self.path)
        import stat as _stat
        if _stat.S_ISFIFO(st.st_mode):
            self._is_fifo = True
            self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | O_BINARY)
            self.poll_based = False
        else:
            self.poll_based = True
            self.fd = os.open(self.path, os.O_RDONLY | O_BINARY)
            self._inode = st.st_ino
            size = st.st_size
            self._pos = max(0, size - self._tail)
            os.lseek(self.fd, self._pos, os.SEEK_SET)
            if self._pos > 0:
                # skip partial first line
                chunk = os.read(self.fd, 4096)
                nl = chunk.find(b"\n")
                self._pos += (nl + 1) if nl >= 0 else len(chunk)
                os.lseek(self.fd, self._pos, os.SEEK_SET)

    def read_fds(self):
        return {self.fd: "out"} if self._is_fifo and self.fd is not None else {}

    def read(self, fd):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return b""
        if not data:
            # writer closed; reopen to wait for the next writer
            try:
                os.close(fd)
            except OSError:
                pass
            if self.follow:
                self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | O_BINARY)
                return b""
            self.fd = None
            return None
        return data

    def poll_data(self) -> bytes:
        if self._is_fifo or self.fd is None:
            return b""
        try:
            st = os.stat(self.path)
        except OSError:
            return b""
        if st.st_ino != self._inode or st.st_size < self._pos:
            # rotated / truncated
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = os.open(self.path, os.O_RDONLY | O_BINARY)
            self._inode = st.st_ino
            self._pos = 0
        if st.st_size > self._pos:
            data = os.read(self.fd, 1 << 20)
            self._pos += len(data)
            return data
        if not self.follow:
            self.alive = False
        return b""

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.alive = False

    def describe(self):
        return {"path": self.path}


class ThreadedPipeSource(PipeSource):
    """PipeSource for platforms where pipes cannot be select()-ed (Windows): helper threads read the child's
    stdout/stderr and hand the bytes to the selector loop through socket pairs; a writer thread feeds stdin."""

    def __init__(self, argv, cwd=None, env=None, shell=False):
        e = dict(os.environ)
        e["PYTERMWM"] = "1"
        if env:
            e.update({str(k): str(v) for k, v in env.items()})
        if IS_WINDOWS and not shell:
            from .compat import resolve_argv
            argv = resolve_argv(list(argv))
        self.argv = argv
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=cwd or None, env=e, shell=shell, bufsize=0, **compat.new_session_kwargs())
        self.pid = self.proc.pid
        self._pumps: Dict[int, "compat.Pump"] = {}
        self._open = {}
        for stream, f in (("out", self.proc.stdout), ("err", self.proc.stderr)):
            pump = compat.Pump((lambda f=f: f.read(65536)), "pipe-" + stream)
            self._pumps[pump.fileno()] = pump
            self._open[pump.fileno()] = stream

        def _write(data, stdin=self.proc.stdin):
            stdin.write(data)
            stdin.flush()
        self._writer = compat.ThreadWriter(_write, "pipe-stdin")
        self.wbuf = b""
        self.alive = True
        self._stdin_closed = False

    def read(self, fd):
        pump = self._pumps.get(fd)
        if pump is None:
            return None
        data = pump.recv()
        if data is None:
            self._open.pop(fd, None)
            return None if not self._open else b""
        return data

    def write(self, data: bytes):
        if not self._stdin_closed:
            self._writer.put(data)

    def close_stdin(self):
        if not self._stdin_closed:
            self._stdin_closed = True
            self._writer.close()
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def write_fd(self):
        return None

    @property
    def wants_write(self) -> bool:
        return False

    def flush(self):
        pass

    def close(self):
        if self.proc.poll() is None:
            compat.signal_group(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                compat.signal_group(self.proc.pid, compat.SIGKILL)
        self._writer.close()
        for f in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
            try:
                f.close()
            except (OSError, ValueError):
                pass
        for pump in self._pumps.values():
            pump.close()
        self.alive = False


class ThreadedPtySource(PtySource):
    """A real pty whose master is read/written by helper threads.  Only used on POSIX when
    ``PYTERMWM_THREADED_IO=1`` so the Windows I/O model (pumps + socket pairs) can be tested on Linux/macOS."""

    def __init__(self, argv, cwd, env, rows, cols):
        super().__init__(argv, cwd, env, rows, cols)
        os.set_blocking(self.master, True)
        master = self.master

        def _read():
            try:
                return os.read(master, 65536)
            except OSError:
                return b""
        self._pump = compat.Pump(_read, "pty-read")
        self._writer = compat.ThreadWriter(lambda d: os.write(master, d), "pty-write")

    def read_fds(self):
        return {self._pump.fileno(): "out"} if not self._eof else {}

    def read(self, fd):
        d = self._pump.recv()
        if d is None:
            self._eof = True
        return d

    def write(self, data: bytes):
        if self.alive and not self._eof:
            self._writer.put(data)

    def write_fd(self):
        return None

    def flush(self):
        pass

    def close(self):
        super().close()
        self._writer.close()
        self._pump.close()


# --------------------------------------------------------------------- window
class Window:
    """A window: screen + source + presentation options."""
    kind = "term"
    refresh_interval: Optional[float] = None    # internal windows
    recorder = None                             # a recording.CastRecorder while `record` is active
    emits_output = True                         # False for replay windows: their text is not "output" for rules

    def __init__(self, wid: int, title: str = "", rows: int = 24, cols: int = 80, **opts):
        self.id = wid
        self.name = opts.pop("name", None) or ""
        self.title_text = title or ""
        self.opts = {k: spec[1] for k, spec in OPTION_SPECS.items()}
        for k, v in opts.items():
            if k in OPTION_SPECS:
                self.opts[k] = coerce_option(k, v)
        self.screen = Screen(rows, cols, self.opts["history"])
        self.screen.on_title = self._on_title
        self.screen.on_resize_request = self._on_resize_request
        self.dyn_title = ""
        self.default_title = ""
        self.drag_overlay_title: Optional[str] = None   # transient: shown instead of the title while
                                                          # a floating window is being dragged/resized
        self.source: Optional[Source] = None
        self.decoders = {}
        self.viewport = (cols, rows)   # cols, rows of the visible content area
        self.vsize: Optional[Tuple[int, int]] = None   # (cols, rows) virtual size; None = auto
        self.scroll_y = 0
        self.scroll_x = 0
        self.floating = False
        self.frect: Optional[Tuple[int, int, int, int]] = None   # x, y, w, h (outer)
        self.dock: Optional[Tuple[str, int]] = None              # (edge, size)
        self.created = time.time()
        self.last_output = 0.0
        self.output_bytes = 0
        self.output_lines = 0
        self.exited = False
        self.exit_code: Optional[int] = None
        self.exit_time: Optional[float] = None
        self.desktop = None
        self.wm = None
        self.routes: Dict[str, dict] = {}    # stream -> {"self": bool, "sinks": [(kind, target)]}
        self.spec: dict = {}                 # spec used to create it (for restore / restart)
        self.bell = False
        self.activity = False               # output while not focused
        self.resized_at = 0.0               # a resize makes shells redraw their prompt: that is not activity
        self._lf_seen = 0
        self.closing = False
        self.hidden = False
        self._set_decoders()

    # ---------------------------------------------------------------- basics
    @property
    def title(self) -> str:
        return (self.drag_overlay_title or self.title_text or self.dyn_title or self.name or
                self.default_title or ("window %d" % self.id))

    def _on_title(self, t: str):
        self.dyn_title = t
        if self.wm:
            self.wm.emit("window_title", window=self)

    def _on_resize_request(self, rows: int, cols: int):
        if rows > 0 and cols > 0:
            self.set_vsize((min(cols, 1000), min(rows, 1000)))
            if self.wm:
                self.wm.relayout()

    def _set_decoders(self):
        self.decoders = {"out": make_decoder(self.opts["cp437"]), "err": make_decoder(self.opts["cp437"])}

    # ---------------------------------------------------------------- options
    def set_option(self, name: str, value):
        v = coerce_option(name, value)
        self.opts[name] = v
        if name == "cp437":
            self._set_decoders()
        elif name == "history":
            self.screen.set_history_limit(v)
        elif name == "overflow":
            self.screen.force_no_wrap = (v in ("clip", "ellipsis"))
        return v

    def apply_overflow(self):
        self.screen.force_no_wrap = self.opts["overflow"] in ("clip", "ellipsis")

    # ---------------------------------------------------------------- sizing
    def set_viewport(self, cols: int, rows: int):
        cols, rows = max(1, cols), max(1, rows)
        self.viewport = (cols, rows)
        if self.vsize is None:
            self._resize_screen(rows, cols)
        self._clamp_scroll()

    def set_vsize(self, size: Optional[Tuple[int, int]]):
        self.vsize = size
        if size is None:
            c, r = self.viewport
            self._resize_screen(r, c)
        else:
            self._resize_screen(max(1, size[1]), max(1, size[0]))
        self._clamp_scroll()

    def _resize_screen(self, rows, cols):
        if rows != self.screen.rows or cols != self.screen.cols:
            self.screen.resize(rows, cols)
            if self.output_bytes:           # a running program redraws after SIGWINCH (not the initial size of a new one)
                self.resized_at = time.time()
            if self.recorder is not None:
                self.recorder.resize(cols, rows)
            if self.source:
                self.source.resize(rows, cols)

    def _clamp_scroll(self):
        vc, vr = self.viewport
        max_y = max(0, self.screen.total_lines() - vr)
        self.scroll_y = max(0, min(self.scroll_y, max_y))
        max_x = max(0, self.screen.cols - vc)
        self.scroll_x = max(0, min(self.scroll_x, max_x))

    # ---------------------------------------------------------------- scrolling
    def scroll(self, dy: int = 0, dx: int = 0):
        self.scroll_y += dy
        self.scroll_x += dx
        self._clamp_scroll()

    def view_offset(self) -> int:
        """Lines scrolled back from the bottom of the screen buffer in the current view (used by selection)."""
        return self.scroll_y

    def scroll_to_bottom(self):
        self.scroll_y = 0

    def scroll_to_top(self):
        self.scroll_y = 1 << 30
        self._clamp_scroll()

    # ---------------------------------------------------------------- output path
    def feed_bytes(self, data: bytes, stream: str = "out"):
        if not data:
            return
        dec = self.decoders.get(stream) or self.decoders["out"]
        text = dec.decode(data)
        if self.source is not None and not self.source.tty:
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        self.feed_text(text, stream=stream, nbytes=len(data), raw=data)

    def feed_text(self, text: str, stream: str = "out", nbytes: Optional[int] = None, raw: bytes = None):
        if not text:
            return
        scr = self.screen
        if stream == "err" and self.opts["stderr_color"] is not None:
            from .colors import color_params
            text = "\x1b[%sm%s\x1b[39m" % (color_params(self.opts["stderr_color"], False), text)
        before = scr.last_line_feeds
        bells = scr.bell_count
        route = self.routes.get(stream)
        if route is not None and route.get("self") is False:
            # output is re-routed elsewhere: do not show it in this window
            self.last_output = time.time()
            self.output_bytes += nbytes if nbytes is not None else len(text)
            if self.wm:
                self.wm.window_output(self, text, stream, raw if raw is not None else text.encode("utf-8", "replace"))
            return
        if self.recorder is not None:
            self.recorder.output(text)
        scr.feed(text)
        self.last_output = time.time()
        self.output_bytes += nbytes if nbytes is not None else len(text)
        self.output_lines += text.count("\n")
        delta = scr.last_line_feeds - before
        if self.scroll_y > 0 and delta:
            self.scroll_y += delta
            self._clamp_scroll()
        if scr.responses and self.source is not None and self.source.tty:
            self.source.write(scr.take_responses().encode("utf-8", "replace"))
        else:
            scr.responses.clear()
        if self.wm:
            if self.emits_output:
                self.wm.window_output(self, text, stream, raw if raw is not None else text.encode("utf-8", "replace"),
                                      bell=scr.bell_count > bells)
            else:
                self.wm.dirty = True

    def write_input(self, data: bytes):
        """Bytes typed into the window (to the process' stdin / tty)."""
        if self.opts["readonly"]:
            return
        if self.recorder is not None:
            self.recorder.input(data)
        if self.source is not None and self.source.alive:
            self.source.write(data)
        elif self.exited and self.wm:
            # any key on a kept dead window closes it
            if self.opts["on_exit"] == "keep":
                self.wm.close_window(self.id)

    def handle_key(self, key: str, raw: bytes) -> bool:
        """Internal windows override to consume keys.  Return True if handled."""
        return False

    def on_focus(self, focused: bool):
        pass

    def on_tick(self, now: float):
        pass

    def restart(self):
        self.spec_restart = True

    # ---------------------------------------------------------------- viewing
    def visible_lines(self, vw: int, vh: int) -> List[List[Cell]]:
        scr = self.screen
        lines = scr.view(vh, self.scroll_y)
        out = []
        sx = self.scroll_x
        ell = self.opts["overflow"] == "ellipsis"
        for l in lines:
            seg = l[sx:sx + vw]
            if len(seg) < vw:
                seg = list(seg) + [(" ", None, None, 0)] * (vw - len(seg))
            else:
                seg = list(seg)
            if seg and seg[0][3] & TAIL:
                seg[0] = (" ", seg[0][1], seg[0][2], seg[0][3] & ~TAIL)
            if seg and seg[-1][3] & WIDE:
                seg[-1] = (" ", seg[-1][1], seg[-1][2], seg[-1][3] & ~WIDE)
            if ell and len(l) > sx + vw and l[sx + vw - 1][0].strip():
                c = seg[-1]
                seg[-1] = ("…", c[1], c[2], c[3] & ~(WIDE | TAIL))
            out.append(seg)
        while len(out) < vh:
            out.append(blank_line(vw))
        return out[-vh:] if len(out) > vh else out

    def cursor_position(self, vw: int, vh: int) -> Optional[Tuple[int, int]]:
        scr = self.screen
        if not scr.cursor_visible or self.exited and not (self.source and self.source.alive):
            return None
        total = scr.total_lines()
        row_abs = len(scr.history) + scr.y
        end = total - self.scroll_y
        start = end - vh
        if not (start <= row_abs < end):
            return None
        cx = scr.x - self.scroll_x
        if not (0 <= cx < vw):
            return None
        return cx, row_abs - start

    def scroll_info(self, vh: int):
        """(total_lines, first_visible_index) for scrollbar drawing."""
        total = self.screen.total_lines()
        end = total - self.scroll_y
        return total, max(0, end - vh)

    def hscroll_info(self, vw: int):
        return self.screen.cols, self.scroll_x

    # ---------------------------------------------------------------- text access
    def text(self, history: bool = False) -> str:
        return self.screen.text(history=history)

    def cwd(self) -> Optional[str]:
        if isinstance(self.source, PtySource):
            return self.source.cwd_live()
        return self.screen.cwd

    # ---------------------------------------------------------------- lifecycle
    def on_exit(self, code):
        self.exited = True
        self.exit_code = code
        self.exit_time = time.time()

    def close(self):
        self.closing = True
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None
        if self.source:
            self.source.close()

    def describe(self) -> dict:
        d = {
            "id": self.id, "name": self.name, "title": self.title, "kind": self.kind,
            "size": [self.screen.cols, self.screen.rows],
            "viewport": list(self.viewport),
            "floating": self.floating, "dock": list(self.dock) if self.dock else None,
            "exited": self.exited, "exit_code": self.exit_code,
            "opts": dict(self.opts),
            "history_lines": len(self.screen.history),
            "output_bytes": self.output_bytes,
            "last_output": self.last_output,
            "cursor": [self.screen.x, self.screen.y],
            "vsize": list(self.vsize) if self.vsize else None,
            "scroll": [self.scroll_x, self.scroll_y],
            "desktop": self.desktop.name if self.desktop is not None else None,
        }
        if self.opts.get("stderr_color") is not None:
            d["opts"]["stderr_color"] = str(self.opts["stderr_color"])
        if self.source:
            d["source"] = self.source.describe()
        if self.frect:
            d["frect"] = list(self.frect)
        if self.recorder is not None:
            d["recording"] = self.recorder.describe()
        return d


class ProcessWindow(Window):
    kind = "term"

    def start(self, argv=None, cwd=None, env=None, mode="pty", shell=False, path=None):
        cols, rows = self.viewport
        if mode == "file":
            self.source = FileSource(path)
            self.kind = "file"
        elif mode == "pipe":
            self.source = PipeSource(argv, cwd, env, shell=shell)
            self.kind = "pipe"
        else:
            self.source = PtySource(argv, cwd, env, self.screen.rows, self.screen.cols)
        return self.source


def parse_command(cmd) -> Tuple[List[str], bool]:
    """Return (argv, use_shell).  Strings with shell syntax are run through the shell."""
    if cmd is None or cmd == "":
        return [default_shell()], False
    if isinstance(cmd, (list, tuple)):
        return [str(c) for c in cmd], False
    s = str(cmd)
    return compat.shell_command(s), False


class InternalWindow(Window):
    """Window whose content is generated by python code."""
    kind = "internal"
    refresh_interval: Optional[float] = 1.0

    def __init__(self, wid, title="", rows=24, cols=80, **opts):
        opts.setdefault("history", 0)
        super().__init__(wid, title, rows, cols, **opts)
        self._last_refresh = 0.0
        self.dirty = True

    def render(self, cols: int, rows: int) -> List[str]:
        """Return list of ANSI-text lines (no newlines)."""
        return []

    def refresh(self):
        cols, rows = self.screen.cols, self.screen.rows
        lines = self.render(cols, rows)
        scr = self.screen
        scr.feed("\x1b[?7l\x1b[H\x1b[2J\x1b[0m")
        out = "\r\n".join(lines[:rows])
        scr.feed(out)
        scr.feed("\x1b[?7h")
        scr.cursor_visible = False
        self._last_refresh = time.time()
        self.dirty = False

    def on_tick(self, now: float):
        if self.dirty or (self.refresh_interval and now - self._last_refresh >= self.refresh_interval):
            self.refresh()

    def set_viewport(self, cols, rows):
        super().set_viewport(cols, rows)
        self.dirty = True

    def write_input(self, data: bytes):
        pass


class TextWindow(InternalWindow):
    """Static/pushed text (used for help, dialogs' bodies, etc.)."""
    kind = "text"
    refresh_interval = None

    def __init__(self, wid, title="", rows=24, cols=80, text: str = "", **opts):
        super().__init__(wid, title, rows, cols, **opts)
        self.lines = text.split("\n") if text else []

    def set_text(self, text: str):
        self.lines = text.split("\n")
        self.dirty = True

    def render(self, cols, rows):
        off = self.scroll_y
        return self.lines[off:off + rows]

    def handle_key(self, key, raw):
        n = self.screen.rows
        if key in ("Up", "k"):
            self.scroll_y = max(0, self.scroll_y - 1)
        elif key in ("Down", "j"):
            self.scroll_y = min(max(0, len(self.lines) - n), self.scroll_y + 1)
        elif key == "PageUp":
            self.scroll_y = max(0, self.scroll_y - n + 1)
        elif key in ("PageDown", "Space"):
            self.scroll_y = min(max(0, len(self.lines) - n), self.scroll_y + n - 1)
        elif key in ("Home", "g"):
            self.scroll_y = 0
        elif key in ("End", "G"):
            self.scroll_y = max(0, len(self.lines) - n)
        elif key in ("q", "Esc") and self.wm:
            self.wm.close_window(self.id)
        else:
            return False
        self.dirty = True
        return True

    def _clamp_scroll(self):
        # scroll_y here indexes into self.lines, not the emulator history
        self.scroll_y = max(0, self.scroll_y)

    def scroll(self, dy=0, dx=0):
        # positive dy scrolls back (towards the top) in the generic API
        n = self.screen.rows
        self.scroll_y = max(0, min(max(0, len(self.lines) - n), self.scroll_y - dy))
        self.dirty = True

    def view_offset(self) -> int:
        return 0                                 # the text window scrolls its own line list, not the emulator history

    def visible_lines(self, vw, vh):
        saved = self.scroll_y
        self.scroll_y = 0
        try:
            return super().visible_lines(vw, vh)
        finally:
            self.scroll_y = saved

    def scroll_info(self, vh):
        return max(len(self.lines), vh), self.scroll_y


# ---------------------------------------------------------------- platform specific sources
if THREADED_IO:
    PipeSource = ThreadedPipeSource            # noqa: F811
if IS_WINDOWS:
    class PtySource:                            # noqa: F811  (factory: ConPTY needs the Windows-only winpty module)
        tty = True

        def __new__(cls, *args, **kwargs):
            from .winpty import ConPtySource
            return ConPtySource(*args, **kwargs)
elif THREADED_IO:
    PtySource = ThreadedPtySource               # noqa: F811
