"""Control operations shared by the unix socket, HTTP API, MCP server and CLI."""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from .commands import CommandError
from .keys import keys_to_bytes


def window_text(wm, ref, history=False, lines: Optional[int] = None) -> str:
    w = wm.resolve_window(ref)
    t = w.text(history=history)
    if lines:
        t = "\n".join(t.split("\n")[-lines:])
    return t


def frame_text(wm) -> dict:
    from .render import Compositor
    f = Compositor(wm).compose(wm.cols, wm.rows)
    return {"cols": f.cols, "rows": f.rows, "text": f.text(), "cursor": f.cursor}


def frame_json(wm) -> dict:
    """Frame as rows of styled runs for the web UI."""
    from .render import Compositor
    from .colors import to_rgb, REVERSE, BOLD, DIM, ITALIC, UNDERLINE, STRIKE, TAIL
    f = Compositor(wm).compose(wm.cols, wm.rows)
    rows = []
    for row in f.cells:
        runs = []           # [chars, style, cells]
        cur = None
        for c in row:
            if c[3] & TAIL:              # second half of a wide character: belongs to the previous run
                if cur is not None:
                    cur[2] += 1
                continue
            st = (c[1], c[2], c[3] & 0xFF)
            if cur is not None and cur[3] == st:
                cur[0].append(c[0] or " ")
                cur[2] += 1
            else:
                cur = [[c[0] or " "], None, 1, st]
                runs.append(cur)
        out = []
        for chars, _, ncells, (fg, bg, fl) in runs:
            fr, br = to_rgb(fg), to_rgb(bg)
            out.append([("".join(chars)), "%02x%02x%02x" % fr if fr else "", "%02x%02x%02x" % br if br else "", fl, ncells])
        rows.append(out)
    return {"cols": f.cols, "rows": f.rows, "lines": rows, "cursor": f.cursor, "title": f.title,
            "cursor_shape": f.cursor_shape,
            "seq": wm.frame_seq if hasattr(wm, "frame_seq") else 0}


def handle_op(wm, req: Dict[str, Any], scope: Optional[str] = None) -> Dict[str, Any]:
    """Execute one control request under the WM lock.  Always returns a dict with ``ok``.

    ``scope`` restricts a scoped API token (see :mod:`pytermwm.perms`); None is the full session token."""
    op = req.get("op", "command")
    with wm.lock:
        try:
            if scope is not None:
                from . import perms
                why = perms.check(scope, wm, req)
                if why:
                    return {"ok": False, "error": why, "denied": True}
                req = perms.restrict(scope, req)
            return {"ok": True, **_dispatch(wm, op, req)}
        except CommandError as e:
            return {"ok": False, "error": str(e)}
        except KeyError as e:
            return {"ok": False, "error": "missing field: %s" % e}
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": "bad request: %s" % e}


def _dispatch(wm, op: str, req: dict) -> dict:
    if op == "command":
        line = req["line"] if "line" in req else req["command"]
        res = wm.run_command_line(line, source=req.get("source", "api"), raise_errors=True)
        return {"result": res}
    if op == "state":
        return {"state": wm.state()}
    if op == "windows":
        return {"windows": [w.describe() for w in wm.windows.values()]}
    if op == "capture":
        return {"text": window_text(wm, req.get("window"), bool(req.get("history")), req.get("lines"))}
    if op == "send":
        w = wm.resolve_window(req.get("window"))
        if "keys" in req:
            data = keys_to_bytes(req["keys"], w.screen.app_cursor)
        elif "text" in req:
            data = req["text"].encode()
            if req.get("enter"):
                data += b"\r"
        else:
            raise CommandError("send needs keys or text")
        w.write_input(data)
        return {"sent": len(data)}
    if op == "create":
        w = wm.create_window(req.get("spec") or {})
        return {"id": w.id, "title": w.title}
    if op == "close":
        wm.close_window(req["window"])
        return {}
    if op == "feed":
        w = wm.resolve_window(req.get("window"))
        data = req["data"]
        b = data.encode() if isinstance(data, str) else bytes(data)
        w.feed_bytes(b)
        return {"fed": len(b)}
    if op == "frame":
        return frame_text(wm)
    if op == "frame_json":
        return frame_json(wm)
    if op == "keys":       # input for the WM itself (as if typed on the keyboard)
        from .keys import KeyParser
        p = KeyParser()
        raw = req["data"].encode() if isinstance(req["data"], str) else req["data"]
        evs = p.feed(raw) + p.flush()
        wm.process_events(evs)
        return {"events": len(evs)}
    if op == "key":        # named keys for the WM itself
        names = req["keys"] if isinstance(req["keys"], list) else str(req["keys"]).split()
        for n in names:
            wm.handle_key(n, b"")
        return {}
    if op == "status_set":
        wm.set_status(req["key"], req["value"], req.get("label"), req.get("style", "normal"), req.get("ttl"))
        return {}
    if op == "progress":
        wm.update_progress(req["name"], float(req.get("current", 0)), float(req.get("total", 0)), req.get("label", ""),
                           float(req.get("rate", 0)), bool(req.get("done", False)))
        return {}
    if op == "logs":
        from .logs import ring_of
        ring = ring_of(wm.log)
        return {"logs": ring.tail(int(req.get("n", 100)), req.get("level"), req.get("pattern")) if ring else []}
    if op == "events":
        since = int(req.get("since", 0))
        evs = [e for e in wm.event_ring if e["seq"] > since]
        return {"events": evs, "seq": wm.event_seq}
    if op == "commands":
        return {"commands": [{"name": c.name, "usage": c.usage, "help": c.help, "category": c.category, "aliases": list(c.aliases)}
                             for c in sorted(wm.commands.commands.values(), key=lambda c: c.name) if not c.hidden]}
    if op == "config_get":
        mgr = wm.extra.get("config_manager")
        text = ""
        if mgr and mgr.path and os.path.exists(mgr.path):
            with open(mgr.path, encoding="utf-8") as f:
                text = f.read()
        else:
            from .config import DEFAULT_CONFIG_YAML
            text = DEFAULT_CONFIG_YAML
        from .config import DEFAULT_CONFIG_YAML as _DEFAULT
        return {"text": text, "path": mgr.path if mgr else None, "error": mgr.last_error if mgr else None,
                "active": wm.cfg, "default": _DEFAULT}
    if op == "config_validate":
        from .config import load_yaml_text, validate_config, ConfigError
        try:
            cfg = load_yaml_text(req["text"])
        except ConfigError as e:
            return {"valid": False, "errors": [str(e)], "warnings": []}
        errors, warnings = validate_config(cfg)
        return {"valid": not errors, "errors": errors, "warnings": warnings}
    if op == "config_put":
        return _config_put(wm, req["text"])
    if op == "rules":
        return {"rules": wm.ensure_rules().describe()}
    if op == "plugins":
        return {"plugins": wm.ensure_plugins().describe()}
    if op == "themes":
        from .theme import all_theme_names
        return {"themes": all_theme_names(), "current": wm.theme.name}
    if op == "help":
        from .helpdata import get_topic
        t, body = get_topic(wm, req.get("topic", "index"))
        return {"title": t, "text": body}
    if op == "detach":
        wm.detach_requested = True
        wm.detach_all = True
        return {}
    if op == "quit":
        wm.quit_requested = True
        return {}
    if op == "dialogs":
        return {"dialogs": [d.describe() for d in wm.dialogs.stack]}
    if op == "script_get":
        return _script_get(wm, req.get("name"))
    if op == "script_put":
        return _script_put(wm, req["name"], req["text"])
    if op == "resize":
        srv = getattr(wm, "server", None)
        if srv is not None and any(c.hello and c.writer for c in srv.clients):
            raise CommandError("a terminal is attached; the session follows its size")
        wm.resize(int(req["cols"]), int(req["rows"]))
        return {"cols": wm.cols, "rows": wm.rows}
    if op == "selection":
        return wm.selection_info()
    if op == "ping":
        return {"pong": time.time()}
    raise CommandError("unknown op: %s" % op)


def scripts_dir(wm) -> str:
    mgr = wm.extra.get("config_manager")
    base = os.path.dirname(mgr.path) if mgr and mgr.path else os.getcwd()
    d = os.path.join(base, "scripts")
    return d


def _script_get(wm, name):
    d = scripts_dir(wm)
    if not name:
        try:
            names = sorted(f for f in os.listdir(d) if f.endswith(".py"))
        except OSError:
            names = []
        return {"scripts": names, "dir": d}
    if not re.match(r"^[\w.-]+\.py$", name):
        raise CommandError("invalid script name")
    p = os.path.join(d, name)
    try:
        with open(p, encoding="utf-8") as f:
            return {"name": name, "text": f.read()}
    except OSError:
        return {"name": name, "text": ""}


def _script_put(wm, name, text):
    if not re.match(r"^[\w.-]+\.py$", name):
        raise CommandError("invalid script name (use letters, digits, _ . - and end with .py)")
    d = scripts_dir(wm)
    os.makedirs(d, exist_ok=True)
    try:
        compile(text, name, "exec")
    except SyntaxError as e:
        raise CommandError("syntax error line %s: %s" % (e.lineno, e.msg))
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)
    wm.ensure_rules().reload_scripts()
    return {"saved": os.path.join(d, name)}


def _config_put(wm, text: str) -> dict:
    from .config import load_yaml_text, validate_config, ConfigError
    mgr = wm.extra.get("config_manager")
    try:
        cfg = load_yaml_text(text)
    except ConfigError as e:
        raise CommandError(str(e))
    errors, warnings = validate_config(cfg)
    if errors:
        raise CommandError("invalid configuration: " + "; ".join(errors))
    if not mgr or not mgr.path:
        # no file in use: apply in memory only
        wm.configure(cfg)
        return {"applied": True, "saved": False, "warnings": warnings}
    tmp = mgr.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, mgr.path)
    try:
        mgr.load()
    except ConfigError as e:
        raise CommandError("saved, but applying failed: %s" % e)
    return {"applied": True, "saved": True, "path": mgr.path, "warnings": warnings}
