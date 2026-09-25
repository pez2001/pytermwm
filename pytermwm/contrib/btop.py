"""btop-like system monitor window: per-core CPU, memory, network, disks and a sortable process list.

    plugins: [btop]        :btop          keys: c/m/p/n sort, k kill (asks first), / filter, q close
"""
from __future__ import annotations

import collections
import os
import signal
import time
from typing import Deque, List

from pytermwm.builtin_windows import bold, dim
from pytermwm.charts import ansi_color, braille_line, gauge, spark, bar, color_ramp
from pytermwm.commands import CommandError
from pytermwm.sysinfo import SAMPLER, human_bytes, human_rate
from pytermwm.window import InternalWindow

SORTS = ("cpu", "mem", "pid", "name")


class BtopWindow(InternalWindow):
    kind = "btop"
    refresh_interval = 1.0

    def __init__(self, wid, title, rows, cols, wm=None, sort="cpu", interval=1.0, **opts):
        super().__init__(wid, title or "btop", rows, cols, **opts)
        self._wm = wm
        self.sort = sort if sort in SORTS else "cpu"
        self.refresh_interval = float(interval)
        self.filter = ""
        self.sel = 0
        self.cpu_hist: Deque[float] = collections.deque(maxlen=300)
        self.rx_hist: Deque[float] = collections.deque(maxlen=300)
        self.tx_hist: Deque[float] = collections.deque(maxlen=300)
        self._procs: List[dict] = []
        self._last_sample = 0.0

    # ---------------------------------------------------------------- data
    def sample(self):
        now = time.time()
        if now - self._last_sample < 0.5 and self.cpu_hist:
            return
        self._last_sample = now
        total, cores = SAMPLER.cpu()
        self.cpu_hist.append(total)
        rx, tx = SAMPLER.net()
        self.rx_hist.append(rx)
        self.tx_hist.append(tx)
        procs = SAMPLER.processes(200, self.sort)
        if self.filter:
            f = self.filter.lower()
            procs = [p for p in procs if f in p["name"].lower() or f in str(p["pid"])]
        self._procs = procs
        self.sel = min(self.sel, max(0, len(procs) - 1))

    # ---------------------------------------------------------------- render
    def render(self, cols, rows):
        self.sample()
        out: List[str] = []
        total, cores = SAMPLER.cpu()
        mem = SAMPLER.mem()
        load = SAMPLER.load()
        out.append(bold("CPU ") + gauge(total, max(10, cols - 30), "%4.1f%%" % total)
                   + dim("  load %.2f %.2f %.2f" % load))
        if cores:
            per = max(1, min(4, cols // 24))
            cw = max(8, cols // per - 12)
            line = []
            for i, c in enumerate(cores):
                line.append("%2d %s" % (i, gauge(c, cw, "%3.0f%%" % c)))
                if len(line) == per:
                    out.append("  ".join(line))
                    line = []
            if line:
                out.append("  ".join(line))
        hist_h = max(2, min(6, rows // 6))
        if hist_h and cols > 30:
            out.extend(ansi_color(l, 6) for l in braille_line(list(self.cpu_hist), cols, hist_h, 0, 100))
        mt = mem["total"] or 1
        out.append(bold("MEM ") + gauge(100.0 * mem["used"] / mt, max(10, cols - 40),
                                          "%s/%s" % (human_bytes(mem["used"]), human_bytes(mem["total"])))
                   + (dim("  swap %s/%s" % (human_bytes(mem["swap_used"]), human_bytes(mem["swap_total"])) if mem["swap_total"] else "")))
        rx = self.rx_hist[-1] if self.rx_hist else 0
        tx = self.tx_hist[-1] if self.tx_hist else 0
        out.append(bold("NET ") + "↓ %-10s %s  ↑ %-10s %s" % (human_rate(rx), spark(list(self.rx_hist), 20),
                                                              human_rate(tx), spark(list(self.tx_hist), 20)))
        try:
            t, u, f = SAMPLER.disk("/")
            out.append(bold("DSK ") + gauge(100.0 * u / max(1, t), max(10, cols - 40), "%s/%s" % (human_bytes(u), human_bytes(t))))
        except Exception:
            pass
        out.append("")
        head = "%7s %-20s %5s %7s %9s" % ("PID", "NAME", "ST", "CPU%", "MEM")
        out.append(bold(head[:cols]))
        room = rows - len(out) - 1
        start = max(0, min(self.sel - room + 1, len(self._procs) - room)) if room > 0 else 0
        for i, p in enumerate(self._procs[start:start + max(0, room)]):
            line = "%7d %-20s %5s %6.1f%% %9s" % (p["pid"], p["name"][:20], p["state"], p["cpu"], human_bytes(p["rss"]))
            line = line[:cols]
            if start + i == self.sel:
                line = "\x1b[7m%-*s\x1b[27m" % (cols, line)
            elif p["cpu"] > 50:
                line = ansi_color(line, 1)
            out.append(line)
        while len(out) < rows - 1:
            out.append("")
        out = out[:rows - 1]
        out.append(dim("[c]pu [m]em [p]id [n]ame sort=%s | [k]ill [/]filter%s | %d procs" % (
            self.sort, "=" + self.filter if self.filter else "", len(self._procs))))
        return out

    # ---------------------------------------------------------------- keys
    def handle_key(self, key, raw):
        if key in ("c", "m", "p", "n"):
            self.sort = {"c": "cpu", "m": "mem", "p": "pid", "n": "name"}[key]
            self._last_sample = 0
        elif key in ("Down", "j"):
            self.sel = min(len(self._procs) - 1, self.sel + 1)
        elif key == "Up":
            self.sel = max(0, self.sel - 1)
        elif key == "PageUp":
            self.sel = max(0, self.sel - 10)
        elif key == "PageDown":
            self.sel = min(len(self._procs) - 1, self.sel + 10)
        elif key == "k":
            if self._procs:
                p = self._procs[self.sel]
                self._wm.execute("dialog confirm 'Send SIGTERM to %d (%s)?' 'btop-kill %d'" % (p["pid"], p["name"].replace("'", ""), p["pid"]))
        elif key == "/":
            self._wm.prompt.open(self.filter, "custom", "/", on_submit=self.set_filter)
        elif key == "q":
            self._wm.close_window(self.id)
        else:
            return False
        self.dirty = True
        return True

    def set_filter(self, text: str):
        self.filter = text.strip()
        self._last_sample = 0
        self.dirty = True


def setup(api):
    wm = api.wm

    def factory(wm_, wid, spec, rows, cols, opts):
        return BtopWindow(wid, spec.get("title") or "", rows, cols, wm=wm_, name=spec.get("name"),
                          sort=spec.get("sort", api.config.get("sort", "cpu")),
                          interval=spec.get("interval", api.config.get("interval", 1.0)), **opts)

    api.window_kind("btop", factory)

    def c_btop(wm_, args):
        w = wm_.create_window({"kind": "btop", "title": "btop", "sort": args[0] if args else "cpu"})
        return {"id": w.id}

    def c_kill(wm_, args):
        if not args:
            raise CommandError("usage: btop-kill <pid> [signal]")
        pid = int(args[0])
        sig = getattr(signal, "SIG" + args[1].upper().lstrip("SIG"), signal.SIGTERM) if len(args) > 1 else signal.SIGTERM
        if pid <= 1 or pid == os.getpid():
            raise CommandError("refusing to signal pid %d" % pid)
        try:
            os.kill(pid, sig)
        except OSError as e:
            raise CommandError("kill %d: %s" % (pid, e))
        wm_.message("sent %s to %d" % (sig.name, pid), "ok")

    api.command("btop", c_btop, usage="btop [cpu|mem|pid|name]", help="Open a system monitor window")
    api.command("btop-kill", c_kill, usage="btop-kill <pid> [signal]", help="Signal a process")
