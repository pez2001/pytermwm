"""Cheap system statistics (Linux /proc based with portable fallbacks)."""
from __future__ import annotations

import os
import shutil
import time

from .compat import IS_WINDOWS
from typing import Dict, List, Optional, Tuple


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


class Sampler:
    """Caches samples for ``min_interval`` seconds."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._cpu_prev: Optional[List[Tuple[int, int]]] = None
        self._cpu_pct: List[float] = []
        self._cpu_total = 0.0
        self._cpu_time = 0.0
        self._net_prev: Optional[Tuple[float, int, int]] = None
        self._net_rate = (0.0, 0.0)
        self._disk_cache: Dict[str, Tuple[float, tuple]] = {}
        self._io_prev = None
        self._io_rate = (0.0, 0.0)

    # ---------------------------------------------------------------- cpu
    def _cpu_lines(self):
        if IS_WINDOWS:
            from . import winsys
            t = winsys.cpu_times()
            return [("cpu", t[0], t[1])] if t else []
        txt = _read("/proc/stat")
        out = []
        if txt:
            for ln in txt.splitlines():
                if ln.startswith("cpu"):
                    parts = ln.split()
                    vals = [int(v) for v in parts[1:9]] if len(parts) > 8 else [int(v) for v in parts[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    out.append((parts[0], sum(vals), idle))
        return out

    def cpu(self) -> Tuple[float, List[float]]:
        """Return (total percent, per-core percents)."""
        now = time.time()
        if self._cpu_prev is not None and now - self._cpu_time < self.min_interval:
            return self._cpu_total, self._cpu_pct
        lines = self._cpu_lines()
        if not lines:
            try:
                l1 = os.getloadavg()[0]
                n = os.cpu_count() or 1
                self._cpu_total = min(100.0, l1 / n * 100)
            except (OSError, AttributeError):
                self._cpu_total = 0.0
            self._cpu_pct = []
            self._cpu_time = now
            return self._cpu_total, self._cpu_pct
        cur = [(t, i) for _n, t, i in lines]
        if self._cpu_prev is not None and len(self._cpu_prev) == len(cur):
            pcts = []
            for (t0, i0), (t1, i1) in zip(self._cpu_prev, cur):
                dt, di = t1 - t0, i1 - i0
                pcts.append(100.0 * (dt - di) / dt if dt > 0 else 0.0)
            self._cpu_total = pcts[0]
            self._cpu_pct = pcts[1:]
        self._cpu_prev = cur
        self._cpu_time = now
        return self._cpu_total, self._cpu_pct

    # ---------------------------------------------------------------- memory
    def mem(self) -> Dict[str, int]:
        if IS_WINDOWS:
            from . import winsys
            m = winsys.memory()
            if m:
                return m
        txt = _read("/proc/meminfo")
        d: Dict[str, int] = {}
        if txt:
            for ln in txt.splitlines():
                k, _, v = ln.partition(":")
                try:
                    d[k] = int(v.split()[0]) * 1024
                except (ValueError, IndexError):
                    pass
        total = d.get("MemTotal", 0)
        avail = d.get("MemAvailable", d.get("MemFree", 0))
        swap_t = d.get("SwapTotal", 0)
        swap_f = d.get("SwapFree", 0)
        return {"total": total, "available": avail, "used": max(0, total - avail),
                "swap_total": swap_t, "swap_used": max(0, swap_t - swap_f),
                "cached": d.get("Cached", 0), "buffers": d.get("Buffers", 0)}

    # ---------------------------------------------------------------- disk
    def disk(self, path: str = "/") -> Tuple[int, int, int]:
        now = time.time()
        c = self._disk_cache.get(path)
        if c and now - c[0] < 5:
            return c[1]
        try:
            u = shutil.disk_usage(path)
            v = (u.total, u.used, u.free)
        except OSError:
            v = (0, 0, 0)
        self._disk_cache[path] = (now, v)
        return v

    # ---------------------------------------------------------------- load / uptime
    def load(self) -> Tuple[float, float, float]:
        if IS_WINDOWS:                                   # no load average: approximate it with the CPU utilisation
            v = self.cpu()[0] / 100.0 * (os.cpu_count() or 1)
            return (v, v, v)
        try:
            return os.getloadavg()
        except (OSError, AttributeError):
            return (0.0, 0.0, 0.0)

    def uptime(self) -> float:
        if IS_WINDOWS:
            from . import winsys
            return winsys.uptime()
        t = _read("/proc/uptime")
        if t:
            try:
                return float(t.split()[0])
            except ValueError:
                pass
        return 0.0

    # ---------------------------------------------------------------- net
    def net_totals(self) -> Tuple[int, int]:
        if IS_WINDOWS:
            return 0, 0                                  # not available without extra dependencies
        txt = _read("/proc/net/dev")
        rx = tx = 0
        if txt:
            for ln in txt.splitlines()[2:]:
                name, _, rest = ln.partition(":")
                if name.strip() == "lo":
                    continue
                f = rest.split()
                if len(f) >= 9:
                    rx += int(f[0])
                    tx += int(f[8])
        return rx, tx

    def net(self) -> Tuple[float, float]:
        """bytes/s (rx, tx)."""
        now = time.time()
        rx, tx = self.net_totals()
        if self._net_prev is None:
            self._net_prev = (now, rx, tx)
            return self._net_rate
        t0, rx0, tx0 = self._net_prev
        if now - t0 >= self.min_interval:
            dt = now - t0
            self._net_rate = (max(0, rx - rx0) / dt, max(0, tx - tx0) / dt)
            self._net_prev = (now, rx, tx)
        return self._net_rate

    # ---------------------------------------------------------------- processes
    def processes(self, limit: int = 30, sort: str = "cpu") -> List[dict]:
        """Top processes (Linux /proc)."""
        procs = []
        if IS_WINDOWS:
            from . import winsys
            procs = winsys.processes()
            key = {"cpu": lambda p: -p["cpu"], "mem": lambda p: -p["rss"], "pid": lambda p: p["pid"],
                   "name": lambda p: p["name"].lower()}.get(sort, lambda p: -p["rss"])
            procs.sort(key=key)
            return procs[:limit]
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return procs
        now = time.time()
        if not hasattr(self, "_pp"):
            self._pp = {}
            self._pp_t = now
        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        dt = max(0.001, now - self._pp_t)
        newpp = {}
        for pid in pids:
            st = _read("/proc/%s/stat" % pid)
            if not st:
                continue
            try:
                rp = st.rindex(")")
                name = st[st.index("(") + 1:rp]
                f = st[rp + 2:].split()
                state = f[0]
                ut, stt = int(f[11]), int(f[12])
                rss = int(f[21]) * page
            except (ValueError, IndexError):
                continue
            cpu_ticks = ut + stt
            newpp[pid] = cpu_ticks
            prev = self._pp.get(pid)
            cpu = 0.0
            if prev is not None and now - self._pp_t > 0.05:
                cpu = 100.0 * (cpu_ticks - prev) / hz / dt
            procs.append({"pid": int(pid), "name": name, "state": state, "cpu": cpu, "rss": rss})
        if now - self._pp_t >= 0.5 or not self._pp:
            self._pp = newpp
            self._pp_t = now
        key = {"cpu": lambda p: -p["cpu"], "mem": lambda p: -p["rss"], "pid": lambda p: p["pid"],
               "name": lambda p: p["name"]}.get(sort, lambda p: -p["cpu"])
        procs.sort(key=key)
        return procs[:limit]


SAMPLER = Sampler()


def human_bytes(n: float, precision: int = 1) -> str:
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024.0:
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.*f%s" % (precision, n, unit))
        n /= 1024.0
    return "%.1fE" % n


def human_rate(n: float) -> str:
    return human_bytes(n) + "/s"


def human_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)
