"""``pytermwm doctor``: check that this machine can run pytermwm and say what is missing."""
from __future__ import annotations

import os
import platform
import sys
import tempfile
import time
from typing import Callable, List, Tuple

from . import compat


def _check_python() -> str:
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9 or newer is required (found %s)" % platform.python_version())
    return "Python %s (%s)" % (platform.python_version(), sys.executable)


def _check_yaml() -> str:
    try:
        import yaml
    except ImportError:
        raise RuntimeError("PyYAML is missing: pip install -r requirements.txt")
    return "PyYAML %s" % getattr(yaml, "__version__", "?")


def _check_dirs() -> str:
    from . import protocol as P
    out = []
    for name, fn in (("runtime", P.runtime_dir), ("state", P.state_dir)):
        d = fn()
        probe = os.path.join(d, ".doctor-%d" % os.getpid())
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.unlink(probe)
        out.append("%s=%s" % (name, d))
    return ", ".join(out)


def _check_endpoint() -> str:
    """Open a listener, connect to it and exchange a message (unix socket, or loopback TCP + key on Windows)."""
    d = tempfile.mkdtemp(prefix="ptw-doctor-")
    path = os.path.join(d, "probe" + compat.endpoint_ext())
    lis = compat.Listener(path)
    try:
        c = compat.connect_endpoint(path, 3.0)
        conn = None
        end = time.time() + 3
        while conn is None and time.time() < end:
            conn = lis.accept()
            time.sleep(0.01)
        if conn is None:
            raise RuntimeError("the listener did not accept the connection")
        c.sendall(b"ping")
        got = b""
        end = time.time() + 3
        while len(got) < 4 and time.time() < end:
            try:
                got += conn.recv(16)
            except BlockingIOError:
                time.sleep(0.01)
        c.close()
        conn.close()
        if got != b"ping":
            raise RuntimeError("no data through the endpoint")
    finally:
        lis.close()
        try:
            os.rmdir(d)
        except OSError:
            pass
    return "%s endpoint works" % ("loopback TCP" if compat.USE_TCP else "unix socket")


def _check_terminal() -> str:
    if compat.IS_WINDOWS:
        try:
            con = compat._WinConsole()
        except Exception as e:                       # pragma: no cover - Windows only
            raise RuntimeError("cannot use the console API: %s" % e)
        mode = con.mode(con.hout)
        if mode is None:
            return "stdout is not a console (fine for scripts; the attach client needs a real console)"
        vt = bool(mode & con.OUT_VT) or bool(con.k.SetConsoleMode(con.hout, mode | con.OUT_VT))
        if not vt:
            raise RuntimeError("this console does not support ANSI escape sequences (use Windows Terminal or Windows 10 1809+)")
        return "console supports virtual terminal sequences"
    if not sys.stdin.isatty():
        return "stdin is not a tty (fine for scripts)"
    return "tty available"


def _check_pty() -> str:
    """Start a child in a pty (POSIX) / pseudo console (Windows) and read its output."""
    marker = "pytermwm-doctor-ok"
    if compat.IS_WINDOWS:
        from . import winpty
        return winpty.selftest()
    from .window import PtySource
    src = PtySource([sys.executable, "-c", "print(%r)" % marker], None, None, 24, 80)
    got = b""
    import select
    try:
        end = time.time() + 8
        while time.time() < end and marker.encode() not in got:
            fds = list(src.read_fds())
            if not fds:
                break
            r, _, _ = select.select(fds, [], [], 0.2)
            for fd in r:
                d = src.read(fd)
                if d is None:
                    end = 0
                    break
                got += d
    finally:
        src.close()
    if marker.encode() not in got:
        raise RuntimeError("child output did not arrive through the pty (got %r)" % got[-100:])
    return "pty works" + (" (threaded I/O mode)" if compat.THREADED_IO else "")


def _check_shell() -> str:
    sh = compat.default_shell()
    from shutil import which
    if not (os.path.isfile(sh) or which(sh)):
        raise RuntimeError("default shell %r not found (set `shell:` in the config or $PYTERMWM_SHELL)" % sh)
    return "default shell: %s" % sh


CHECKS: List[Tuple[str, Callable[[], str]]] = [
    ("python", _check_python),
    ("PyYAML", _check_yaml),
    ("directories", _check_dirs),
    ("session endpoint", _check_endpoint),
    ("terminal", _check_terminal),
    ("pty / ConPTY", _check_pty),
    ("shell", _check_shell),
]


def run(out=sys.stdout) -> int:
    out.write("pytermwm doctor - %s %s, %s\n" % (platform.system(), platform.release(), platform.machine()))
    failed = 0
    for name, fn in CHECKS:
        try:
            msg = fn()
            out.write("  ok    %-17s %s\n" % (name, msg))
        except Exception as e:
            failed += 1
            out.write("  FAIL  %-17s %s\n" % (name, e))
    out.write("everything looks fine\n" if not failed else "%d problem(s) found\n" % failed)
    return 1 if failed else 0
