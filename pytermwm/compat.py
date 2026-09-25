"""Platform layer: everything that differs between POSIX and Windows lives here.

* ``IS_WINDOWS``            running on Windows (console + ConPTY + loopback TCP)
* ``THREADED_IO``           blocking handles are read by helper threads and handed to the selector loop through
                            socket pairs (always true on Windows; ``PYTERMWM_THREADED_IO=1`` forces it elsewhere,
                            which is how the Windows I/O model is exercised by the Linux test-suite)
* ``USE_TCP``               sessions listen on ``127.0.0.1`` + a per-session key instead of a unix socket
                            (always true on Windows; ``PYTERMWM_TCP=1`` forces it elsewhere)

The rest of the code base imports from here instead of touching ``termios`` / ``fcntl`` / ``pty`` / ``SIGWINCH`` /
``AF_UNIX`` directly, so every module can be imported on every platform.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win") or os.name == "nt"
THREADED_IO = IS_WINDOWS or os.environ.get("PYTERMWM_THREADED_IO") == "1"
USE_TCP = IS_WINDOWS or os.environ.get("PYTERMWM_TCP") == "1" or not hasattr(socket, "AF_UNIX")

try:                                    # POSIX only
    import fcntl
    import pty
    import termios
    import tty
except ImportError:                     # Windows
    fcntl = pty = termios = tty = None  # type: ignore[assignment]

O_BINARY = getattr(os, "O_BINARY", 0)

# signals that do not exist on Windows
SIGHUP = getattr(signal, "SIGHUP", None)
SIGWINCH = getattr(signal, "SIGWINCH", None)
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
SIGTERM = signal.SIGTERM


def install_signal(sig: Optional[int], handler) -> bool:
    """signal.signal() that tolerates missing signals, non-main threads and Windows."""
    if sig is None:
        return False
    try:
        signal.signal(sig, handler)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


# ---------------------------------------------------------------------------------------------- directories
def _win_dir(var: str) -> str:
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), "AppData", "Local" if var == "LOCALAPPDATA" else "Roaming")


def config_home() -> str:
    """Directory that contains ``pytermwm/`` (config, plugins, scripts)."""
    x = os.environ.get("XDG_CONFIG_HOME")
    if x:
        return x
    if IS_WINDOWS:
        return _win_dir("APPDATA")
    return os.path.expanduser("~/.config")


def state_home() -> str:
    """Base directory for persistent state (a ``pytermwm`` subdirectory is added by the caller)."""
    x = os.environ.get("XDG_STATE_HOME")
    if x:
        return x
    if IS_WINDOWS:
        return _win_dir("LOCALAPPDATA")
    return os.path.expanduser("~/.local/state")


def runtime_home() -> str:
    """Base directory for sockets/tokens of running sessions (private to the user)."""
    if IS_WINDOWS:
        return os.path.join(_win_dir("LOCALAPPDATA"), "pytermwm", "run")
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return os.path.join(xdg, "pytermwm")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(tempfile.gettempdir(), "pytermwm-%d" % uid)


# ---------------------------------------------------------------------------------------------- shells / executables
def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def default_shell() -> str:
    """The shell for new windows when the config does not say otherwise."""
    env = os.environ.get("PYTERMWM_SHELL")
    if env:
        return env
    if not IS_WINDOWS:
        return os.environ.get("SHELL") or "/bin/bash"
    sh = os.environ.get("SHELL")                       # set by git-bash / msys; only trust a real Windows path
    if sh and os.path.isfile(sh):
        return sh
    for cand in ("pwsh.exe", "powershell.exe"):
        p = _which(cand)
        if p:
            return p
    return os.environ.get("COMSPEC") or "cmd.exe"


def shell_command(line: str) -> List[str]:
    """argv that runs ``line`` through the platform shell (used for ``cmd`` strings in configs and windows)."""
    sh = default_shell()
    if not IS_WINDOWS:
        return [sh, "-c", line]
    base = re.split(r"[\\/]", sh)[-1].lower()
    if base in ("cmd", "cmd.exe"):
        return [sh, "/d", "/c", line]
    if base in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        return [sh, "-NoLogo", "-Command", line]
    return [sh, "-c", line]


def split_command_line(line: str) -> List[str]:
    """Windows-friendly ``shlex.split``: backslashes are path separators, not escapes."""
    import shlex
    if not IS_WINDOWS:
        return shlex.split(line)
    out, cur, quote, have = [], [], None, False
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            quote, have = ch, True
        elif ch.isspace():
            if cur or have:
                out.append("".join(cur))
                cur, have = [], False
        else:
            cur.append(ch)
        i += 1
    if quote:
        raise ValueError("No closing quotation")
    if cur or have:
        out.append("".join(cur))
    return out


def resolve_argv(argv: List[str]) -> List[str]:
    """Windows: make ``argv`` runnable by CreateProcess (find the executable on PATH, wrap .bat/.cmd in cmd.exe)."""
    if not argv:
        raise ValueError("empty command")
    exe = argv[0]
    found = shutil.which(exe) or (exe if os.path.isfile(exe) else None)
    if found is None:
        return list(argv)
    if os.path.splitext(found)[1].lower() in (".bat", ".cmd"):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", found] + list(argv[1:])
    return [found] + list(argv[1:])


def quote_shell_arg(v: str) -> str:
    """Quote ``v`` as one argument for the platform shell, safe against injection.

    POSIX: ``shlex.quote``.  Windows (cmd.exe and PowerShell): characters that either shell would interpret inside
    double quotes are replaced by ``_`` and the result is double-quoted."""
    import shlex
    if not IS_WINDOWS:
        return shlex.quote(v)
    return '"' + re.sub(r'["&|<>^%!`$\r\n;()]', "_", v) + '"'


def is_executable_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    if IS_WINDOWS:
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        return os.path.splitext(path)[1].lower() in exts
    return os.access(path, os.X_OK)


# ---------------------------------------------------------------------------------------------- process control
def kill_process_tree(pid: int, force: bool = True):
    """Terminate ``pid`` and its children (Windows has no process groups)."""
    if IS_WINDOWS:
        args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        try:
            subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=0x08000000, timeout=5)      # CREATE_NO_WINDOW
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.killpg(pid, SIGKILL if force else SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def signal_group(pid: int, sig) -> None:
    """Send ``sig`` to the process group of ``pid`` (Windows: terminate the process tree)."""
    if IS_WINDOWS:
        kill_process_tree(pid, force=True)
        return
    try:
        os.killpg(pid, sig)
    except (OSError, ProcessLookupError):
        pass


def new_session_kwargs() -> dict:
    """Popen kwargs that put the child in its own session/process group."""
    if IS_WINDOWS:
        return {"creationflags": 0x00000200 | 0x08000000}          # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    return {"start_new_session": True}


def detached_kwargs() -> dict:
    """Popen kwargs for a background daemon that outlives this process."""
    if IS_WINDOWS:
        return {"creationflags": 0x00000200 | 0x08000000, "close_fds": True}
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.restype = ctypes.c_void_p
            h = k.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
            k.CloseHandle(ctypes.c_void_p(h))
            return bool(ok) and code.value == 259              # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------------------------- threaded I/O helpers
class Pump:
    """Runs a blocking ``read() -> bytes`` in a daemon thread and exposes the data through a socket pair, so a handle
    that ``select`` cannot wait on (console, ConPTY pipe, subprocess pipe on Windows) behaves like any other fd.

    ``recv()`` returns bytes, ``b""`` when nothing is available right now, or ``None`` at end of stream.
    """

    def __init__(self, read: Callable[[], bytes], name: str = "pump", chunk: int = 65536):
        self._read = read
        self.chunk = chunk
        self.rsock, self.wsock = socket.socketpair()
        self.rsock.setblocking(False)
        self.eof = False
        self.closed = False
        self.done = threading.Event()               # reader thread finished (EOF or error)
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.closed:
                data = self._read()
                if not data:
                    break
                self.wsock.sendall(data)            # blocks (back-pressure) while the loop is busy
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.wsock.close()                  # the selector wakes up and recv() reports EOF
            except OSError:
                pass
            self.done.set()

    def fileno(self) -> int:
        return self.rsock.fileno()

    def recv(self, n: int = 65536) -> Optional[bytes]:
        if self.eof:
            return None
        try:
            d = self.rsock.recv(n)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            self.eof = True
            return None
        if not d:
            self.eof = True
            return None
        return d

    def close(self):
        self.closed = True
        for s in (self.rsock, self.wsock):
            try:
                s.close()
            except OSError:
                pass


class ThreadWriter:
    """Serialises writes to a handle whose ``write`` may block (pipe to a child that is not reading)."""

    def __init__(self, write: Callable[[bytes], None], name: str = "writer"):
        self._write = write
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.dead = False
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            data = self.q.get()
            if data is None:
                break
            try:
                self._write(data)
            except (OSError, ValueError):
                self.dead = True
                break

    def put(self, data: bytes):
        if not self.dead and data:
            self.q.put(data)

    def close(self):
        self.q.put(None)


class Waker:
    """Lets other threads (plugins, signal handlers) interrupt the selector loop."""

    def __init__(self):
        self.r, self.w = socket.socketpair()
        self.r.setblocking(False)
        self.w.setblocking(False)

    def wake(self):
        try:
            self.w.send(b"x")
        except (OSError, ValueError):
            pass

    def drain(self):
        try:
            self.r.recv(4096)
        except (OSError, ValueError):
            pass

    def close(self):
        for s in (self.r, self.w):
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------- session endpoints
KEY_LEN = 32


def endpoint_ext() -> str:
    return ".port" if USE_TCP else ".sock"


def read_port_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        int(d["port"])
        str(d["key"])
        return d
    except FileNotFoundError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ConnectionRefusedError("bad endpoint file %s: %s" % (path, e))


def connect_endpoint(path: str, timeout: float = 3.0) -> socket.socket:
    """Connect to a session endpoint: ``*.port`` files describe a loopback TCP listener, anything else is a unix socket."""
    if path.endswith(".port"):
        d = read_port_file(path)
        s = socket.create_connection(("127.0.0.1", int(d["port"])), timeout)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(str(d["key"]).encode("ascii"))
        except OSError:
            s.close()
            raise
        return s
    if not hasattr(socket, "AF_UNIX"):
        raise ConnectionRefusedError("unix sockets are not available on this platform")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError:
        s.close()
        raise
    return s


def endpoint_alive(path: str, timeout: float = 0.5) -> bool:
    try:
        s = connect_endpoint(path, timeout)
    except OSError:
        return False
    s.close()
    return True


class Listener:
    """Server side of an endpoint (unix socket, or loopback TCP with a shared key for Windows)."""

    def __init__(self, path: str):
        self.path = path
        self.tcp = path.endswith(".port")
        self.key = ""
        if self.tcp:
            self.key = secrets.token_hex(KEY_LEN // 2)
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                srv.setsockopt(socket.SOL_SOCKET, excl, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(16)
            self.port = srv.getsockname()[1]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"port": self.port, "key": self.key, "pid": os.getpid()}, f)
            os.replace(tmp, path)
        else:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            srv.listen(16)
        srv.setblocking(False)
        self.sock = srv

    def accept(self) -> Optional[socket.socket]:
        """Accept one connection (None if it failed authentication or nothing is pending)."""
        try:
            conn, _ = self.sock.accept()
        except (BlockingIOError, InterruptedError, OSError):
            return None
        if self.tcp:
            if not self._authenticate(conn):
                try:
                    conn.close()
                except OSError:
                    pass
                return None
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        conn.setblocking(False)
        return conn

    def _authenticate(self, conn: socket.socket) -> bool:
        conn.settimeout(1.0)
        buf = b""
        try:
            while len(buf) < KEY_LEN:
                chunk = conn.recv(KEY_LEN - len(buf))
                if not chunk:
                    return False
                buf += chunk
        except (OSError, socket.timeout):
            return False
        return hmac.compare_digest(buf, self.key.encode("ascii"))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------- console / terminal
def _binary_stdio():
    """Windows: stdin/stdout must not translate line ends or treat Ctrl-Z as EOF."""
    if IS_WINDOWS:
        try:
            import msvcrt
            for fd in (0, 1, 2):
                msvcrt.setmode(fd, os.O_BINARY)
        except (ImportError, OSError):
            pass


def term_size(fd: int = 1) -> Tuple[int, int]:
    if not IS_WINDOWS and fcntl is not None:
        import struct
        try:
            data = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            rows, cols, _, _ = struct.unpack("HHHH", data)
            if rows and cols:
                return cols, rows
        except OSError:
            pass
    s = shutil.get_terminal_size((80, 24))
    return s.columns, s.lines


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD), ("wAttributes", wintypes.WORD),
                ("srWindow", _SMALL_RECT), ("dwMaximumWindowSize", _COORD)]


class _WinConsole:
    """Raw-mode handling of the Windows console (ENABLE_VIRTUAL_TERMINAL_INPUT/PROCESSING)."""

    STD_INPUT, STD_OUTPUT = -10, -11
    IN_PROCESSED, IN_LINE, IN_ECHO, IN_WINDOW, IN_MOUSE, IN_QUICK_EDIT, IN_EXTENDED, IN_VT = 0x1, 0x2, 0x4, 0x8, 0x10, 0x40, 0x80, 0x200
    OUT_PROCESSED, OUT_WRAP, OUT_VT, OUT_NO_AUTO_CR = 0x1, 0x2, 0x4, 0x8

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.k
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.ReadConsoleW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.GetConsoleCP.restype = wintypes.UINT
        k.GetConsoleOutputCP.restype = wintypes.UINT
        k.SetConsoleCP.argtypes = [wintypes.UINT]
        k.SetConsoleOutputCP.argtypes = [wintypes.UINT]
        k.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO)]
        k.SetConsoleScreenBufferSize.argtypes = [wintypes.HANDLE, _COORD]
        self._COORD = _COORD
        self._CSBI = _CONSOLE_SCREEN_BUFFER_INFO
        self.hin = k.GetStdHandle(self.STD_INPUT)
        self.hout = k.GetStdHandle(self.STD_OUTPUT)
        self.wintypes = wintypes
        self.saved_in = self.saved_out = None
        self.saved_cp = None
        self._hi = ""

    def mode(self, h) -> Optional[int]:
        m = self.wintypes.DWORD()
        if self.k.GetConsoleMode(h, self.ct.byref(m)):
            return m.value
        return None

    def is_console_in(self) -> bool:
        return self.mode(self.hin) is not None

    def window_size(self) -> Optional[Tuple[int, int]]:
        """The console's currently visible window size (columns, rows), independent of the
        screen buffer's own dimensions -- None if it can't be read (not a real console)."""
        info = self._CSBI()
        if not self.k.GetConsoleScreenBufferInfo(self.hout, self.ct.byref(info)):
            return None
        return (info.srWindow.Right - info.srWindow.Left + 1,
                info.srWindow.Bottom - info.srWindow.Top + 1)

    def sync_buffer_to_window(self):
        """Best-effort: grow the console's screen buffer so it is at least as wide/tall as the
        currently visible window.

        A screen buffer narrower than the window makes the console itself wrap output early --
        every line pytermwm draws assuming the window's real width gets cut and wrapped a second
        time by the console host, which looks exactly like garbled/misaligned rendering right
        after attaching from a terminal that starts already maximized or otherwise bigger than the
        console's buffer. Some console hosts only reconcile buffer and window size on an actual
        interactive resize, which is why nudging the window (even by one pixel and back) has been
        the known workaround; this does the same reconciliation up front instead. Only ever grows
        the buffer, never shrinks it (shrinking below the current window can fail or clip it).
        """
        info = self._CSBI()
        if not self.k.GetConsoleScreenBufferInfo(self.hout, self.ct.byref(info)):
            return
        ww = info.srWindow.Right - info.srWindow.Left + 1
        wh = info.srWindow.Bottom - info.srWindow.Top + 1
        nx, ny = max(info.dwSize.X, ww), max(info.dwSize.Y, wh)
        if (nx, ny) != (info.dwSize.X, info.dwSize.Y):
            self.k.SetConsoleScreenBufferSize(self.hout, self._COORD(nx, ny))

    def enter(self):
        try:
            self.sync_buffer_to_window()
        except OSError:
            pass
        self.saved_in, self.saved_out = self.mode(self.hin), self.mode(self.hout)
        self.saved_cp = (self.k.GetConsoleCP(), self.k.GetConsoleOutputCP())
        self.k.SetConsoleCP(65001)
        self.k.SetConsoleOutputCP(65001)
        if self.saved_in is not None:
            m = self.saved_in & ~(self.IN_PROCESSED | self.IN_LINE | self.IN_ECHO | self.IN_QUICK_EDIT | self.IN_WINDOW)
            m |= self.IN_VT | self.IN_EXTENDED | self.IN_MOUSE
            self.k.SetConsoleMode(self.hin, m)
        if self.saved_out is not None:
            m = self.saved_out | self.OUT_PROCESSED | self.OUT_VT
            if not self.k.SetConsoleMode(self.hout, m | self.OUT_NO_AUTO_CR):
                self.k.SetConsoleMode(self.hout, m)

    def leave(self):
        if self.saved_in is not None:
            self.k.SetConsoleMode(self.hin, self.saved_in)
        if self.saved_out is not None:
            self.k.SetConsoleMode(self.hout, self.saved_out)
        if self.saved_cp:
            self.k.SetConsoleCP(self.saved_cp[0])
            self.k.SetConsoleOutputCP(self.saved_cp[1])

    def read_utf8(self) -> bytes:
        """Blocking read of console input as UTF-8 (VT sequences included).  ``b""`` = EOF."""
        buf = self.ct.create_unicode_buffer(4096)
        n = self.wintypes.DWORD()
        if not self.k.ReadConsoleW(self.hin, buf, 4096, self.ct.byref(n), None) or n.value == 0:
            return b""
        text = self._hi + buf[:n.value]              # buf[:n] (not .value) so embedded NULs (Ctrl-Space) survive
        self._hi = ""
        if text and "\ud800" <= text[-1] <= "\udbff":  # a surrogate pair split across two reads
            self._hi, text = text[-1], text[:-1]
        return text.encode("utf-8", "replace")


class StdinReader:
    """The keyboard/stdin as something ``select`` can wait on: ``fileno()`` + ``read() -> bytes | None``.

    POSIX: the tty itself (fd 0).  Windows: a helper thread reading the console (or a redirected stdin)."""

    def __init__(self, console: Optional[_WinConsole] = None):
        self.pump = None
        if THREADED_IO:
            if IS_WINDOWS and console is not None and console.is_console_in():
                self.pump = Pump(console.read_utf8, "stdin")
            else:
                self.pump = Pump(lambda: _safe_read(0), "stdin")
        else:
            os.set_blocking(0, False)

    def fileno(self) -> int:
        return self.pump.fileno() if self.pump else 0

    def read(self) -> Optional[bytes]:
        if self.pump:
            return self.pump.recv()
        try:
            data = os.read(0, 65536)
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return data or None

    def close(self):
        if self.pump:
            self.pump.close()
        else:
            try:
                os.set_blocking(0, True)          # the tty is shared with the parent shell: hand it back blocking
            except OSError:
                pass


def _safe_read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except OSError:
        return b""


class RawTerminal:
    """Context manager: raw mode + alternate screen (works on POSIX ttys and the Windows console)."""

    ENTER = "\x1b[?1049h\x1b[?7l\x1b[?25l\x1b[2J\x1b[H\x1b[?2004h\x1b[?1004h"
    MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
    MOUSE_OFF = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"
    LEAVE = "\x1b[?1004l\x1b[?2004l\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"

    def __init__(self, fd_in: int = 0, fd_out: int = 1, mouse: bool = True, palette: Optional[Dict[int, Tuple[int, int, int]]] = None):
        self.fd_in, self.fd_out = fd_in, fd_out
        self.saved = None
        self.mouse = mouse
        self.console: Optional[_WinConsole] = None
        self.palette = palette

    def _palette_seq(self) -> str:
        """``ESC ] P nrrggbb`` per entry: the Linux console's private palette-redefinition escape (see
        ``terminal.console_palette``). Not sent unless a caller explicitly opted in via ``palette=``."""
        return "".join("\x1b]P%x%02x%02x%02x" % (i, r, g, b) for i, (r, g, b) in sorted(self.palette.items()))

    def __enter__(self):
        if IS_WINDOWS:
            _binary_stdio()
            self.console = _WinConsole()
            self.console.enter()
        else:
            self.saved = termios.tcgetattr(self.fd_in)
            tty.setraw(self.fd_in)
        self._write(self.ENTER + (self.MOUSE_ON if self.mouse else "") + (self._palette_seq() if self.palette else ""))
        return self

    def _write(self, s: str):
        data = s.encode()
        while data:
            try:
                n = os.write(self.fd_out, data)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            data = data[n:]

    def set_mouse(self, on: bool):
        self.mouse = on
        self._write(self.MOUSE_ON if on else self.MOUSE_OFF)

    def __exit__(self, *a):
        try:
            self._write(self.MOUSE_OFF + ("\x1b]R" if self.palette else "") + self.LEAVE)
        except OSError:
            pass
        if IS_WINDOWS:
            if self.console is not None:
                self.console.leave()
        elif self.saved is not None:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self.saved)
            try:
                os.set_blocking(self.fd_in, True)
            except OSError:
                pass

    def stdin_reader(self) -> StdinReader:
        return StdinReader(self.console)


def write_all(fd: int, data: bytes):
    """Write everything to ``fd`` (which may be non-blocking)."""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            time.sleep(0.002)
            continue
        view = view[n:]
