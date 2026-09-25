"""Plugin system.

A plugin is a python module (a single ``.py`` file in a plugin directory, or one of the
built-ins in :mod:`pytermwm.contrib`) exposing::

    def setup(api):            # called on load;  api.config holds the plugin's config mapping
        api.command("hello", lambda wm, args: "hi", help="say hi")
        api.segment("mine", lambda wm, opts: Segment("x"))
        api.window_kind("clock", factory)
        api.key("M-g", "hello")
        api.every(5.0, lambda: ...)
        api.on("window_created", lambda window, **kw: ...)
        api.theme("neon", {...})

    def teardown(api):         # optional; everything registered through ``api`` is removed automatically

Plugins are enabled from the configuration (``plugins: [name, {name: mqtt, host: ...}]``) or with the
``plugin load`` command.  User plugins live in ``~/.config/pytermwm/plugins`` (also next to the config
file in ``plugins/`` and in ``$PYTERMWM_PLUGIN_PATH``); they are hot reloaded when the file changes.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import os

from . import compat
import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from .commands import CommandError


def _defines_setup(path: str) -> Optional[bool]:
    """Whether the module at `path` defines a top-level `setup` function, without importing it (so
    listing available plugins never runs arbitrary module-level code or fails on a missing optional
    dependency). Returns None -- "can't tell, so don't hide it" -- if the file can't be read or
    parsed; only a confirmed absence of `setup` returns False."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError, ValueError):
        return None
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "setup"
               for n in tree.body)


def user_plugin_dirs(wm=None) -> List[str]:
    dirs: List[str] = []
    for p in os.environ.get("PYTERMWM_PLUGIN_PATH", "").split(os.pathsep):
        if p:
            dirs.append(os.path.expanduser(p))
    if wm is not None:
        for p in (wm.cfg.get("plugin_dirs") or []):
            dirs.append(os.path.expanduser(str(p)))
        mgr = wm.extra.get("config_manager")
        if mgr is not None and getattr(mgr, "path", None):
            dirs.append(os.path.join(os.path.dirname(os.path.abspath(mgr.path)), "plugins"))
    xdg = compat.config_home()
    dirs.append(os.path.join(xdg, "pytermwm", "plugins"))
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


class PluginAPI:
    """What a plugin gets.  Everything registered here is undone when the plugin is unloaded."""

    def __init__(self, manager: "PluginManager", name: str, config: dict):
        self._m = manager
        self.wm = manager.wm
        self.name = name
        self.config: dict = config
        self.log = logging.getLogger("pytermwm.plugin.%s" % name)
        self.state: Dict[str, Any] = {}          # free storage for the plugin
        self._commands: List[str] = []
        self._segments: List[str] = []
        self._kinds: List[str] = []
        self._keys: Dict[str, str] = {}
        self._handlers: List[tuple] = []
        self._timers: List[list] = []
        self._themes: List[str] = []
        self._status_keys: List[str] = []
        self._threads: List[threading.Thread] = []
        self._cleanups: List[Callable] = []
        self.stop_event = threading.Event()

    # ------------------------------------------------------------- registration
    def command(self, name: str, fn: Callable, usage: str = "", help: str = "", aliases=(), completer=None):
        """fn(wm, args) -> result   (same signature as builtin commands)"""
        self.wm.commands.register(name, fn, usage or name, help, aliases, completer, "plugin:" + self.name)
        self._commands.append(name)

    def segment(self, name: str, fn: Callable):
        """fn(wm, opts) -> Segment | [Segment] | None, usable in ``statusline:`` lists."""
        from .statusline import SEGMENTS
        SEGMENTS[name] = fn
        self._segments.append(name)

    def window_kind(self, name: str, factory: Callable):
        """factory(wm, wid, spec, rows, cols, opts) -> Window"""
        self.wm.register_window_kind(name, factory)
        self._kinds.append(name)

    def key(self, key: str, command: str):
        """Bind a direct (prefix-less) key unless the user's configuration already binds it."""
        self._keys[key] = command
        self._m.apply_keys()

    def on(self, event: str, fn: Callable):
        self.wm.on(event, fn)
        self._handlers.append((event, fn))
        return fn

    def on_output(self, pattern: str, fn: Callable, window: Optional[str] = None):
        """fn(window, line, match) for every output line matching ``pattern`` (optionally only in one window)."""
        import re
        from .scripting import strip_ansi
        rx = re.compile(pattern)
        bufs: Dict[int, str] = {}

        def handler(window=None, text="", **kw):
            if window is None:
                return
            if window_filter and not (str(window.id) == window_filter or (window.name or "") == window_filter):
                return
            buf = bufs.get(window.id, "") + strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
            *lines, rest = buf.split("\n")
            bufs[window.id] = rest[-4000:]
            for line in lines:
                m = rx.search(line)
                if m:
                    fn(window, line, m)
        window_filter = str(window) if window is not None else None
        self.on("window_output", handler)

    def every(self, seconds: float, fn: Callable):
        """Call fn() every ``seconds`` from the main loop (never blocks other plugins on error)."""
        self._timers.append([float(seconds), fn, 0.0])

    def theme(self, name: str, data: dict):
        from .theme import register_theme
        register_theme(name, data)
        self._themes.append(name)

    def on_unload(self, fn: Callable):
        self._cleanups.append(fn)

    # ------------------------------------------------------------- helpers
    def call_soon(self, fn: Callable, *a, **kw):
        """Run ``fn`` on the main loop (thread safe).  Use this from worker threads."""
        self._m.queue.put((self.name, fn, a, kw))
        self.wm.wake()

    def thread(self, target: Callable, *a, **kw) -> threading.Thread:
        """Start a daemon worker thread.  ``self.stop_event`` is set when the plugin unloads."""
        t = threading.Thread(target=target, args=a, kwargs=kw, name="ptw-plugin-" + self.name, daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def status(self, key: str, value, style: str = "normal"):
        self.wm.set_status(key, value, style=style)
        if key not in self._status_keys:
            self._status_keys.append(key)

    def message(self, text: str, style: str = "normal", ttl: float = 3.0):
        self.wm.message(text, style, ttl)

    def run(self, line: str):
        return self.wm.execute(line, source="plugin")

    @property
    def data_dir(self) -> str:
        from .protocol import state_dir
        d = os.path.join(state_dir(), "plugins", self.name)
        os.makedirs(d, exist_ok=True)
        return d

    # ------------------------------------------------------------- teardown
    def _teardown(self):
        self.stop_event.set()
        for fn in reversed(self._cleanups):
            try:
                fn()
            except Exception:
                self.log.error("cleanup failed:\n%s", traceback.format_exc())
        for c in self._commands:
            self.wm.commands.unregister(c)
        from .statusline import SEGMENTS
        for s in self._segments:
            SEGMENTS.pop(s, None)
        for k in self._kinds:
            self.wm.window_kinds.pop(k, None)
        for ev, fn in self._handlers:
            self.wm.off(ev, fn)
        for k in self._status_keys:
            try:
                self.wm.status_items.pop(k, None)
            except Exception:
                pass
        for t in self._themes:
            try:
                from .theme import _user_themes
                _user_themes.pop(t, None)
            except Exception:
                pass
        self._commands, self._segments, self._kinds, self._handlers, self._timers = [], [], [], [], []
        self._keys = {}
        self._m.apply_keys()
        self.wm.dirty = True


class Loaded:
    def __init__(self, name, module, api, path, config):
        self.name, self.module, self.api, self.path, self.config = name, module, api, path, config
        self.mtime = _mtime(path)
        self.error: Optional[str] = None


def _mtime(path: Optional[str]) -> float:
    try:
        return os.stat(path).st_mtime if path else 0.0
    except OSError:
        return 0.0


class PluginManager:
    def __init__(self, wm):
        self.wm = wm
        self.plugins: Dict[str, Loaded] = {}
        self.configured: Dict[str, dict] = {}     # names owned by the configuration file
        self.queue: "queue.Queue" = queue.Queue()
        self._last_scan = 0.0
        self.errors: Dict[str, str] = {}

    # ------------------------------------------------------------- discovery
    def find(self, name: str):
        """Return (module_import_spec_kind, path_or_modname)."""
        if not name.replace("_", "").replace("-", "").isalnum():
            raise CommandError("invalid plugin name: %r" % name)
        for d in user_plugin_dirs(self.wm):
            p = os.path.join(d, name + ".py")
            if os.path.isfile(p):
                return "file", p
            p = os.path.join(d, name, "__init__.py")
            if os.path.isfile(p):
                return "file", p
        try:
            if importlib.util.find_spec("pytermwm.contrib." + name) is not None:
                return "module", "pytermwm.contrib." + name
        except (ImportError, ValueError):
            pass
        raise CommandError("plugin not found: %s (looked in %s and built-ins)" % (name, ", ".join(user_plugin_dirs(self.wm))))

    def available(self) -> List[str]:
        names = set()
        try:
            import pkgutil
            from . import contrib
            for m in pkgutil.iter_modules(contrib.__path__):
                if m.name.startswith("_"):
                    continue
                candidate = os.path.join(contrib.__path__[0], m.name + ".py")
                if _defines_setup(candidate) is False:      # not a plugin (e.g. a helper module like
                    continue                                 # ansiart.py, used internally by effects.py)
                names.add(m.name)
        except Exception:
            pass
        for d in user_plugin_dirs(self.wm):
            try:
                for f in os.listdir(d):
                    if f.endswith(".py") and not f.startswith("_") and _defines_setup(os.path.join(d, f)) is not False:
                        names.add(f[:-3])
            except OSError:
                pass
        return sorted(names)

    def names(self) -> List[str]:
        return sorted(self.plugins) + [n for n in self.available() if n not in self.plugins]

    # ------------------------------------------------------------- load / unload
    def _import(self, kind, where, name):
        if kind == "module":
            mod = importlib.import_module(where)
            return importlib.reload(mod) if where in sys.modules and getattr(mod, "__pytermwm_loaded__", False) else mod
        modname = "pytermwm_userplugin_" + name.replace("-", "_")
        spec = importlib.util.spec_from_file_location(modname, where)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(modname, None)
            raise
        return mod

    def load(self, name: str, config: Optional[dict] = None) -> Loaded:
        if name in self.plugins:
            raise CommandError("plugin %s is already loaded (use plugin reload)" % name)
        kind, where = self.find(name)
        try:
            mod = self._import(kind, where, name)
        except Exception as e:
            self.errors[name] = "%s: %s" % (type(e).__name__, e)
            self.wm.log.error("plugin %s failed to import:\n%s", name, traceback.format_exc())
            raise CommandError("plugin %s failed to import: %s" % (name, e))
        setattr(mod, "__pytermwm_loaded__", True)
        if not hasattr(mod, "setup"):
            raise CommandError("plugin %s has no setup(api) function" % name)
        api = PluginAPI(self, name, dict(config or {}))
        path = where if kind == "file" else getattr(mod, "__file__", None)
        rec = Loaded(name, mod, api, path, dict(config or {}))
        rec.builtin = kind == "module"
        try:
            mod.setup(api)
        except Exception as e:
            api._teardown()
            self.errors[name] = "%s: %s" % (type(e).__name__, e)
            self.wm.log.error("plugin %s setup failed:\n%s", name, traceback.format_exc())
            raise CommandError("plugin %s failed: %s" % (name, e))
        self.plugins[name] = rec
        self.apply_keys()
        self.errors.pop(name, None)
        self.wm.log.info("plugin loaded: %s", name)
        self.wm.emit("plugin_loaded", name=name)
        self.wm.dirty = True
        return rec

    def unload(self, name: str):
        rec = self.plugins.pop(name, None)
        if rec is None:
            raise CommandError("plugin not loaded: %s" % name)
        td = getattr(rec.module, "teardown", None)
        if td:
            try:
                td(rec.api)
            except Exception:
                self.wm.log.error("plugin %s teardown failed:\n%s", name, traceback.format_exc())
        rec.api._teardown()
        self.wm.log.info("plugin unloaded: %s", name)
        self.wm.emit("plugin_unloaded", name=name)

    def reload(self, name: str):
        rec = self.plugins.get(name)
        if rec is None:
            raise CommandError("plugin not loaded: %s" % name)
        cfg = rec.config
        self.unload(name)
        try:
            self.load(name, cfg)
        except CommandError as e:
            self.wm.message("plugin %s: %s" % (name, e), "err", 6.0)
            raise
        self.wm.message("plugin %s reloaded" % name, "ok", 2.0)

    def unload_all(self):
        for n in list(self.plugins):
            try:
                self.unload(n)
            except Exception:
                pass

    # ------------------------------------------------------------- configuration
    def configure(self, entries: List[Any]):
        """Bring the loaded set in line with the ``plugins:`` list of the configuration."""
        want: Dict[str, dict] = {}
        for e in entries:
            if isinstance(e, str):
                want[e] = {}
            elif isinstance(e, dict) and e.get("name"):
                if e.get("enabled", True) is False:
                    continue
                want[str(e["name"])] = {k: v for k, v in e.items() if k not in ("name", "enabled")}
            else:
                self.wm.log.warning("plugins: ignoring bad entry %r", e)
        for name in [n for n in self.configured if n not in want]:
            if name in self.plugins:
                self.unload(name)
            self.configured.pop(name, None)
        for name, cfg in want.items():
            rec = self.plugins.get(name)
            if rec is not None and rec.config == cfg:
                self.configured[name] = cfg
                continue
            try:
                if rec is not None:
                    self.unload(name)
                self.load(name, cfg)
                self.configured[name] = cfg
            except CommandError as e:
                self.wm.log.error("plugin %s: %s", name, e)
                self.wm.message("plugin %s: %s" % (name, e), "err", 8.0)
        self.apply_keys()

    def apply_keys(self):
        from .keys import DEFAULT_KEYS
        km = self.wm.keymap
        user = (getattr(km, "user_direct", None) or {})
        want: Dict[str, str] = {}
        for rec in self.plugins.values():
            for k, cmd in rec.api._keys.items():
                if k not in user:
                    want[k] = cmd
        applied = getattr(self, "_applied_keys", {})
        for k in applied:
            if k not in want and km.cfg["direct"].get(k) == applied[k]:
                orig = DEFAULT_KEYS["direct"].get(k)
                if orig:
                    km.cfg["direct"][k] = orig
                else:
                    km.cfg["direct"].pop(k, None)
        km.cfg["direct"].update(want)
        self._applied_keys = want

    # ------------------------------------------------------------- main loop
    def tick(self, now: float):
        while True:
            try:
                name, fn, a, kw = self.queue.get_nowait()
            except queue.Empty:
                break
            if name in self.plugins:
                try:
                    fn(*a, **kw)
                except Exception:
                    self.wm.log.error("plugin %s callback failed:\n%s", name, traceback.format_exc())
        for rec in list(self.plugins.values()):
            for t in rec.api._timers:
                if now - t[2] >= t[0]:
                    t[2] = now
                    try:
                        t[1]()
                    except Exception:
                        self.wm.log.error("plugin %s timer failed:\n%s", rec.name, traceback.format_exc())
        if now - self._last_scan >= 1.0 and self.wm.opt("plugin_autoreload", True):
            self._last_scan = now
            for name, rec in list(self.plugins.items()):
                if getattr(rec, "builtin", False):
                    continue
                m = _mtime(rec.path)
                if m and m != rec.mtime:
                    rec.mtime = m
                    try:
                        self.reload(name)
                    except CommandError:
                        pass

    # ------------------------------------------------------------- introspection
    def describe(self) -> List[dict]:
        out = []
        for name, rec in sorted(self.plugins.items()):
            a = rec.api
            out.append({"name": name, "loaded": True, "builtin": getattr(rec, "builtin", False), "path": rec.path,
                        "config": rec.config, "commands": list(a._commands), "segments": list(a._segments),
                        "kinds": list(a._kinds), "keys": dict(a._keys), "timers": len(a._timers),
                        "doc": (rec.module.__doc__ or "").strip().split("\n")[0]})
        for n in self.available():
            if n not in self.plugins:
                out.append({"name": n, "loaded": False, "error": self.errors.get(n)})
        return out
