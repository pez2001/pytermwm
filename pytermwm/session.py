"""Session snapshots: save / restore layouts, windows and (optionally) their last screen text."""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

from .commands import CommandError
from .layout import TileTree
from .protocol import state_dir

SNAPSHOT_VERSION = 1


def sessions_dir() -> str:
    d = os.path.join(state_dir(), "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def default_path(name: str) -> str:
    return os.path.join(sessions_dir(), "%s.json" % name)


def _win_snapshot(w, include_text: bool, text_lines: int) -> dict:
    spec = {k: v for k, v in (w.spec or {}).items() if k not in ("focus", "desktop", "rect", "split")}
    d = {
        "kind": w.kind, "spec": spec, "name": w.name, "title": w.title_text, "cwd": w.cwd(),
        "opts": {k: (v if not isinstance(v, tuple) else list(v)) for k, v in w.opts.items() if k != "stderr_color"},
        "floating": w.floating, "frect": list(w.frect) if w.frect else None,
        "dock": list(w.dock) if w.dock else None, "vsize": list(w.vsize) if w.vsize else None,
    }
    if w.opts.get("stderr_color") is not None:
        d["opts"]["stderr_color"] = str(w.opts["stderr_color"])
    if include_text and w.kind == "term":
        t = w.screen.text(history=True)
        d["text"] = "\n".join(t.split("\n")[-text_lines:])
    return d


def snapshot(wm, include_text: bool = True, text_lines: int = 300) -> dict:
    with wm.lock:
        desktops = []
        for d in wm.desktops:
            desktops.append({
                "name": d.name, "layout": d.layout, "params": d.params,
                "windows": list(d.windows), "focus": d.focus, "zoom": d.zoom,
                "tree": d.tree.to_dict(),
            })
        wins = {str(w.id): _win_snapshot(w, include_text, text_lines) for w in wm.windows.values()
                if w.kind not in ("debug",)}
        return {"version": SNAPSHOT_VERSION, "session": wm.session_name, "saved": time.time(),
                "theme": wm.theme.name, "current": wm.cur, "size": [wm.cols, wm.rows],
                "desktops": desktops, "windows": wins}


def save_session(wm, path: Optional[str] = None, include_text: bool = True) -> str:
    path = path or default_path(wm.session_name)
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = snapshot(wm, include_text)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return path


def load_session_file(path: str) -> dict:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise CommandError("cannot read session file %s: %s" % (path, e))
    if data.get("version") != SNAPSHOT_VERSION:
        raise CommandError("unsupported session file version: %s" % data.get("version"))
    return data


def restore(wm, data: dict, restore_text: bool = True):
    """Recreate desktops and windows from a snapshot (into an empty WM)."""
    with wm.lock:
        idmap: Dict[int, int] = {}
        try:
            wm.set_theme(data.get("theme", "default"))
        except KeyError:
            pass
        # desktops
        for i, dd in enumerate(data["desktops"]):
            if i < len(wm.desktops):
                d = wm.desktops[i]
                d.name = dd["name"]
            else:
                d = wm.add_desktop(dd["name"])
            d.layout = dd["layout"]
            d.params = dict(dd.get("params") or {})
        wm.cur = 0
        for i, dd in enumerate(data["desktops"]):
            d = wm.desktops[i]
            wm.cur = i
            for old in dd["windows"]:
                ws = data["windows"].get(str(old))
                if ws is None:
                    continue
                spec = dict(ws.get("spec") or {})
                spec["kind"] = ws["kind"] if ws["kind"] in wm.window_kinds or ws["kind"] in ("term", "pipe", "file") else spec.get("kind", "term")
                if ws.get("cwd") and ws["kind"] in ("term", "pipe"):
                    spec["cwd"] = ws["cwd"]
                if ws.get("name"):
                    spec["name"] = ws["name"]
                if ws.get("title"):
                    spec["title"] = ws["title"]
                spec["floating"] = bool(ws.get("floating"))
                if ws.get("frect"):
                    spec["rect"] = ws["frect"]
                if ws.get("dock"):
                    spec["dock"] = ws["dock"]
                if ws.get("vsize"):
                    spec["vsize"] = ws["vsize"]
                spec["opts"] = {k: v for k, v in (ws.get("opts") or {}).items() if k != "stderr_color"}
                spec["focus"] = False
                spec["desktop"] = i + 1
                if ws["kind"] == "term" and not spec.get("cmd"):
                    spec.pop("cmd", None)
                try:
                    w = wm.create_window(spec)
                except CommandError as e:
                    wm.log.warning("restore: window %s: %s", old, e)
                    continue
                idmap[int(old)] = w.id
                if restore_text and ws.get("text") and ws["kind"] == "term":
                    w.screen.feed("\x1b[2m" + ws["text"].replace("\n", "\r\n") + "\x1b[0m\r\n\x1b[2m--- restored ---\x1b[0m\r\n")
            # tile tree
            tree = dd.get("tree")
            if tree:
                try:
                    t = TileTree.from_dict(tree, lambda x: idmap.get(int(x)))
                    leaves = [x for x in t.leaves() if x is not None]
                    # drop leaves whose windows failed to restore
                    for wid in list(t.leaves()):
                        if wid is None:
                            t.remove(None)
                    d.tree = t
                except Exception as e:
                    wm.log.warning("restore: layout tree: %s", e)
            if dd.get("focus") in idmap:
                d.focus = idmap[dd["focus"]]
            if dd.get("zoom") in idmap:
                d.zoom = idmap[dd["zoom"]]
            wm.relayout()
        wm.cur = min(int(data.get("current", 0)), len(wm.desktops) - 1)
        wm.relayout()
        wm.log.info("session restored: %d windows", len(idmap))
        return idmap


def list_saved() -> List[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(sessions_dir()) if f.endswith(".json"))
    except OSError:
        return []
