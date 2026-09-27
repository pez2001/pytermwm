"""The window manager core: desktops, windows, focus, layout, input routing."""
from __future__ import annotations

import logging
import re
import os
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

from .commands import CommandError, CommandRegistry, split_line
from .dialogs import DialogManager
from .keys import Event, Keymap, key_to_bytes
from .layout import (Rect, TileTree, carve_docks, cascade_rect, clamp_rect, focus_neighbor, layout_centered,
                     layout_grid, layout_master, layout_spiral, layout_stack, layout_table, AUTO_MODES)
from .logs import ring_of, setup_logging
from .prompt import Palette, Prompt
from .selectionwm import SelectionMixin
from .statusline import StatusLine
from .theme import Theme, get_theme
from .window import (FileSource, InternalWindow, PipeSource, ProcessWindow, PtySource, TextWindow, Window,
                     OPTION_SPECS, default_shell, parse_command)

LAYOUT_CYCLE = ["tile", "master", "spiral", "columns", "rows", "grid", "centered", "monocle", "float"]

DEFAULT_CONFIG = {
    "theme": "default",
    "layout": "tile",
    "desktops": ["main"],
    "shell": None,
    "history": 2000,
    "gap": 0,
    "master_ratio": 0.55,
    "master_count": 1,
    "new_window_position": "end",
    "on_last_close": "exit",       # exit | respawn | keep
    "prompt_takeover": 2.0,        # seconds the status line shows command feedback
    "mouse": True,
    "window_defaults": {},
    "confirm_quit": False,
}

_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")    # OSC strings (title, cwd, ...): their BEL is a terminator


class Desktop:
    def __init__(self, name: str, layout: str = "tile", params: Optional[dict] = None):
        self.name = name
        self.windows: List[int] = []
        self.focus: Optional[int] = None
        self.focus_stack: List[int] = []
        self.zoom: Optional[int] = None
        self.layout = layout
        self.params: dict = dict(params or {})
        self.tree = TileTree()
        self.rects: Dict[int, Rect] = {}
        self.order: List[int] = []

    def describe(self) -> dict:
        return {"name": self.name, "layout": self.layout, "windows": list(self.windows), "focus": self.focus,
                "zoom": self.zoom, "params": dict(self.params)}


class WindowManager(SelectionMixin):
    def __init__(self, cols: int = 80, rows: int = 24, config: Optional[dict] = None,
                 session_name: str = "default", logger: Optional[logging.Logger] = None):
        self.lock = threading.RLock()
        self.cols, self.rows = cols, rows
        self.session_name = session_name
        self.cfg: dict = dict(DEFAULT_CONFIG)
        self.log = logger or setup_logging("pytermwm.%s" % session_name if session_name else "pytermwm")
        self.windows: Dict[int, Window] = {}
        self._next_id = 1
        self.desktops: List[Desktop] = []
        self.pending_terminal_output: List[str] = []      # escape sequences for attached terminals (e.g. OSC 777)
        self.chart_glyphs_hint = "unicode"                 # from the attaching terminal's own environment (see terminal.detect_glyphs)
        self._init_selection()
        self.cur = 0
        self.dirty = True
        self.theme: Theme = get_theme("default")
        self.keymap = Keymap()
        self.commands = CommandRegistry()
        self.statusline = StatusLine(self)
        self.prompt = Prompt(self)
        self.palette = Palette(self)
        self.dialogs = DialogManager(self)
        self.status_items: Dict[str, dict] = {}
        self.progress: Dict[str, dict] = {}
        self.messages: List[Tuple[str, str, float]] = []
        self.subscribers: Dict[str, List[Callable]] = {}
        self.window_kinds: Dict[str, Callable] = {}
        self.hist: Dict[str, List[float]] = {}
        self.hist_time: Dict[str, float] = {}
        self.quit_requested = False
        self.detach_requested = False
        self.redraw_requested = False    # repaint every client from scratch on the next frame
        self.screen_recorder = None      # screenshot.ScreenRecorder while `record-screen` runs
        self.start_time = time.time()
        self.sock_path: Optional[str] = None
        self.background = None           # effect object with render(cols, rows, t) -> cells
        self._bg_frame_at = 0.0          # when tick() last asked for a new background frame
        self.plugins = None
        self.wake = lambda: None        # set by the server: makes the main loop run now (thread safe)
        self.rules = None
        self.config_path: Optional[str] = None
        self.drag: Optional[dict] = None
        self.last_focus_id: Optional[int] = None
        self._route_files: Dict[str, object] = {}
        self._routing: set = set()
        self.trace_events = False
        self.trace: List[dict] = []
        self.clients: List = []
        self.extra: Dict[str, object] = {}    # free slots for plugins
        self.event_ring: List[dict] = []
        self.event_seq = 0
        self.detach_all = False
        self._closing = False
        from . import builtin_windows
        builtin_windows.register_all(self)
        from . import recording
        recording.register(self)
        self.add_desktop("main")
        if config:
            self.configure(config)

    # ------------------------------------------------------------------ properties
    @property
    def desk(self) -> Desktop:
        return self.desktops[self.cur]

    @property
    def focused(self) -> Optional[Window]:
        f = self.desk.focus
        return self.windows.get(f) if f is not None else None

    def opt(self, name, default=None):
        v = self.cfg.get(name, default)
        return default if v is None else v

    # ------------------------------------------------------------------ events
    def on(self, event: str, fn: Callable):
        self.subscribers.setdefault(event, []).append(fn)
        return fn

    def off(self, event: str, fn: Callable):
        try:
            self.subscribers.get(event, []).remove(fn)
        except ValueError:
            pass

    RING_EVENTS = {"window_created", "window_closed", "window_exit", "window_focus", "desktop_switch", "status",
                   "dialog_result", "layout_set", "theme_changed", "config_applied", "bell", "progress", "window_title",
                   "window_moved", "dirwatch", "notify", "clipboard"}

    def emit(self, event: str, **kw):
        if event in self.RING_EVENTS:
            self.event_seq += 1
            data = {}
            for k, v in kw.items():
                if isinstance(v, Window):
                    data["window"] = v.id
                    data["title"] = v.title
                elif isinstance(v, (str, int, float, bool)) or v is None:
                    data[k] = v
                elif hasattr(v, "name") and isinstance(getattr(v, "name"), str):
                    data[k] = v.name
                elif event == "dialog_result":
                    data[k] = repr(v)
            self.event_ring.append({"seq": self.event_seq, "time": time.time(), "event": event, "data": data})
            del self.event_ring[:-500]
        if self.trace_events:
            self.trace.append({"time": time.time(), "event": event,
                               "data": {k: (getattr(v, "id", None) if isinstance(v, Window) else repr(v)[:80]) for k, v in kw.items()}})
            del self.trace[:-500]
        for fn in list(self.subscribers.get(event, ())) + list(self.subscribers.get("*", ())):
            try:
                if fn in self.subscribers.get("*", ()):
                    fn(event, **kw)
                else:
                    fn(**kw)
            except Exception:
                self.log.error("event handler for %s failed:\n%s", event, traceback.format_exc())

    # ------------------------------------------------------------------ messages / status
    def message(self, text: str, style: str = "normal", ttl: float = 3.0):
        self.messages.append((text, style, time.time() + ttl))
        del self.messages[:-5]
        self.dirty = True

    def current_message(self) -> Optional[Tuple[str, str]]:
        now = time.time()
        self.messages = [m for m in self.messages if m[2] > now]
        if self.messages:
            m = self.messages[-1]
            return m[0], m[1]
        return None

    def set_status(self, key: str, value, label: Optional[str] = None, style: str = "normal",
                   ttl: Optional[float] = None):
        self.status_items[key] = {"value": value, "label": label, "style": style,
                                  "time": time.time(), "expires": (time.time() + ttl) if ttl else None}
        self.dirty = True
        self.emit("status", key=key, value=value)

    def clear_status(self, key: Optional[str] = None):
        if key is None:
            self.status_items.clear()
        else:
            self.status_items.pop(key, None)
        self.dirty = True

    def history_for(self, key: str, value: float, n: int = 20, period: float = 1.0) -> List[float]:
        h = self.hist.setdefault(key, [])
        now = time.time()
        if now - self.hist_time.get(key, 0) >= period or not h:
            h.append(value)
            del h[:-max(n, 60)]
            self.hist_time[key] = now
        return h[-n:]

    def update_progress(self, name: str, current: float, total: float = 0, label: str = "", rate: float = 0.0,
                        done: bool = False):
        t = self.progress.setdefault(name, {})
        t.update({"current": current, "total": total, "label": label or t.get("label", name), "rate": rate,
                  "done": done, "time": time.time()})
        if done:
            t["done_time"] = time.time()
        self.dirty = True
        self.emit("progress", name=name, task=t)

    # ------------------------------------------------------------------ config bits
    def configure(self, cfg: dict):
        from .config import apply_config
        apply_config(self, cfg)

    def set_theme(self, t):
        """Switch theme by name or from an inline dict."""
        if isinstance(t, dict):
            theme = Theme.from_dict(t)
        else:
            theme = get_theme(str(t))
        self.theme = theme
        for w in self.windows.values():
            w._rc = None
        self.dirty = True
        self.emit("theme_changed", theme=theme)

    def ensure_plugins(self):
        if self.plugins is None:
            from .plugins import PluginManager
            self.plugins = PluginManager(self)
        return self.plugins

    def ensure_rules(self):
        if self.rules is None:
            from .scripting import RuleEngine
            self.rules = RuleEngine(self)
        return self.rules

    def load_plugin_by_name(self, name: str):
        self.ensure_plugins()
        if name not in self.plugins.plugins:
            self.plugins.load(name, {})

    # ------------------------------------------------------------------ desktops
    def add_desktop(self, name: Optional[str] = None, layout: Optional[str] = None) -> Desktop:
        name = name or str(len(self.desktops) + 1)
        params = {"master_ratio": self.opt("master_ratio", 0.55), "master_count": self.opt("master_count", 1),
                  "gap": self.opt("gap", 0)}
        d = Desktop(name, layout or self.opt("layout", "tile"), params)
        self.desktops.append(d)
        self.dirty = True
        self.emit("desktop_created", desktop=d)
        return d

    def switch_desktop(self, target) -> Desktop:
        if isinstance(target, Desktop):
            idx = self.desktops.index(target)
        else:
            idx = self.resolve_desktop(target)
        if idx == self.cur:
            return self.desk
        self.cur = idx
        self.last_desktop = getattr(self, "_prev_desktop", 0)
        d = self.desk
        for wid in d.windows:
            w = self.windows[wid]
            if wid == d.focus:
                w.activity = False
        self.relayout()
        self.emit("desktop_switch", desktop=d, index=idx)
        self.dirty = True
        return d

    def resolve_desktop(self, ref) -> int:
        n = len(self.desktops)
        if isinstance(ref, int):
            idx = ref - 1
        else:
            s = str(ref)
            if s == "next":
                return (self.cur + 1) % n
            if s == "prev":
                return (self.cur - 1) % n
            if s == "last":
                return getattr(self, "_prev_desktop", self.cur) % n
            if s.isdigit():
                idx = int(s) - 1
            else:
                for i, d in enumerate(self.desktops):
                    if d.name == s:
                        return i
                raise CommandError("no such desktop: %s" % s)
        if not (0 <= idx < n):
            raise CommandError("no such desktop: %s (have %d)" % (ref, n))
        return idx

    def close_desktop(self, idx: Optional[int] = None):
        idx = self.cur if idx is None else idx
        if len(self.desktops) <= 1:
            raise CommandError("cannot close the last desktop")
        d = self.desktops[idx]
        for wid in list(d.windows):
            self.close_window(wid, _no_respawn=True)
        self.desktops.pop(idx)
        self.cur = min(self.cur if self.cur < idx else max(0, self.cur - (1 if self.cur >= idx else 0)), len(self.desktops) - 1)
        self.relayout()
        self.emit("desktop_closed", desktop=d)

    # ------------------------------------------------------------------ window lookup
    def resolve_window(self, ref=None) -> Window:
        if ref is None or ref in ("", "current", "@"):
            w = self.focused
            if not w:
                raise CommandError("no focused window")
            return w
        if isinstance(ref, Window):
            return ref
        if isinstance(ref, int) or (isinstance(ref, str) and ref.lstrip("#").isdigit()):
            wid = int(str(ref).lstrip("#"))
            w = self.windows.get(wid)
            if w:
                return w
            raise CommandError("no such window: %s" % ref)
        s = str(ref)
        for w in self.windows.values():
            if w.name == s:
                return w
        cands = [w for w in self.windows.values() if s.lower() in w.title.lower()]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise CommandError("ambiguous window: %s (%s)" % (s, ", ".join(str(w.id) for w in cands)))
        raise CommandError("no such window: %s" % s)

    # ------------------------------------------------------------------ area / layout
    def content_area(self) -> Rect:
        h = self.statusline.height
        pos = self.statusline.position
        if pos == "top":
            return Rect(0, h, self.cols, max(1, self.rows - h))
        return Rect(0, 0, self.cols, max(1, self.rows - h))

    def status_row(self) -> Optional[int]:
        pos = self.statusline.position
        if pos == "off":
            return None
        return 0 if pos == "top" else self.rows - 1

    def resize(self, cols: int, rows: int):
        cols, rows = max(10, cols), max(4, rows)
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self.relayout()
        self.emit("resize", cols=cols, rows=rows)

    def _tiled_ids(self, desk: Desktop) -> List[int]:
        out = []
        for wid in desk.windows:
            w = self.windows[wid]
            if not w.hidden and not w.floating and not w.dock:
                out.append(wid)
        return out

    def compute_layout(self, desk: Optional[Desktop] = None):
        desk = desk or self.desk
        area = self.content_area()
        vis = [wid for wid in desk.windows if not self.windows[wid].hidden]
        rects: Dict[int, Rect] = {}
        order: List[int] = []
        if desk.zoom is not None and desk.zoom in vis:
            desk.rects = {desk.zoom: area}
            desk.order = [desk.zoom]
            return
        p = desk.params
        gap = int(p.get("gap", 0))
        docked = [(wid,) + tuple(self.windows[wid].dock) for wid in vis if self.windows[wid].dock]
        drects, rem = carve_docks(docked, area)
        tiled = [w for w in vis if not self.windows[w].floating and not self.windows[w].dock]
        floats = [w for w in vis if self.windows[w].floating and not self.windows[w].dock]
        eng = desk.layout
        if eng == "float":
            floats = tiled + floats
            tiled = []
        trects: Dict[int, Rect] = {}
        if tiled:
            if eng == "tile":
                desk.tree.sync(tiled)
                trects = desk.tree.layout(rem, gap)
            elif eng == "master":
                trects = layout_master(tiled, rem, int(p.get("master_count", 1)), float(p.get("master_ratio", 0.55)), gap)
            elif eng == "spiral":
                trects = layout_spiral(tiled, rem, float(p.get("master_ratio", 0.5)), gap)
            elif eng == "columns":
                trects = layout_stack(tiled, rem, "h", gap)
            elif eng == "rows":
                trects = layout_stack(tiled, rem, "v", gap)
            elif eng == "centered":
                trects = layout_centered(tiled, rem, float(p.get("master_ratio", 0.5)), gap)
            elif eng == "monocle":
                f = desk.focus if desk.focus in tiled else tiled[0]
                trects = {f: rem}
                tiled = [f]
            elif eng == "grid":
                trects = layout_grid(tiled, rem, p.get("grid_cols"), gap)
            elif eng == "table":
                names = {wid: (self.windows[wid].name or str(wid)) for wid in tiled}
                trects = layout_table(tiled, rem, p.get("cols"), p.get("rows"), p.get("cells"), gap, names)
            else:
                trects = layout_grid(tiled, rem, None, gap)
        frects = {}
        for i, wid in enumerate(floats):
            w = self.windows[wid]
            if w.frect is None:
                r = cascade_rect(i, area)
                w.frect = tuple(r)
            r = clamp_rect(Rect(*w.frect), area)
            w.frect = tuple(r)
            frects[wid] = r
        rects.update(drects)
        rects.update(trects)
        rects.update(frects)
        order = list(drects) + [w for w in tiled if w in trects] + floats
        desk.rects = rects
        desk.order = order

    def relayout(self):
        """Recompute geometry of the current desktop and resize its windows."""
        if self._closing:
            return
        self.compute_layout()
        d = self.desk
        for wid, r in d.rects.items():
            w = self.windows[wid]
            if w.opts["border"]:
                cols, rows = max(1, r.w - 2), max(1, r.h - 2)
            else:
                cols, rows = max(1, r.w), max(1, r.h)
            if w.viewport != (cols, rows) or (w.vsize is None and (w.screen.cols, w.screen.rows) != (cols, rows)):
                w.set_viewport(cols, rows)
        self.dirty = True
        self.emit("layout_changed", desktop=d)

    def inner_rect(self, w: Window, r: Rect) -> Rect:
        return r.shrink(1, 1, 1, 1) if w.opts["border"] else r

    # ------------------------------------------------------------------ window creation
    def register_window_kind(self, name: str, factory: Callable):
        """factory(wm, wid, spec, rows, cols) -> Window"""
        self.window_kinds[name] = factory

    def create_window(self, spec: Optional[dict] = None, **kw) -> Window:
        spec = dict(spec or {})
        spec.update(kw)
        kind = spec.get("kind")
        if not kind:
            if spec.get("path"):
                kind = "file"
            elif spec.get("pipe") or spec.get("mode") == "pipe":
                kind = "pipe"
            else:
                kind = "term"
        # target desktop
        desk = self.desk
        if spec.get("desktop") not in (None, ""):
            desk = self.desktops[self.resolve_desktop(spec["desktop"])]
        wid = self._next_id
        self._next_id += 1
        defaults = dict(self.cfg.get("window_defaults") or {})
        opts = {k: v for k, v in defaults.items() if k in OPTION_SPECS}
        opts.update({k: v for k, v in (spec.get("opts") or {}).items() if k in OPTION_SPECS})
        opts.update({k: v for k, v in spec.items() if k in OPTION_SPECS})
        opts.setdefault("history", self.opt("history", 2000))
        if "border" in spec:
            opts["border"] = spec["border"]
        area = self.content_area()
        rows0, cols0 = max(2, area.h - 2), max(2, area.w // 2)
        title = spec.get("title") or ""
        try:
            if kind in self.window_kinds:
                w = self.window_kinds[kind](self, wid, spec, rows0, cols0, opts)
            elif kind in ("term", "pipe", "file"):
                w = ProcessWindow(wid, title, rows0, cols0, name=spec.get("name"), **opts)
                w.kind = kind
            else:
                raise CommandError("unknown window kind: %s" % kind)
        except CommandError:
            self._next_id -= 1 if wid == self._next_id - 1 else 0
            raise
        w.wm = self
        w.spec = {k: v for k, v in spec.items() if k not in ("focus",)}
        if spec.get("name"):
            w.name = str(spec["name"])
        if title:
            w.title_text = title
        w.apply_overflow()
        if spec.get("vsize"):
            v = spec["vsize"]
            if isinstance(v, str):
                a, _, b = v.lower().partition("x")
                v = (int(a), int(b))
            w.vsize = (int(v[0]), int(v[1]))
            w.screen.resize(w.vsize[1], w.vsize[0])
        if spec.get("floating"):
            w.floating = True
        if spec.get("rect"):
            w.frect = tuple(int(x) for x in spec["rect"])
            w.floating = True
        if spec.get("dock"):
            d = spec["dock"]
            if isinstance(d, str):
                edge, _, size = d.partition(":")
                d = (edge, float(size) if size else 30)
            w.dock = (str(d[0]), float(d[1]))
        self.windows[wid] = w
        w.desktop = desk
        # position in list
        if self.opt("new_window_position", "end") == "master" and not w.floating and desk.windows:
            desk.windows.insert(0, wid)
        else:
            desk.windows.append(wid)
        # tile tree insertion
        if kind != "internal" or True:
            self._tree_insert(desk, w, spec.get("split"))
        if desk is self.desk:
            self.relayout()
        else:
            self.compute_layout(desk)
        try:
            self._start_window(w, spec)
        except Exception as e:
            self.log.error("cannot start window: %s", e)
            self._remove_window(w)
            raise CommandError("cannot start: %s" % e)
        if spec.get("focus", True) or desk.focus is None:
            if desk is self.desk or spec.get("focus", True):
                self.focus_window(wid)
        self.dirty = True
        self.log.info("window %d created (%s) %s", wid, kind, w.title)
        self.emit("window_created", window=w)
        return w

    def _tree_insert(self, desk: Desktop, w: Window, split: Optional[str]):
        if w.floating or w.dock:
            return
        target = desk.focus if desk.focus in desk.tree else None
        if target is None:
            leaves = desk.tree.leaves()
            target = leaves[-1] if leaves else None
        direction = split
        if direction not in ("h", "v"):
            r = desk.rects.get(target)
            direction = "h" if (r and r.w >= r.h * 2.2) else "v"
            if not r:
                direction = "h"
        desk.tree.insert(w.id, target, direction)

    def _start_window(self, w: Window, spec: dict):
        kind = w.kind
        if isinstance(w, InternalWindow) or kind in self.window_kinds:
            return
        env = dict(spec.get("env") or {})
        env["PYTERMWM_SESSION"] = self.session_name
        env["PYTERMWM_WINDOW"] = str(w.id)
        if self.sock_path:
            env["PYTERMWM_SOCK"] = self.sock_path
        cwd = spec.get("cwd")
        if cwd:
            cwd = os.path.expanduser(cwd)
        elif self.cfg.get("cwd"):
            cwd = os.path.expanduser(self.cfg["cwd"])
        if kind == "file":
            w.source = FileSource(spec["path"], follow=spec.get("follow", True))
            w.source.tty = False
        elif kind == "pipe":
            argv, _ = parse_command(spec.get("cmd"))
            w.source = PipeSource(argv, cwd, env)
        else:
            cmd = spec.get("cmd")
            if cmd in (None, ""):
                shell = self.cfg.get("shell") or default_shell()
                argv = [shell]
                w.opts.setdefault("on_exit", "close")
            else:
                argv, _ = parse_command(cmd)
                if "on_exit" not in spec and "on_exit" not in (spec.get("opts") or {}):
                    w.opts["on_exit"] = spec.get("on_exit", "keep" if spec.get("keep", True) else "close")
            w.source = PtySource(argv, cwd, env, w.screen.rows, w.screen.cols)
            w.default_title = os.path.basename(argv[0]) if cmd in (None, "") else ""
        w.exited = False

    def _remove_window(self, w: Window):
        d = w.desktop
        if d is not None:
            if w.id in d.windows:
                d.windows.remove(w.id)
            d.tree.remove(w.id)
            if w.id in d.focus_stack:
                d.focus_stack = [x for x in d.focus_stack if x != w.id]
            if d.focus == w.id:
                d.focus = None
            if d.zoom == w.id:
                d.zoom = None
        self.windows.pop(w.id, None)

    # ------------------------------------------------------------------ closing
    def close_window(self, wid, _no_respawn: bool = False):
        w = self.resolve_window(wid)
        d = w.desktop
        was_focus = d is not None and d.focus == w.id
        self.clear_routes_of(w.id)
        try:
            w.close()
        except Exception:
            self.log.error("closing window %s: %s", w.id, traceback.format_exc())
        self._remove_window(w)
        if d is not None and was_focus and d.windows:
            nxt = None
            for cand in reversed(d.focus_stack):
                if cand in self.windows:
                    nxt = cand
                    break
            if nxt is None:
                nxt = d.windows[-1]
            d.focus = None
            if d is self.desk:
                self.focus_window(nxt)
            else:
                d.focus = nxt
        if d is self.desk:
            self.relayout()
        self.dirty = True
        self.log.info("window %d closed", w.id)
        self.emit("window_closed", window=w)
        if not self.windows and not _no_respawn and not self._closing:
            policy = self.opt("on_last_close", "exit")
            if policy == "exit":
                self.quit_requested = True
            elif policy == "respawn":
                self.create_window({})

    # ------------------------------------------------------------------ focus
    def focus_window(self, ref):
        w = self.resolve_window(ref)
        d = w.desktop
        if d is not self.desk:
            self.switch_desktop(d)
        prev = d.focus
        if prev == w.id:
            self._raise(w)
            return w
        if prev in self.windows:
            self.windows[prev].on_focus(False)
            self.last_focus_id = prev
        d.focus = w.id
        d.focus_stack = [x for x in d.focus_stack if x != w.id] + [w.id]
        w.activity = False
        w.bell = False
        w.on_focus(True)
        self._raise(w)
        if d.zoom is not None and d.zoom != w.id:
            d.zoom = w.id
        self.relayout() if (d.zoom is not None or d.layout == "monocle") else None
        self.dirty = True
        self.emit("window_focus", window=w)
        return w

    def _raise(self, w: Window):
        d = w.desktop
        if (w.floating or d.layout == "float") and d and d.windows and d.windows[-1] != w.id:
            d.windows.remove(w.id)
            d.windows.append(w.id)
            self.compute_layout(d)

    def focus_dir(self, direction: str):
        d = self.desk
        if not d.windows:
            return
        if direction in ("next", "prev"):
            ids = [w for w in d.windows if not self.windows[w].hidden]
            if d.layout == "monocle" or d.zoom is not None or True:
                pass
            if not ids:
                return
            if d.focus in ids:
                i = ids.index(d.focus)
                ids_order = ids
                i = (i + (1 if direction == "next" else -1)) % len(ids)
                self.focus_window(ids[i])
            else:
                self.focus_window(ids[0])
            return
        if direction == "last":
            if self.last_focus_id in self.windows and self.windows[self.last_focus_id].desktop is d:
                self.focus_window(self.last_focus_id)
            return
        if d.focus is None:
            self.focus_window(d.windows[0])
            return
        n = focus_neighbor(d.rects, d.focus, direction)
        if n is not None:
            self.focus_window(n)

    def swap_dir(self, direction: str):
        d = self.desk
        if d.focus is None:
            return
        n = focus_neighbor(d.rects, d.focus, direction)
        if n is None:
            return
        a, b = d.focus, n
        self.swap_windows(a, b)

    def swap_windows(self, a: int, b: int):
        d = self.desk
        ia, ib = d.windows.index(a), d.windows.index(b)
        d.windows[ia], d.windows[ib] = d.windows[ib], d.windows[ia]
        if a in d.tree and b in d.tree:
            d.tree.swap(a, b)
        self.relayout()

    # ------------------------------------------------------------------ desktop moves
    def send_to_desktop(self, ref, target):
        w = self.resolve_window(ref)
        try:
            idx = self.resolve_desktop(target)
        except CommandError:
            if isinstance(target, int) or str(target).isdigit():
                # create desktops on demand up to that index
                n = int(target)
                while len(self.desktops) < n:
                    self.add_desktop()
                idx = n - 1
            else:
                self.add_desktop(str(target))
                idx = len(self.desktops) - 1
        dst = self.desktops[idx]
        src = w.desktop
        if dst is src:
            return
        was_cur = src is self.desk
        if src.focus == w.id:
            src.focus = None
        src.windows.remove(w.id)
        src.tree.remove(w.id)
        if src.zoom == w.id:
            src.zoom = None
        src.focus_stack = [x for x in src.focus_stack if x != w.id]
        if src.focus is None and src.windows:
            src.focus = src.windows[-1]
        dst.windows.append(w.id)
        w.desktop = dst
        self._tree_insert(dst, w, None)
        dst.focus = dst.focus or w.id
        self.compute_layout(dst)
        if was_cur:
            self.relayout()
        self.dirty = True
        self.emit("window_moved", window=w, desktop=dst)

    # ------------------------------------------------------------------ layouts
    def set_layout(self, name: str, desk: Optional[Desktop] = None):
        from .layout import ENGINE_NAMES
        if name not in ENGINE_NAMES:
            raise CommandError("unknown layout %r (choose from %s)" % (name, ", ".join(ENGINE_NAMES)))
        d = desk or self.desk
        d.layout = name
        if name != "float":
            pass
        if desk is None or desk is self.desk:
            self.relayout()
        self.emit("layout_set", desktop=d, layout=name)

    def cycle_layout(self, step: int = 1):
        d = self.desk
        try:
            i = LAYOUT_CYCLE.index(d.layout)
        except ValueError:
            i = -1
        self.set_layout(LAYOUT_CYCLE[(i + step) % len(LAYOUT_CYCLE)])

    def resize_focused(self, direction: str, amount: int = 2):
        d = self.desk
        w = self.focused
        if not w:
            return
        area = self.content_area()
        if w.floating or d.layout == "float":
            r = list(w.frect or tuple(d.rects.get(w.id, area)))
            if direction == "left":
                r[0] -= amount
                r[2] += amount
            elif direction == "right":
                r[2] += amount
            elif direction == "up":
                r[1] -= amount
                r[3] += amount
            else:
                r[3] += amount
            w.frect = tuple(r)
            w.floating = True
        elif d.layout == "tile":
            dim = area.w if direction in ("left", "right") else area.h
            d.tree.resize(w.id, direction, amount / max(1, dim))
        elif d.layout in ("master", "centered", "spiral"):
            d.params["master_ratio"] = max(0.1, min(0.9, float(d.params.get("master_ratio", 0.55)) +
                                                       (amount / max(1, area.w)) * (1 if direction in ("right", "down") else -1)))
        self.relayout()

    # ------------------------------------------------------------------ input handling
    def process_events(self, events: List[Event], client=None):
        for ev in events:
            try:
                self.process_event(ev, client)
            except CommandError as e:
                self.message(str(e), "err")
            except Exception:
                self.log.error("input handling failed:\n%s", traceback.format_exc())

    def process_event(self, ev: Event, client=None):
        if ev.type == "key":
            self.handle_key(ev.name, ev.raw)
        elif ev.type == "paste":
            self.handle_paste(ev.data)
        elif ev.type == "mouse":
            if self.cfg.get("mouse", True):
                self.handle_mouse(ev.data)
        elif ev.type == "focus":
            w = self.focused
            if w and w.screen.focus_events and w.source:
                w.source.write(b"\x1b[I" if ev.data else b"\x1b[O")

    def handle_paste(self, text: str):
        m = self.dialogs.modal() or self.dialogs.focused()
        if m:
            m.handle_paste(text)
        elif self.prompt.active:
            self.prompt.handle_paste(text)
        elif self.palette.active:
            self.palette.editor.insert(text.replace("\n", " "))
            self.palette._filter()
            self.dirty = True
        else:
            w = self.focused
            if w and w.source:
                if w.screen.bracketed_paste:
                    w.write_input(b"\x1b[200~" + text.encode() + b"\x1b[201~")
                else:
                    w.write_input(text.encode())

    def handle_key(self, name: str, raw: bytes = b""):
        self.emit("key", key=name)
        if self.copy_view is not None:
            self.copy_view_key(name)
            return
        m = self.dialogs.modal()
        if m:
            m.handle_key(name)
            self.dirty = True
            return
        if self.prompt.active:
            self.prompt.handle_key(name)
            self.dirty = True
            return
        if self.palette.active:
            self.palette.handle_key(name)
            self.dirty = True
            return
        fd = self.dialogs.focused()
        if fd:
            if name == "M-d":
                self.dialogs.toggle_focus()
            else:
                fd.handle_key(name)
            self.dirty = True
            return
        act, consumed = self.keymap.lookup(name)
        if act == "__prefix__":
            self.dirty = True
            return
        if act:
            if isinstance(act, list):
                for a in act:
                    self.run_command_line(a, source="key")
            else:
                self.run_command_line(act, source="key")
            self.dirty = True
            return
        if consumed:
            self.dirty = True
            return
        w = self.focused
        if not w:
            return
        if w.handle_key(name, raw):
            self.dirty = True
            return
        if w.scroll_y and not isinstance(w, InternalWindow):
            w.scroll_y = 0
            self.dirty = True
        if self.selection is not None and self.selection.wid == w.id and not self.selection.copy_mode:
            self.clear_selection()                       # typing into the window drops its selection (as PuTTY does)
        if not raw:
            raw = key_to_bytes(name, w.screen.app_cursor) or b""
        elif w.screen.app_cursor and raw.startswith(b"\x1b[") and len(raw) == 3 and raw[2:3] in b"ABCDHF":
            raw = b"\x1bO" + raw[2:3]
        w.write_input(raw)

    def handle_mouse(self, m: dict):
        x, y, kind = m["x"], m["y"], m["kind"]
        # dialogs first
        if self.dialogs.modal():
            return
        # ongoing drag (also over the status line, so a release there cannot get lost)
        if self.drag and kind in ("move", "release"):
            self._do_drag(x, y, kind == "release")
            return
        if self.sel_drag and kind in ("move", "release"):
            self.selection_drag(m)
            return
        # status line
        row = self.status_row()
        if row is not None and y == row:
            if kind == "press" and m["button"] == 1:
                for x0, x1, cmd in self.statusline.hits:
                    if x0 <= x < x1:
                        self.run_command_line(cmd, source="mouse")
                        break
            return
        d = self.desk
        hit = None
        for wid in reversed(d.order):
            r = d.rects.get(wid)
            if r and r.contains(x, y):
                hit = wid
                break
        if hit is None:
            return
        w = self.windows[hit]
        r = d.rects[hit]
        inner = self.inner_rect(w, r)
        in_inner = inner.contains(x, y)
        if kind == "press" and self.selection_press(m, w, inner, in_inner):
            return
        if kind == "press" and m["button"] == 1:
            if d.focus != hit:
                self.focus_window(hit)
            if not in_inner and w.floating and w.opts["border"]:
                if y == r.y:
                    self.drag = {"wid": hit, "mode": "move", "ox": x - r.x, "oy": y - r.y}
                elif x == r.x2 - 1 and y == r.y2 - 1:
                    self.drag = {"wid": hit, "mode": "resize"}
                return
            if not in_inner and y == r.y and w.opts["border"] and self.theme.o("titlebar"):
                # gadgets in the amiga title bar
                left, right = self.theme.o("gadgets")
                if left and x == r.x + 1:
                    self.close_window(hit)
                    return
                if right and x == r.x2 - 2:
                    self.run_command_line("zoom", source="mouse")
                    return
            if in_inner:
                self._forward_mouse(w, inner, m)
            return
        if kind == "release":
            if in_inner and d.focus == hit:
                self._forward_mouse(w, inner, m)
            return
        if kind in ("wheelup", "wheeldown"):
            if in_inner and w.screen.mouse_mode and w.source:
                self._forward_mouse(w, inner, m)
            else:
                w.scroll(3 if kind == "wheelup" else -3)
                self.dirty = True
            return
        if in_inner and d.focus == hit:
            self._forward_mouse(w, inner, m)

    def _do_drag(self, x, y, release):
        dr = self.drag
        w = self.windows.get(dr["wid"])
        if not w:
            self.drag = None
            return
        area = self.content_area()
        r = self.desk.rects.get(w.id)
        if dr["mode"] == "move":
            w.frect = (x - dr["ox"], y - dr["oy"], r.w, r.h)
        else:
            w.frect = (r.x, r.y, max(8, x - r.x + 1), max(3, y - r.y + 1))
        self.relayout()
        if release:
            self.drag = None
            w.drag_overlay_title = None
        else:
            # temporary feedback in the title bar while the drag is in progress: the resulting
            # top-left position while moving (post-clamp to the screen), the resulting content
            # size while resizing -- both read back after relayout so they reflect what actually
            # happened, not the raw unclamped mouse position
            nr = self.desk.rects.get(w.id)
            if dr["mode"] == "move" and nr:
                w.drag_overlay_title = "%d, %d" % (nr.x, nr.y)
            elif dr["mode"] == "resize":
                cols, rows = w.viewport
                w.drag_overlay_title = "%d x %d" % (cols, rows)

    def _forward_mouse(self, w: Window, inner: Rect, m: dict):
        scr = w.screen
        if not scr.mouse_mode or not w.source:
            return
        cx, cy = m["x"] - inner.x + 1, m["y"] - inner.y + 1
        bt = m.get("bt", 0)
        if scr.mouse_sgr:
            w.source.write(("\x1b[<%d;%d;%d%s" % (bt, cx, cy, m.get("final", "M"))).encode())
        else:
            if m["kind"] == "release":
                bt = 3
            w.source.write(b"\x1b[M" + bytes([32 + bt, min(255, 32 + cx), min(255, 32 + cy)]))

    # ------------------------------------------------------------------ prompt & palette
    def run_prompt_line(self, text: str, mode: str = "auto"):
        text = text.strip()
        if not text:
            return
        if mode == "search":
            self.run_command_line("search " + text, source="prompt")
            return
        if mode == "command" or text.startswith(":"):
            self.run_command_line(text.lstrip(":"), source="prompt")
            return
        # shell command: gets a new window; the status line takes over showing what happened
        try:
            w = self.create_window({"cmd": text, "title": text, "keep": True})
            self.message("$ %s → window %d" % (text, w.id), "ok", ttl=float(self.opt("prompt_takeover", 2.0)))
        except CommandError as e:
            self.message(str(e), "err")

    def palette_items(self, scope: str = "all") -> List[Tuple[str, str, str]]:
        items: List[Tuple[str, str, str]] = []
        if scope in ("all", "windows"):
            for d in self.desktops:
                for wid in d.windows:
                    w = self.windows[wid]
                    items.append(("window %d: %s" % (wid, w.title), "desktop %s" % d.name, "focus %d" % wid))
        if scope in ("all", "desktops"):
            for i, d in enumerate(self.desktops):
                items.append(("desktop %d: %s" % (i + 1, d.name), "%d windows" % len(d.windows), "desktop %d" % (i + 1)))
        if scope in ("all", "themes"):
            from .theme import all_theme_names
            for t in all_theme_names():
                items.append(("theme %s" % t, "switch theme", "theme %s" % t))
        if scope in ("all", "commands"):
            for name in sorted(self.commands.commands):
                c = self.commands.commands[name]
                if not c.hidden:
                    items.append((c.usage if scope == "commands" else c.name, c.help, c.name))
        if scope in ("all", "sessions"):
            for s in self.extra.get("sessions_provider", lambda: [])():
                items.append(("session %s" % s, "attach", "attach %s" % s))
        return items

    # ------------------------------------------------------------------ command execution
    def chart_glyphs(self) -> str:
        """Which glyph set the CPU sparkline and other bars/charts should draw with: an explicit ``charts.glyphs`` in
        the config wins, otherwise the hint the attaching terminal sent (see ``chart_glyphs_hint``)."""
        from .charts import GLYPH_SETS
        g = (self.cfg.get("charts") or {}).get("glyphs")
        return g if g in GLYPH_SETS else self.chart_glyphs_hint

    def run_command_line(self, line: str, source: str = "api", raise_errors: bool = False):
        """Run a command line (possibly several ';' separated commands). Returns last result."""
        result = None
        prev_source = getattr(self, "_source", "api")
        self._source = source
        try:
            for argv in split_line(line):
                self.emit("command", argv=argv, source=source)
                result = self.commands.run_argv(self, argv)
        except CommandError as e:
            if raise_errors:
                raise
            self.message(str(e), "err")
            self.log.info("command failed (%s): %s", line, e)
        except ValueError as e:
            err = CommandError("syntax error: %s" % e)
            if raise_errors:
                raise err
            self.message(str(err), "err")
        except Exception as e:
            self.log.error("command %r crashed:\n%s", line, traceback.format_exc())
            if raise_errors:
                raise CommandError("internal error: %s" % e)
            self.message("internal error: %s" % e, "err")
        finally:
            self._source = prev_source
        self.dirty = True
        if source in self.INTERACTIVE_SOURCES:
            try:
                self.show_command_result(line, result)
            except Exception:
                self.log.error("showing the result of %r:\n%s", line, traceback.format_exc())
        return result

    INTERACTIVE_SOURCES = ("prompt", "palette", "key", "mouse")

    def show_command_result(self, line: str, result):
        """Commands like `plugin list` or `effect list` return text; from the prompt, palette or a hotkey nobody reads
        the return value, so show it: short one-liners in the status line, everything else in a text window."""
        if isinstance(result, (list, tuple)) and result and all(isinstance(x, str) for x in result):
            result = "\n".join(result)
        if not isinstance(result, str):
            return
        text = result.rstrip("\n")
        if not text.strip():
            self.message("(empty)", "ok")
            return
        area = self.content_area()
        if "\n" not in text and len(text) <= max(20, area.w - 6):
            self.message(text, "ok", ttl=min(12.0, 3.0 + len(text) / 20.0))
            return
        title = ":" + line.strip().split(";")[-1].strip()
        lines = text.split("\n")
        w = min(max(30, max(len(l) for l in lines) + 4), max(20, int(area.w * 0.95)))
        h = min(len(lines) + 2, max(5, int(area.h * 0.85)))
        rect = [area.x + (area.w - w) // 2, area.y + (area.h - h) // 2, w, h]
        for win in list(self.windows.values()):                # replace the previous result window
            if win.kind == "text" and win.name == "command-result":
                self.close_window(win.id)
        self.create_window({"kind": "text", "title": title, "text": text, "name": "command-result", "floating": True, "rect": rect})

    def execute(self, line: str, source: str = "api") -> dict:
        with self.lock:
            try:
                res = self.run_command_line(line, source, raise_errors=True)
                return {"ok": True, "result": res}
            except CommandError as e:
                return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ dialogs
    def close_dialog(self, d):
        self.dialogs.remove(d)

    def open_dialog(self, d):
        return self.dialogs.add(d)

    # ------------------------------------------------------------------ IO plumbing (used by server loop)
    def iter_read_fds(self):
        for w in list(self.windows.values()):
            src = w.source
            if src is None:
                continue
            for fd, stream in src.read_fds().items():
                yield fd, w, stream

    def iter_write_fds(self):
        for w in list(self.windows.values()):
            src = w.source
            if src is not None and src.wants_write:
                fd = src.write_fd()
                if fd is not None:
                    yield fd, w

    def source_readable(self, w: Window, fd: int, stream: str):
        src = w.source
        if src is None:
            return
        data = src.read(fd)
        if data is None:
            self._source_eof(w)
            return
        if data:
            w.feed_bytes(data, stream)
            self.dirty = True

    def poll_sources(self):
        for w in list(self.windows.values()):
            src = w.source
            if src is None or w.exited:
                continue
            if src.poll_based:
                data = src.poll_data()
                if data:
                    w.feed_bytes(data, "out")
                    self.dirty = True
            if not src.alive and not w.exited:
                self._source_eof(w)
            elif src.tty and not w.exited and hasattr(src, "proc") and src.proc.poll() is not None:
                # child gone: read remaining output then finish
                try:
                    while True:
                        d = src.read(src.master)
                        if d is None:
                            break
                        if d:
                            w.feed_bytes(d, "out")
                        else:
                            break
                except Exception:
                    pass
                self._source_eof(w)

    def _source_eof(self, w: Window):
        if w.exited:
            return
        src = w.source
        try:
            if hasattr(src, "finish"):
                src.finish()
        except Exception:
            pass
        code = getattr(src, "exit_code", None)
        w.on_exit(code)
        self.log.info("window %d process exited (%s)", w.id, code)
        self.emit("window_exit", window=w, code=code)
        policy = w.opts.get("on_exit", "close")
        if policy == "close":
            self.close_window(w.id)
        elif policy == "restart":
            self.restart_window(w)
        else:
            w.screen.feed("\r\n\x1b[0;2m[process exited with status %s - press a key to close]\x1b[0m" % code)
            w.screen.cursor_visible = False
            self.dirty = True

    def restart_window(self, w: Window):
        spec = dict(w.spec)
        if w.source:
            try:
                w.source.close()
            except Exception:
                pass
        w.exited = False
        w.screen.feed("\r\n\x1b[2m[restarting]\x1b[0m\r\n")
        try:
            self._start_window(w, spec)
        except Exception as e:
            self.message("restart failed: %s" % e, "err")

    # ------------------------------------------------------------------ output hooks / routing
    def window_output(self, w: Window, text: str, stream: str, raw: bytes, bell: Optional[bool] = None):
        self.dirty = True
        if (w.desktop is not self.desk or w.id != w.desktop.focus) and time.time() - w.resized_at > 1.0:
            w.activity = True
        if bell is None:                  # not parsed by a screen: a BEL that only ends an OSC (window title) is no bell
            bell = "\x07" in _OSC_RE.sub("", text)
        if bell:
            w.bell = True
            self.emit("bell", window=w)
        r = w.routes.get(stream)
        if r and r["sinks"] and w.id not in self._routing:
            self._routing.add(w.id)
            try:
                for kind, target in r["sinks"]:
                    self._deliver(kind, target, raw)
            finally:
                self._routing.discard(w.id)
        self.emit("window_output", window=w, text=text, stream=stream)

    def _deliver(self, kind: str, target, raw: bytes):
        if kind == "input":
            dst = self.windows.get(target)
            if dst:
                dst.write_input(raw)
        elif kind == "display":
            dst = self.windows.get(target)
            if dst:
                dst.feed_bytes(raw)
        elif kind == "file":
            f = self._route_files.get(target)
            if f is None:
                f = open(os.path.expanduser(target), "ab", buffering=0)
                self._route_files[target] = f
            f.write(raw)

    def set_route(self, src: Window, stream: str, kind: Optional[str], target=None, mute: bool = False, append: bool = True):
        r = src.routes.setdefault(stream, {"self": True, "sinks": []})
        if kind is None:       # reset
            src.routes.pop(stream, None)
            return
        if not append:
            r["sinks"] = []
        if kind != "discard":
            entry = (kind, target)
            if entry not in r["sinks"]:
                r["sinks"].append(entry)
        r["self"] = not mute and kind != "discard"
        if kind == "discard":
            r["self"] = False
        self.emit("route_changed", window=src, stream=stream)

    def clear_routes_of(self, wid: int):
        for w in self.windows.values():
            for stream, r in list(w.routes.items()):
                r["sinks"] = [s for s in r["sinks"] if not (s[0] in ("input", "display") and s[1] == wid)]
                if not r["sinks"] and r["self"]:
                    w.routes.pop(stream, None)
        self.windows.get(wid) and self.windows[wid].routes.clear()

    # ------------------------------------------------------------------ periodic work
    def tick(self, now: Optional[float] = None):
        now = now or time.time()
        self.selection_tick(now)
        for w in list(self.windows.values()):
            try:
                w.on_tick(now)
            except Exception:
                self.log.error("window %s tick failed:\n%s", w.id, traceback.format_exc())
        if self.plugins:
            self.plugins.tick(now)
        if self.rules:
            self.rules.tick(now)
        # dirty once per second for clocks etc.
        if int(now) != getattr(self, "_last_sec", None):
            self._last_sec = int(now)
            self.dirty = True
        for w in list(self.windows.values()):
            if isinstance(w, InternalWindow) and w.dirty:
                self.dirty = True
        bg = self.background
        if bg is not None and now - self._bg_frame_at >= 1.0 / (getattr(bg, "fps", 0) or 20.0):
            self._bg_frame_at = now
            self.dirty = True     # the next frame of the animated background (capped at its fps)
        self.emit("tick", now=now)

    # ------------------------------------------------------------------ state export
    def state(self) -> dict:
        return {
            "session": self.session_name,
            "size": [self.cols, self.rows],
            "theme": self.theme.name,
            "current_desktop": self.cur,
            "desktops": [dict(d.describe(), index=i, current=(i == self.cur)) for i, d in enumerate(self.desktops)],
            "windows": {str(w.id): w.describe() for w in self.windows.values()},
            "focus": self.desk.focus,
            "keymap_mode": self.keymap.mode,
            "status": {k: v["value"] for k, v in self.status_items.items()},
            "progress": self.progress,
            "uptime": time.time() - self.start_time,
        }

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        self._closing = True
        if self.screen_recorder is not None:
            self.screen_recorder.close()
            self.screen_recorder = None
        for w in list(self.windows.values()):
            try:
                w.close()
            except Exception:
                pass
        for f in self._route_files.values():
            try:
                f.close()
            except Exception:
                pass
        if self.plugins:
            try:
                self.plugins.unload_all()
            except Exception:
                pass
