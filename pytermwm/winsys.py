"""System statistics on Windows (ctypes + ``tasklist``), used by :mod:`pytermwm.sysinfo`.

The parsing helper is platform independent so it can be tested anywhere; the ctypes calls are only made on Windows.
"""
from __future__ import annotations

import csv
import io
import subprocess
import time
from typing import Dict, List, Optional, Tuple


def cpu_times() -> Optional[Tuple[int, int]]:
    """(total, idle) system times in 100 ns units (kernel time includes idle), or None."""
    try:
        import ctypes
        from ctypes import wintypes as wt

        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        if not k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        val = lambda f: (f.hi << 32) | f.lo
        return val(kernel) + val(user), val(idle)
    except Exception:
        return None


def memory() -> Optional[Dict[str, int]]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        if not k.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        swap_t = max(0, st.ullTotalPageFile - st.ullTotalPhys)
        swap_f = max(0, st.ullAvailPageFile - st.ullAvailPhys)
        return {"total": st.ullTotalPhys, "available": st.ullAvailPhys, "used": max(0, st.ullTotalPhys - st.ullAvailPhys),
                "swap_total": swap_t, "swap_used": max(0, swap_t - swap_f), "cached": 0, "buffers": 0}
    except Exception:
        return None


def uptime() -> float:
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetTickCount64.restype = ctypes.c_ulonglong
        return k.GetTickCount64() / 1000.0
    except Exception:
        return 0.0


def parse_tasklist(text: str) -> List[dict]:
    """Parse ``tasklist /FO CSV /NH`` output: "Image Name","PID","Session Name","Session#","Mem Usage"."""
    procs = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5:
            continue
        try:
            pid = int(row[1])
            digits = "".join(ch for ch in row[4] if ch.isdigit())
            rss = int(digits) * 1024 if digits else 0
        except ValueError:
            continue
        procs.append({"pid": pid, "name": row[0], "state": "R", "cpu": 0.0, "rss": rss})
    return procs


_cache: Tuple[float, List[dict]] = (0.0, [])


def processes(max_age: float = 2.0) -> List[dict]:
    global _cache
    now = time.time()
    if now - _cache[0] < max_age:
        return list(_cache[1])
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000).stdout
        _cache = (now, parse_tasklist(out))
    except (OSError, subprocess.SubprocessError):
        _cache = (now, [])
    return list(_cache[1])
