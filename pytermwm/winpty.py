"""Windows pseudo console (ConPTY) window source, written against ``ctypes`` only (no pywinpty dependency).

Needs Windows 10 1809 or newer.  Importing this module is safe everywhere (ctypes is only touched when a
pseudo console is actually created), so the pure helpers can be unit-tested on any platform.

How it fits the selector based server: the ConPTY output pipe cannot be ``select``-ed, so a helper thread reads it
and hands the bytes over through a socket pair (:class:`pytermwm.compat.Pump`).  Input is written by a writer thread
so a slow child can never block the event loop.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

from . import compat
from .compat import Pump, ThreadWriter

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF


# ---------------------------------------------------------------------------------------------- pure helpers
def build_env_block(env: Dict[str, str]) -> str:
    """The double-NUL terminated, case-insensitively sorted environment block CreateProcessW expects."""
    items = sorted(((str(k), str(v)) for k, v in env.items()), key=lambda kv: kv[0].upper())
    return "".join("%s=%s\0" % kv for kv in items) + "\0"


resolve_argv = compat.resolve_argv


def command_line(argv: List[str]) -> str:
    return subprocess.list2cmdline(resolve_argv(argv))


# ---------------------------------------------------------------------------------------------- ctypes plumbing
class _Api:
    """Lazy holder of the kernel32 entry points and structures (Windows only)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ct, self.wt = ctypes, wt
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        HANDLE, DWORD, BOOL, LPVOID = wt.HANDLE, wt.DWORD, wt.BOOL, ctypes.c_void_p

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [("cb", DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                        ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD), ("dwYSize", DWORD),
                        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
                        ("dwFlags", DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                        ("lpReserved2", LPVOID), ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]

        self.COORD, self.STARTUPINFOEXW, self.PROCESS_INFORMATION = COORD, STARTUPINFOEXW, PROCESS_INFORMATION
        P = ctypes.POINTER
        k.CreatePipe.argtypes = [P(HANDLE), P(HANDLE), LPVOID, DWORD]
        k.CreatePipe.restype = BOOL
        k.CloseHandle.argtypes = [HANDLE]
        k.CloseHandle.restype = BOOL
        k.CreatePseudoConsole.argtypes = [COORD, HANDLE, HANDLE, DWORD, P(LPVOID)]
        k.CreatePseudoConsole.restype = ctypes.c_long                 # HRESULT
        k.ResizePseudoConsole.argtypes = [LPVOID, COORD]
        k.ResizePseudoConsole.restype = ctypes.c_long
        k.ClosePseudoConsole.argtypes = [LPVOID]
        k.ClosePseudoConsole.restype = None
        k.InitializeProcThreadAttributeList.argtypes = [LPVOID, DWORD, DWORD, P(ctypes.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = BOOL
        k.UpdateProcThreadAttribute.argtypes = [LPVOID, DWORD, ctypes.c_size_t, LPVOID, ctypes.c_size_t, LPVOID, LPVOID]
        k.UpdateProcThreadAttribute.restype = BOOL
        k.DeleteProcThreadAttributeList.argtypes = [LPVOID]
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID, wt.LPCWSTR, LPVOID, LPVOID]
        k.CreateProcessW.restype = BOOL
        k.ReadFile.argtypes = [HANDLE, LPVOID, DWORD, P(DWORD), LPVOID]
        k.ReadFile.restype = BOOL
        k.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, P(DWORD), LPVOID]
        k.WriteFile.restype = BOOL
        k.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        k.WaitForSingleObject.restype = DWORD
        k.GetExitCodeProcess.argtypes = [HANDLE, P(DWORD)]
        k.GetExitCodeProcess.restype = BOOL
        k.TerminateProcess.argtypes = [HANDLE, wt.UINT]
        k.TerminateProcess.restype = BOOL

    def winerror(self, what: str) -> OSError:
        code = self.ct.get_last_error()
        return OSError(code, "%s failed: %s" % (what, self.ct.FormatError(code).strip()))


_API: Optional[_Api] = None


def api() -> _Api:
    global _API
    if _API is None:
        _API = _Api()
    return _API


class _ProcProxy:
    """The bits of ``subprocess.Popen`` the window manager relies on (poll / wait / pid / returncode).

    The process only counts as finished once its output has been fully drained, so no trailing output is lost."""

    def __init__(self, src: "ConPtySource"):
        self._src = src
        self.pid = src.pid

    @property
    def returncode(self) -> Optional[int]:
        return self.poll()

    def poll(self) -> Optional[int]:
        s = self._src
        if s.exited.is_set() and s.pump.done.is_set():
            return s.exit_code
        return None

    def wait(self, timeout: Optional[float] = None) -> int:
        end = None if timeout is None else time.time() + timeout
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if end is not None and time.time() >= end:
                raise subprocess.TimeoutExpired(self._src.argv, timeout)
            time.sleep(0.01)


# ---------------------------------------------------------------------------------------------- the source
from .window import Source  # noqa: E402  (window imports this module lazily, so no cycle at import time)


class ConPtySource(Source):
    """A process attached to a Windows pseudo console."""
    tty = True

    def __init__(self, argv: List[str], cwd: Optional[str], env: Optional[dict], rows: int, cols: int):
        a = api()
        ct, wt, k = a.ct, a.wt, a.k
        self.argv = list(argv)
        self.cwd = cwd
        self._lock = threading.Lock()
        self._pty_closed = False
        self.exited = threading.Event()
        self.exit_code = None
        self.wbuf = b""
        self._eof = False
        self.alive = True

        e = dict(os.environ)
        e.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "PYTERMWM": "1"})
        e.pop("TERM_PROGRAM", None)
        if env:
            e.update({str(kk): str(v) for kk, v in env.items()})
        e["LINES"], e["COLUMNS"] = str(rows), str(cols)

        in_r, in_w, out_r, out_w = wt.HANDLE(), wt.HANDLE(), wt.HANDLE(), wt.HANDLE()
        if not k.CreatePipe(ct.byref(in_r), ct.byref(in_w), None, 0) or not k.CreatePipe(ct.byref(out_r), ct.byref(out_w), None, 0):
            raise a.winerror("CreatePipe")
        hpc = ct.c_void_p()
        hr = k.CreatePseudoConsole(a.COORD(max(1, cols), max(1, rows)), in_r, out_w, 0, ct.byref(hpc))
        if hr != 0:
            for h in (in_r, in_w, out_r, out_w):
                k.CloseHandle(h)
            raise OSError("CreatePseudoConsole failed (HRESULT 0x%08x); ConPTY needs Windows 10 1809 or newer" % (hr & 0xFFFFFFFF))
        self._hpc = hpc
        # the pseudo console now owns its ends of the pipes
        k.CloseHandle(in_r)
        k.CloseHandle(out_w)
        self._in_w, self._out_r = in_w, out_r

        size = ct.c_size_t(0)
        k.InitializeProcThreadAttributeList(None, 1, 0, ct.byref(size))       # fails on purpose: reports the size
        attr_buf = ct.create_string_buffer(size.value)
        attr = ct.c_void_p(ct.addressof(attr_buf))
        if not k.InitializeProcThreadAttributeList(attr, 1, 0, ct.byref(size)):
            self._abort_create(a)
            raise a.winerror("InitializeProcThreadAttributeList")
        if not k.UpdateProcThreadAttribute(attr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc, ct.sizeof(ct.c_void_p), None, None):
            k.DeleteProcThreadAttributeList(attr)
            self._abort_create(a)
            raise a.winerror("UpdateProcThreadAttribute")

        si = a.STARTUPINFOEXW()
        si.StartupInfo.cb = ct.sizeof(a.STARTUPINFOEXW)
        si.lpAttributeList = attr
        pi = a.PROCESS_INFORMATION()
        cmdline = ct.create_unicode_buffer(command_line(self.argv))
        block = build_env_block(e)
        env_buf = (ct.c_wchar * len(block))(*block)
        ok = k.CreateProcessW(None, cmdline, None, None, False, EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                              ct.cast(env_buf, ct.c_void_p), cwd or None, ct.byref(si), ct.byref(pi))
        err = a.winerror("CreateProcess(%s)" % self.argv[0]) if not ok else None
        k.DeleteProcThreadAttributeList(attr)
        if err is not None:
            self._abort_create(a)
            raise err
        k.CloseHandle(pi.hThread)
        self._hproc = pi.hProcess
        self.pid = pi.dwProcessId
        self.proc = _ProcProxy(self)

        self.pump = Pump(self._read_blocking, "conpty-read")
        self.master = self.pump.fileno()
        self._writer = ThreadWriter(self._write_blocking, "conpty-write")
        threading.Thread(target=self._watch, name="conpty-wait", daemon=True).start()

    # ---- blocking primitives used by the helper threads
    def _abort_create(self, a: _Api):
        a.k.ClosePseudoConsole(self._hpc)
        for h in (self._in_w, self._out_r):
            a.k.CloseHandle(h)

    def _read_blocking(self) -> bytes:
        a = api()
        buf = a.ct.create_string_buffer(65536)
        n = a.wt.DWORD(0)
        if not a.k.ReadFile(self._out_r, buf, 65536, a.ct.byref(n), None) or n.value == 0:
            return b""
        return buf.raw[:n.value]

    def _write_blocking(self, data: bytes):
        a = api()
        view = memoryview(data)
        while view:
            n = a.wt.DWORD(0)
            chunk = bytes(view[:16384])
            if not a.k.WriteFile(self._in_w, chunk, len(chunk), a.ct.byref(n), None):
                raise OSError("WriteFile failed")
            view = view[n.value:]

    def _watch(self):
        """Wait for the process, then close the pseudo console so the output pipe reaches EOF."""
        a = api()
        a.k.WaitForSingleObject(self._hproc, INFINITE)
        code = a.wt.DWORD(1)
        a.k.GetExitCodeProcess(self._hproc, a.ct.byref(code))
        self.exit_code = int(code.value)
        self.exited.set()
        self._close_pty()

    def _close_pty(self):
        with self._lock:
            if self._pty_closed:
                return
            self._pty_closed = True
        api().k.ClosePseudoConsole(self._hpc)              # may block until the output is drained: reader thread keeps going

    # ---- Source interface
    def read_fds(self):
        return {self.master: "out"} if not self._eof else {}

    def read(self, fd):
        d = self.pump.recv()
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

    def resize(self, rows, cols):
        if self._pty_closed:
            return
        a = api()
        a.k.ResizePseudoConsole(self._hpc, a.COORD(max(1, cols), max(1, rows)))

    def check_exit(self) -> bool:
        return not self.alive

    def finish(self):
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        self.alive = False

    def cwd_live(self) -> Optional[str]:
        return self.cwd

    def close(self):
        if not self.exited.is_set():
            compat.kill_process_tree(self.pid)
        self.alive = False
        self._eof = True
        threading.Thread(target=self._final_close, name="conpty-close", daemon=True).start()

    def _final_close(self):
        a = api()
        self.exited.wait(2.0)
        self._close_pty()
        self.pump.done.wait(2.0)
        self._writer.close()
        self.pump.close()
        for h in (self._in_w, self._out_r, self._hproc):
            try:
                a.k.CloseHandle(h)
            except Exception:
                pass

    def describe(self):
        return {"pid": self.pid, "argv": self.argv}


# ---------------------------------------------------------------------------------------------- self test
def selftest(timeout: float = 10.0) -> str:
    """Spawn ``cmd /c echo ...`` in a pseudo console and check its output arrives (used by ``pytermwm doctor``)."""
    marker = "pytermwm-conpty-ok"
    src = ConPtySource([os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", "echo " + marker], None, None, 24, 80)
    got = b""
    end = time.time() + timeout
    try:
        while time.time() < end:
            d = src.pump.recv()
            if d is None:
                break
            got += d
            if not d:
                time.sleep(0.02)
        if marker.encode() not in got:
            raise RuntimeError("no output from the pseudo console (got %r)" % got[-200:])
        src.proc.wait(timeout=3)
        return "ConPTY works (exit code %s)" % src.exit_code
    finally:
        src.close()
