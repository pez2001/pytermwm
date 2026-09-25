"""Docker integration (uses the ``docker`` CLI, no python dependencies).

    plugins: [docker]                        (or {name: docker, interval: 3, bin: /usr/bin/docker})

Commands:  docker-ps [all]   docker-images   docker-stats   docker-logs <container> [tail]
           docker-exec <container> [cmd...]   docker-run <image> [args...]   docker-do <start|stop|restart|rm|pause|unpause|kill> <container>
The ``docker`` window lists containers/images with live status; keys: Tab mode, j/k select, l logs, e shell,
s start/stop, r restart, d remove, i inspect, a show all, q close.  Status segment: ``docker`` (running/total).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

from pytermwm.builtin_windows import bold, dim
from pytermwm.charts import ansi_color
from pytermwm.commands import CommandError
from pytermwm.statusline import Segment
from pytermwm.window import InternalWindow

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")
ACTIONS = ("start", "stop", "restart", "rm", "pause", "unpause", "kill")


class DockerClient:
    def __init__(self, binary: Optional[str] = None, host: Optional[str] = None):
        self.binary = binary or shutil.which("docker") or "docker"
        self.host = host

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def argv(self, *args: str) -> List[str]:
        a = [self.binary]
        if self.host:
            a += ["-H", self.host]
        return a + list(args)

    def run(self, *args: str, timeout: float = 15.0) -> str:
        if not self.available():
            raise CommandError("docker not found (install docker or set plugin option bin:)")
        try:
            p = subprocess.run(self.argv(*args), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CommandError("docker %s timed out" % args[0])
        if p.returncode != 0:
            raise CommandError("docker %s: %s" % (args[0], (p.stderr or p.stdout).strip().split("\n")[-1] or "failed"))
        return p.stdout

    def json_lines(self, *args: str) -> List[dict]:
        out = []
        for line in self.run(*args, "--format", "{{json .}}").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def containers(self, all_: bool = True) -> List[dict]:
        return self.json_lines("ps", *(["-a"] if all_ else []))

    def images(self) -> List[dict]:
        return self.json_lines("images")

    def stats(self) -> List[dict]:
        return self.json_lines("stats", "--no-stream")


def check_name(n: str) -> str:
    n = str(n)
    if not NAME_RE.match(n):
        raise CommandError("invalid container/image name: %r" % n)
    return n


class DockerWindow(InternalWindow):
    kind = "docker"
    refresh_interval = 1.0
    MODES = ("containers", "images", "stats")

    def __init__(self, wid, title, rows, cols, wm=None, plugin=None, mode="containers", **opts):
        super().__init__(wid, title or "docker", rows, cols, **opts)
        self._wm = wm
        self.p = plugin
        self.mode = mode if mode in self.MODES else "containers"
        self.sel = 0
        self.show_all = True

    def rows_data(self) -> List[dict]:
        d = self.p.data if self.p else {}
        if self.mode == "images":
            return d.get("images", [])
        if self.mode == "stats":
            return d.get("stats", [])
        cs = d.get("containers", [])
        return cs if self.show_all else [c for c in cs if str(c.get("State", "")).lower() == "running"]

    def render(self, cols, rows):
        d = self.p.data if self.p else {}
        head = " ".join((bold("[%s]" % m) if m == self.mode else dim(m)) for m in self.MODES)
        out = [head + dim("   Tab mode | l logs e shell s start/stop r restart d rm i inspect a all")]
        err = d.get("error")
        if err:
            out.append(ansi_color(err, 1))
            return out
        rowsd = self.rows_data()
        self.sel = max(0, min(self.sel, len(rowsd) - 1))
        if self.mode == "containers":
            out.append(bold("%-14s %-20s %-22s %-10s %s" % ("ID", "NAME", "IMAGE", "STATE", "STATUS")))
            for i, c in enumerate(rowsd):
                st = str(c.get("State", ""))
                col = 2 if st == "running" else 3 if st in ("paused", "restarting") else 1 if st in ("exited", "dead") else None
                line = "%-14s %-20s %-22s %-10s %s" % (str(c.get("ID", ""))[:12], str(c.get("Names", ""))[:20], str(c.get("Image", ""))[:22], st, c.get("Status", ""))
                out.append(self._row(line, i, cols, col))
        elif self.mode == "images":
            out.append(bold("%-40s %-12s %-14s %s" % ("REPOSITORY", "TAG", "ID", "SIZE")))
            for i, c in enumerate(rowsd):
                out.append(self._row("%-40s %-12s %-14s %s" % (str(c.get("Repository", ""))[:40], c.get("Tag", ""), str(c.get("ID", ""))[:12], c.get("Size", "")), i, cols, None))
        else:
            out.append(bold("%-20s %8s %-22s %-18s %s" % ("NAME", "CPU", "MEM", "NET I/O", "PIDS")))
            for i, c in enumerate(rowsd):
                out.append(self._row("%-20s %8s %-22s %-18s %s" % (str(c.get("Name", ""))[:20], c.get("CPUPerc", ""), c.get("MemUsage", ""), c.get("NetIO", ""), c.get("PIDs", "")), i, cols, None))
        if not rowsd:
            out.append(dim("  (nothing to show)"))
        age = time.time() - d.get("time", 0) if d.get("time") else None
        out.append("")
        out.append(dim("updated %s ago" % ("%.0fs" % age if age is not None else "never")))
        return out

    def _row(self, line, i, cols, col):
        line = line[:cols]
        if i == self.sel:
            return "\x1b[7m%-*s\x1b[27m" % (cols, line)
        return ansi_color(line, col) if col is not None else line

    def selected(self) -> Optional[dict]:
        r = self.rows_data()
        return r[self.sel] if r and 0 <= self.sel < len(r) else None

    def cname(self, c) -> str:
        return check_name(c.get("Names") or c.get("Name") or c.get("ID"))

    def handle_key(self, key, raw):
        wm = self._wm
        if key == "Tab":
            self.mode = self.MODES[(self.MODES.index(self.mode) + 1) % 3]
            self.sel = 0
            self.p.refresh_soon()
        elif key in ("j", "Down"):
            self.sel = min(len(self.rows_data()) - 1, self.sel + 1)
        elif key in ("k", "Up"):
            self.sel = max(0, self.sel - 1)
        elif key == "a":
            self.show_all = not self.show_all
        elif key in ("l", "e", "s", "r", "d", "i") and self.selected() and self.mode == "containers":
            c = self.selected()
            try:
                n = self.cname(c)
            except CommandError as e:
                wm.message(str(e), "err")
                return True
            if key == "l":
                wm.execute("docker-logs %s" % n)
            elif key == "e":
                wm.execute("docker-exec %s" % n)
            elif key == "s":
                wm.execute("docker-do %s %s" % ("stop" if str(c.get("State")) == "running" else "start", n))
            elif key == "r":
                wm.execute("docker-do restart %s" % n)
            elif key == "d":
                wm.execute("dialog confirm 'Remove container %s?' 'docker-do rm %s'" % (n, n))
            elif key == "i":
                wm.execute("docker-inspect %s" % n)
        elif key == "q":
            wm.close_window(self.id)
        else:
            return False
        self.dirty = True
        return True


class DockerPlugin:
    def __init__(self, api):
        self.api = api
        self.wm = api.wm
        self.client = DockerClient(api.config.get("bin"), api.config.get("host"))
        self.interval = float(api.config.get("interval", 3.0))
        self.data: Dict[str, object] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.mode_needed = {"containers"}
        self.thread = api.thread(self._poll)

    def refresh_soon(self):
        self.wake.set()

    def _poll(self):
        api = self.api
        while not api.stop_event.is_set():
            data: Dict[str, object] = {"time": time.time()}
            try:
                if not self.client.available():
                    raise CommandError("docker not found")
                data["containers"] = self.client.containers()
                wins = [w for w in list(self.wm.windows.values()) if w.kind == "docker"]
                modes = {w.mode for w in wins}
                if "images" in modes:
                    data["images"] = self.client.images()
                if "stats" in modes:
                    data["stats"] = self.client.stats()
            except CommandError as e:
                data["error"] = str(e)
            with self.lock:
                self.data = data
            api.call_soon(self._changed)
            self.wake.wait(self.interval)
            self.wake.clear()

    def _changed(self):
        self.wm.dirty = True
        for w in self.wm.windows.values():
            if w.kind == "docker":
                w.dirty = True

    def counts(self):
        cs = self.data.get("containers") or []
        return sum(1 for c in cs if str(c.get("State", "")).lower() == "running"), len(cs)


def setup(api):
    p = DockerPlugin(api)
    api.on_unload(p.wake.set)
    wm = api.wm

    def factory(wm_, wid, spec, rows, cols, opts):
        return DockerWindow(wid, spec.get("title") or "", rows, cols, wm=wm_, plugin=p, name=spec.get("name"),
                            mode=spec.get("mode", "containers"), **opts)

    api.window_kind("docker", factory)

    def seg(wm_, o):
        if p.data.get("error"):
            return Segment("docker: n/a", "dim", prio=2)
        run, tot = p.counts()
        if not p.data:
            return None
        return Segment("🐳 %d/%d" % (run, tot), "ok" if run else "dim", click="docker-ps", prio=3)

    api.segment("docker", seg)

    def names_completer(wm_, prev, partial):
        return [str(c.get("Names")) for c in (p.data.get("containers") or [])]

    def c_ps(wm_, args):
        return {"id": wm_.create_window({"kind": "docker", "mode": "containers", "title": "docker"}).id}

    def c_images(wm_, args):
        return {"id": wm_.create_window({"kind": "docker", "mode": "images", "title": "docker images"}).id}

    def c_stats(wm_, args):
        return {"id": wm_.create_window({"kind": "docker", "mode": "stats", "title": "docker stats"}).id}

    def spawn(wm_, argv, title):
        if not p.client.available():
            raise CommandError("docker not found")
        return {"id": wm_.create_window({"cmd": argv, "title": title, "keep": True, "pipe": False}).id}

    def c_logs(wm_, args):
        if not args:
            raise CommandError("usage: docker-logs <container> [tail-lines]")
        n = check_name(args[0])
        tail = str(int(args[1])) if len(args) > 1 else "200"
        return spawn(wm_, p.client.argv("logs", "-f", "--tail", tail, n), "logs %s" % n)

    def c_exec(wm_, args):
        if not args:
            raise CommandError("usage: docker-exec <container> [cmd...]")
        n = check_name(args[0])
        cmd = args[1:] or ["sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"]
        return spawn(wm_, p.client.argv("exec", "-it", n, *cmd), "exec %s" % n)

    def c_run(wm_, args):
        if not args:
            raise CommandError("usage: docker-run <image> [args...]")
        check_name(args[0])
        return spawn(wm_, p.client.argv("run", "--rm", "-it", *args), "run %s" % args[0])

    def c_inspect(wm_, args):
        if not args:
            raise CommandError("usage: docker-inspect <container|image>")
        n = check_name(args[0])
        text = p.client.run("inspect", n)
        w = wm_.create_window({"kind": "viewer", "title": "inspect %s" % n})
        w.feed_bytes(text.encode())
        return {"id": w.id}

    def c_do(wm_, args):
        if len(args) < 2 or args[0] not in ACTIONS:
            raise CommandError("usage: docker-do <%s> <container>" % "|".join(ACTIONS))
        action, n = args[0], check_name(args[1])

        def work():
            try:
                p.client.run(action, n)
                api.call_soon(wm_.message, "docker %s %s: ok" % (action, n), "ok", 2.0)
            except CommandError as e:
                api.call_soon(wm_.message, str(e), "err", 6.0)
            p.refresh_soon()
        api.thread(work)
        return "docker %s %s ..." % (action, n)

    ac = lambda wm_, prev, partial: list(ACTIONS) if not prev else names_completer(wm_, prev, partial)
    api.command("docker-ps", c_ps, usage="docker-ps", help="Open the docker containers window")
    api.command("docker-images", c_images, usage="docker-images", help="Open the docker images window")
    api.command("docker-stats", c_stats, usage="docker-stats", help="Open the docker stats window")
    api.command("docker-logs", c_logs, usage="docker-logs <container> [tail]", help="Follow container logs in a window", completer=names_completer)
    api.command("docker-exec", c_exec, usage="docker-exec <container> [cmd...]", help="Shell (or command) inside a container", completer=names_completer)
    api.command("docker-run", c_run, usage="docker-run <image> [args...]", help="Run an image interactively")
    api.command("docker-inspect", c_inspect, usage="docker-inspect <name>", help="Inspect JSON in a viewer window", completer=names_completer)
    api.command("docker-do", c_do, usage="docker-do <action> <container>", help="start/stop/restart/rm/pause/unpause/kill", completer=ac)
