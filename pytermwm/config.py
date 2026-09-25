"""YAML configuration: loading, validation, application and live reload."""
from __future__ import annotations

import copy
import glob
import os

from . import compat
import time
import traceback
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:      # pragma: no cover
    yaml = None

from .commands import CommandError
from .layout import ENGINE_NAMES
from .theme import BORDER_SETS, Theme, get_theme, register_theme

KNOWN_KEYS = {
    "theme", "themes", "layout", "desktops", "shell", "cwd", "history", "gap", "master_ratio", "master_count",
    "new_window_position", "on_last_close", "prompt_takeover", "mouse", "window_defaults", "confirm_quit",
    "keys", "statusline", "windows", "rules", "plugins", "web", "include", "effect", "log", "allow_eval",
    "allow_remote_debug", "session", "auto_create_desktops", "desktop_start_window", "cp437", "autosave",
    "scripts", "plugin_dirs", "notify", "selection", "charts",
}

WINDOW_KEYS = {"name", "title", "cmd", "cwd", "env", "kind", "path", "pipe", "desktop", "floating", "rect", "dock",
                "vsize", "keep", "split", "focus", "restart_on_change", "opts", "text", "follow", "topic", "level",
                "source", "chart", "interval", "ignore", "recursive", "max_lines", "mode", "lo", "hi", "unit", "color",
                "pattern", "on_exit", "speed", "idle", "loop", "autoplay"} | {"border", "scrollbar", "overflow", "cp437", "history", "stderr_color", "shadow",
                                       "readonly", "icon", "tag"}


class ConfigError(Exception):
    pass


DEFAULT_CONFIG_YAML = """\
# pytermwm configuration.  Changes to this file are applied immediately.
theme: default            # default light modern hacker bbs mc c64 amiga (or an inline theme mapping)
layout: tile              # tile master spiral columns rows grid centered monocle table float
desktops: [main, dev, logs]
history: 5000             # scrollback lines per window
gap: 0                    # cells between tiled windows
mouse: true
on_last_close: exit       # exit | respawn | keep
prompt_takeover: 2.0      # seconds the status line shows command feedback

# shell: /bin/bash
# window_defaults: {border: true, scrollbar: auto, overflow: wrap}

statusline:
  position: bottom        # top | bottom | off
  left: [desktops, layout, mode]
  center: [title]
  right: [pv, status, cpu, mem, disk, time]

keys:
  prefix: C-b
  direct:                 # keys that work without the prefix (Alt based, like i3)
    M-Enter: new-window
    M-h: focus left
    M-l: focus right
  prefix_table:           # keys after the prefix
    c: new-window
    x: close-window
  # modes:
  #   resize: {h: resize left, l: resize right, Esc: mode normal}

# Windows created/kept in sync with this file (matched by name).
windows:
  - name: shell
    title: shell
    desktop: main
  # - name: build
  #   cmd: make watch
  #   desktop: dev
  #   keep: true
  # - name: cpu
  #   kind: chart
  #   source: cpu
  #   desktop: logs

# Automation: react to output / events / time.
rules: []
  # - name: fullscreen-on-error
  #   when: {window: build, output_matches: "ERROR|FAILED"}
  #   do: ["focus $window", "zoom on", "message build failed: $line"]
  #   undo_after: 20

plugins: []              # e.g. [docker, btop, mqtt, ssh, effects, openai]

web:
  enabled: false
  host: 127.0.0.1
  port: 8765
  # token: change-me      # default: random token printed in the log and stored next to the socket
"""


def config_search_paths() -> List[str]:
    paths = []
    if os.environ.get("PYTERMWM_CONFIG"):
        paths.append(os.environ["PYTERMWM_CONFIG"])
    paths.append(os.path.join(os.getcwd(), "pytermwm.yaml"))
    xdg = compat.config_home()
    paths.append(os.path.join(xdg, "pytermwm", "config.yaml"))
    return paths


def find_config() -> Optional[str]:
    for p in config_search_paths():
        if os.path.isfile(p):
            return p
    return None


def load_yaml_text(text: str) -> dict:
    if yaml is None:
        raise ConfigError("PyYAML is required for configuration files (pip install pyyaml)")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError("YAML error: %s" % e)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a mapping at the top level")
    return data


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k in ("windows", "rules", "plugins") and isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config_file(path: str, _seen=None) -> Tuple[dict, List[str]]:
    """Load a config file (following ``include``).  Returns (config, list of files read)."""
    path = os.path.abspath(os.path.expanduser(path))
    _seen = _seen or set()
    if path in _seen:
        raise ConfigError("include loop at %s" % path)
    _seen.add(path)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    cfg = load_yaml_text(text)
    files = [path]
    inc = cfg.pop("include", None)
    if inc:
        if isinstance(inc, str):
            inc = [inc]
        base = os.path.dirname(path)
        merged: dict = {}
        for pat in inc:
            pat = os.path.expanduser(pat)
            if not os.path.isabs(pat):
                pat = os.path.join(base, pat)
            for fp in sorted(glob.glob(pat)):
                sub, sf = load_config_file(fp, _seen)
                merged = _merge(merged, sub)
                files.extend(sf)
        cfg = _merge(merged, cfg)        # the including file wins over what it includes
    return cfg, files


def validate_config(cfg: dict) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(cfg, dict):
        return ["configuration must be a mapping"], []
    for k in cfg:
        if k not in KNOWN_KEYS:
            warnings.append("unknown top-level key: %s" % k)

    def need(key, typ, desc):
        if key in cfg and cfg[key] is not None and not isinstance(cfg[key], typ):
            errors.append("%s: expected %s" % (key, desc))

    need("history", int, "an integer")
    need("gap", int, "an integer")
    need("mouse", bool, "true/false")
    need("desktops", list, "a list")
    need("keys", dict, "a mapping")
    need("statusline", dict, "a mapping")
    need("windows", list, "a list")
    need("rules", list, "a list")
    need("plugins", list, "a list")
    need("web", dict, "a mapping")
    need("window_defaults", dict, "a mapping")
    errors += _validate_selection(cfg.get("selection"))
    if "charts" in cfg:
        from .charts import GLYPH_SETS
        c = cfg["charts"]
        if not isinstance(c, dict):
            errors.append("charts: must be a mapping")
        elif c.get("glyphs") is not None and c["glyphs"] not in GLYPH_SETS:
            errors.append("charts.glyphs: %r is not one of %s" % (c["glyphs"], ", ".join(GLYPH_SETS)))
    from .notify import validate as _validate_notify
    errors += _validate_notify(cfg.get("notify"))
    if isinstance(cfg.get("web"), dict):
        from .perms import validate_tokens
        for key in ("allowed_hosts",):
            if cfg["web"].get(key) is not None and not (isinstance(cfg["web"][key], list) and all(isinstance(x, str) for x in cfg["web"][key])):
                errors.append("web.%s: must be a list of host names" % key)
        if cfg["web"].get("public_host") is not None and not isinstance(cfg["web"]["public_host"], str):
            errors.append("web.public_host: must be a host name")
        e2, w2 = validate_tokens(cfg["web"].get("tokens"))
        errors += e2
        warnings += w2
    if isinstance(cfg.get("history"), int) and cfg["history"] < 0:
        errors.append("history: must be >= 0")
    if cfg.get("layout") is not None and cfg["layout"] not in ENGINE_NAMES:
        errors.append("layout: %r is not one of %s" % (cfg["layout"], ", ".join(ENGINE_NAMES)))
    if cfg.get("on_last_close") not in (None, "exit", "respawn", "keep"):
        errors.append("on_last_close: expected exit, respawn or keep")
    if isinstance(cfg.get("master_ratio"), (int, float)) and not 0.1 <= cfg["master_ratio"] <= 0.9:
        errors.append("master_ratio: must be between 0.1 and 0.9")
    th = cfg.get("theme")
    if isinstance(th, str):
        try:
            get_theme(th) if th not in (cfg.get("themes") or {}) else None
        except KeyError:
            errors.append("theme: unknown theme %r" % th)
    elif isinstance(th, dict):
        try:
            Theme.from_dict(th)
        except (ValueError, KeyError) as e:
            errors.append("theme: %s" % e)
    elif th is not None:
        errors.append("theme: expected a name or a mapping")
    themes = cfg.get("themes")
    if themes is not None:
        if not isinstance(themes, dict):
            errors.append("themes: expected a mapping of name -> theme")
        else:
            for n, t in themes.items():
                try:
                    Theme.from_dict(t if isinstance(t, dict) else {})
                except (ValueError, KeyError) as e:
                    errors.append("themes.%s: %s" % (n, e))
    sl = cfg.get("statusline")
    if isinstance(sl, dict):
        from .statusline import SEGMENTS
        for zone in ("left", "center", "right"):
            for item in sl.get(zone) or []:
                nm = item if isinstance(item, str) else (item.get("segment") if isinstance(item, dict) else None)
                if isinstance(item, dict) and "text" in item and not nm:
                    continue
                if nm and nm not in SEGMENTS and not _plugin_segment_possible(cfg, nm):
                    warnings.append("statusline.%s: unknown segment %r" % (zone, nm))
        if sl.get("position") not in (None, "top", "bottom", "off"):
            errors.append("statusline.position: expected top, bottom or off")
    wins = cfg.get("windows")
    if isinstance(wins, list):
        seen = set()
        for i, w in enumerate(wins):
            if not isinstance(w, dict):
                errors.append("windows[%d]: expected a mapping" % i)
                continue
            for k in w:
                if k not in WINDOW_KEYS:
                    warnings.append("windows[%d]: unknown key %r" % (i, k))
            n = w.get("name")
            if n is not None:
                if n in seen:
                    errors.append("windows[%d]: duplicate name %r" % (i, n))
                seen.add(n)
    rules = cfg.get("rules")
    if isinstance(rules, list):
        try:
            from .scripting import validate_rules
            errors.extend(validate_rules(rules))
        except ImportError:
            pass
    return errors, warnings


def _validate_selection(sel) -> List[str]:
    if sel is None:
        return []
    if not isinstance(sel, dict):
        return ["selection: expected a mapping"]
    errs = []
    known = {"copy_on_release", "modifier", "paste_buttons", "osc52", "command", "word_chars"}
    for k in sel:
        if k not in known:
            errs.append("selection: unknown key %r" % k)
    for k in ("copy_on_release", "osc52"):
        if k in sel and not isinstance(sel[k], bool):
            errs.append("selection.%s: expected true/false" % k)
    if sel.get("modifier") is not None and str(sel["modifier"]).lower() not in ("alt", "shift", "ctrl", "none"):
        errs.append("selection.modifier: alt, shift, ctrl or none")
    pb = sel.get("paste_buttons")
    if pb is not None and (not isinstance(pb, list) or any(str(b).lower() not in ("middle", "right") for b in pb)):
        errs.append("selection.paste_buttons: a list of middle and/or right")
    for k in ("command", "word_chars"):
        if sel.get(k) is not None and not isinstance(sel[k], str):
            errs.append("selection.%s: expected a string" % k)
    return errs


def _plugin_segment_possible(cfg, name):
    return bool(cfg.get("plugins"))


# ----------------------------------------------------------------------------- application
def apply_config(wm, cfg: dict, initial: bool = False):
    """Validate and apply ``cfg`` to the window manager.  Raises ConfigError (state untouched) on errors."""
    errors, warnings = validate_config(cfg)
    if errors:
        raise ConfigError("; ".join(errors))
    for w in warnings:
        wm.log.warning("config: %s", w)
    if not cfg.get("plugins"):      # plugins may add window kinds; without them the kind must already exist
        known = set(wm.window_kinds) | {"term", "pipe", "file"}
        bad = ["windows[%d]: unknown kind %r" % (i, w["kind"]) for i, w in enumerate(cfg.get("windows") or [])
               if isinstance(w, dict) and w.get("kind") and w["kind"] not in known]
        if bad:
            raise ConfigError("; ".join(bad))
    old = wm.cfg
    merged = dict(_default_cfg())
    merged.update({k: v for k, v in cfg.items() if v is not None})
    wm.cfg = merged
    # themes
    for n, t in (cfg.get("themes") or {}).items():
        register_theme(n, t)
    th = cfg.get("theme", "default")
    if wm.theme.name != th or isinstance(th, dict):
        wm.set_theme(th)
    # keys
    wm.keymap.set_config(cfg.get("keys") or {})
    # status line
    wm.statusline.set_config(cfg.get("statusline"))
    # desktops
    _apply_desktops(wm, cfg.get("desktops"), cfg.get("layout"), initial or not old)
    # per-window defaults / history limit for existing windows are left alone
    # logging level
    lg = cfg.get("log") or {}
    if isinstance(lg, dict) and lg.get("level"):
        import logging
        from .logs import LEVELS
        wm.log.setLevel(LEVELS.get(str(lg["level"]).lower(), logging.INFO))
    # plugins & rules
    _apply_plugins(wm, cfg.get("plugins"))
    _apply_rules(wm, cfg)
    # windows
    _reconcile_windows(wm, cfg.get("windows") or [], initial)
    # effect
    if "effect" in cfg:
        try:
            wm.load_plugin_by_name("effects")
            eff = wm.extra.get("effects_command")
            if eff:
                e = cfg["effect"]
                if isinstance(e, dict):                    # effect: {name: ansi, path: ~/art, hold: 20} / {name: matrix, color: red}
                    eff(wm, [str(e.get("name") or "off")], {k: v for k, v in e.items() if k != "name"})
                else:
                    eff(wm, [str(e)] if e else ["off"])
        except Exception as e:
            wm.log.warning("effect: %s", e)
    wm.relayout()
    wm.emit("config_applied", config=cfg)


def _default_cfg() -> dict:
    from .wm import DEFAULT_CONFIG
    return copy.deepcopy(DEFAULT_CONFIG)


def _apply_desktops(wm, desktops, default_layout, initial):
    if default_layout and initial:
        wm.desk.layout = default_layout
    if not desktops:
        return
    for i, spec in enumerate(desktops):
        name, layout, params = spec, None, {}
        if isinstance(spec, dict):
            name = spec.get("name") or str(i + 1)
            layout = spec.get("layout")
            params = spec.get("params") or {}
        name = str(name)
        if i < len(wm.desktops):
            d = wm.desktops[i]
            d.name = name
        else:
            d = wm.add_desktop(name, layout or default_layout)
        if layout:
            if layout not in ENGINE_NAMES:
                raise ConfigError("desktops[%d]: unknown layout %r" % (i, layout))
            d.layout = layout
        d.params.update(params)


def _apply_plugins(wm, plugins):
    if plugins is None and wm.plugins is None:
        return
    try:
        from .plugins import PluginManager
    except ImportError:
        return
    if wm.plugins is None:
        wm.plugins = PluginManager(wm)
    wm.plugins.configure(plugins or [])


def _apply_rules(wm, cfg):
    try:
        from .scripting import RuleEngine
    except ImportError:
        return
    if wm.rules is None:
        wm.rules = RuleEngine(wm)
    wm.rules.load(cfg.get("rules") or [], cfg.get("scripts") or [])


def _reconcile_windows(wm, specs: List[dict], initial: bool):
    """Create/update/close config declared windows so the state follows the file."""
    tracked: Dict[str, dict] = wm.extra.setdefault("config_windows", {})
    new_names = set()
    for spec in specs:
        name = spec.get("name")
        if not name:
            # unnamed windows only get created on the initial load
            if initial:
                _create_from_spec(wm, spec)
            continue
        new_names.add(name)
        existing = None
        for w in wm.windows.values():
            if w.name == name:
                existing = w
                break
        if existing is None:
            if name in tracked and not initial and tracked[name].get("closed_by_user"):
                continue
            w = _create_from_spec(wm, spec)
            if w is not None:
                tracked[name] = {"spec": copy.deepcopy(spec), "wid": w.id}
        else:
            prev = tracked.get(name, {}).get("spec")
            if prev != spec:
                _update_window(wm, existing, spec, prev)
                tracked[name] = {"spec": copy.deepcopy(spec), "wid": existing.id}
    for name in list(tracked):
        if name not in new_names:
            for w in list(wm.windows.values()):
                if w.name == name:
                    wm.close_window(w.id, _no_respawn=True)
            tracked.pop(name, None)


def _create_from_spec(wm, spec: dict):
    s = dict(spec)
    s.pop("restart_on_change", None)
    if wm.windows or True:
        s.setdefault("focus", False if wm.windows else True)
    try:
        return wm.create_window(s)
    except CommandError as e:
        wm.log.error("config window %s: %s", spec.get("name"), e)
        wm.message("config window %s: %s" % (spec.get("name"), e), "err")
        return None


def _update_window(wm, w, spec: dict, prev: Optional[dict]):
    if "title" in spec:
        w.title_text = spec["title"] or ""
    for k, v in list(spec.items()):
        if k in ("border", "scrollbar", "overflow", "cp437", "history", "stderr_color", "shadow", "readonly", "icon", "tag", "on_exit"):
            try:
                w.set_option(k, v)
            except (ValueError, KeyError) as e:
                wm.log.warning("config window %s: %s", w.name, e)
    if "opts" in spec:
        for k, v in (spec["opts"] or {}).items():
            try:
                w.set_option(k, v)
            except (ValueError, KeyError):
                pass
    if "floating" in spec and bool(spec["floating"]) != w.floating:
        wm.run_command_line("float %s %d" % ("on" if spec["floating"] else "off", w.id))
    if spec.get("rect"):
        w.frect = tuple(int(x) for x in spec["rect"])
    if "dock" in spec and spec["dock"]:
        d = spec["dock"]
        if isinstance(d, str):
            edge, _, size = d.partition(":")
            d = (edge, float(size) if size else 30)
        w.dock = (str(d[0]), float(d[1]))
    elif prev and prev.get("dock") and not spec.get("dock"):
        w.dock = None
    if "vsize" in spec and spec["vsize"]:
        v = spec["vsize"]
        if isinstance(v, str):
            a, _, b = v.lower().partition("x")
            v = (int(a), int(b))
        w.set_vsize((int(v[0]), int(v[1])))
    if spec.get("desktop") not in (None, "") and w.desktop is not None:
        try:
            idx = wm.resolve_desktop(spec["desktop"])
            if wm.desktops[idx] is not w.desktop:
                wm.send_to_desktop(w, idx + 1)
        except CommandError:
            pass
    # command change => restart the process
    if prev is not None and spec.get("restart_on_change", True):
        if (prev.get("cmd") != spec.get("cmd") or prev.get("cwd") != spec.get("cwd") or prev.get("env") != spec.get("env")) \
                and w.kind in ("term", "pipe"):
            w.spec = dict(spec)
            wm.restart_window(w)
    if w.kind == "chart":
        for k in ("source", "chart", "interval", "lo", "hi", "unit", "color"):
            if k in spec and hasattr(w, k if k != "chart" else "chart"):
                setattr(w, k, spec[k])
    w.spec = dict(spec)
    wm.relayout()


# ----------------------------------------------------------------------------- loader / watcher
class ConfigManager:
    """Loads the config file and watches it (and its includes) for changes."""

    def __init__(self, wm, path: Optional[str]):
        self.wm = wm
        self.path = path
        self.files: List[str] = []
        self.stamps: Dict[str, Tuple[float, int]] = {}
        self.last_check = 0.0
        self.last_error: Optional[str] = None
        self.loaded = False

    def _stamp(self, fp):
        try:
            st = os.stat(fp)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def load(self, initial: bool = False) -> dict:
        if not self.path:
            return {}
        cfg, files = load_config_file(self.path)
        self.files = files
        apply_config(self.wm, cfg, initial=initial)
        self.stamps = {f: self._stamp(f) for f in files}
        self.loaded = True
        self.last_error = None
        return cfg

    def check(self, now: float):
        if not self.path or now - self.last_check < 0.5:
            return
        self.last_check = now
        files = self.files or [self.path]
        changed = any(self._stamp(f) != self.stamps.get(f) for f in files)
        if not changed:
            return
        # wait for the writer to finish (editors write in several steps)
        self.stamps = {f: self._stamp(f) for f in files}
        try:
            self.load()
            self.wm.log.info("config reloaded from %s", self.path)
            self.wm.message("config reloaded", "ok")
        except (ConfigError, CommandError) as e:
            self.last_error = str(e)
            self.wm.log.error("config error (state unchanged): %s", e)
            self.wm.message("config error: %s" % e, "err", 8.0)
        except Exception as e:
            self.last_error = str(e)
            self.wm.log.error("config reload failed:\n%s", traceback.format_exc())
            self.wm.message("config reload failed: %s" % e, "err", 8.0)
            self.stamps = {f: self._stamp(f) for f in files}


def reload_config(wm) -> str:
    mgr = wm.extra.get("config_manager")
    if mgr is None or not mgr.path:
        raise CommandError("no configuration file in use")
    try:
        mgr.load()
    except ConfigError as e:
        raise CommandError("config error: %s" % e)
    wm.message("config reloaded", "ok")
    return "reloaded %s" % mgr.path


def attach_config(wm, path: Optional[str], initial: bool = True) -> ConfigManager:
    mgr = ConfigManager(wm, path)
    wm.extra["config_manager"] = mgr
    wm.config_path = path
    if path:
        mgr.load(initial=initial)
    wm.on("tick", lambda now=None, **kw: mgr.check(now or time.time()))
    return mgr
