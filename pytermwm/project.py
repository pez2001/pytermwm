"""Project workspaces: ``pytermwm up`` reads ``.pytermwm.yaml`` from a repository and builds the desktops for it.

    name: myapp                 # session name (default: the directory name)
    layout: tile                # default layout for the desktops below
    env: {FLASK_ENV: dev}       # added to every window's environment
    focus: editor               # desktop to show when done (default: the first)
    desktops:
      - name: editor
        layout: master
        windows:
          - {cmd: "nvim .", title: edit}
          - {cmd: "git status -sb", name: git}
      - name: run
        windows:
          - {cmd: "python -m pytest -f", name: tests}
          - {kind: file, path: logs/app.log, title: app log}

Windows use the same keys as ``windows:`` in the configuration.  ``cwd`` defaults to the project directory and a
relative ``cwd`` or ``path`` is relative to it.  Running ``up`` again is safe: desktops and named windows that already
exist are left alone, only missing ones are created.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .commands import CommandError
from .config import ConfigError, WINDOW_KEYS, load_yaml_text

FILE_NAMES = (".pytermwm.yaml", ".pytermwm.yml", "pytermwm.project.yaml")
TOP_KEYS = {"name", "layout", "env", "focus", "desktops", "windows", "description"}


def find_project_file(start: Optional[str] = None) -> Optional[str]:
    """Look for a project file in ``start`` and its parents."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        for n in FILE_NAMES:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    doc = load_yaml_text(text)
    errors, _ = validate(doc)
    if errors:
        raise ConfigError("%s: %s" % (path, "; ".join(errors)))
    return doc


def _desktop_list(doc: dict) -> List[dict]:
    """Normalise ``desktops`` (names, mappings, or a top-level ``windows`` list) to a list of mappings."""
    out: List[dict] = []
    for d in doc.get("desktops") or []:
        out.append({"name": d} if isinstance(d, str) else dict(d))
    if doc.get("windows"):                       # shorthand: a desktop called "main" (the session's first one)
        main = next((d for d in out if d.get("name") == "main"), None)
        if main is None:
            out.append({"name": "main", "windows": list(doc["windows"])})
        else:
            main["windows"] = list(main.get("windows") or []) + list(doc["windows"])
    return out


def validate(doc: Any) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(doc, dict):
        return ["the project file must be a mapping"], []
    for k in doc:
        if k not in TOP_KEYS:
            warnings.append("unknown key: %s" % k)
    if not isinstance(doc.get("desktops", []) or [], list):
        errors.append("desktops: expected a list")
        return errors, warnings
    if doc.get("env") is not None and not isinstance(doc["env"], dict):
        errors.append("env: expected a mapping")
    if not (doc.get("desktops") or doc.get("windows")):
        errors.append("nothing to do: add desktops: (or windows:)")
    from .layout import ENGINE_NAMES
    names = set()
    for i, d in enumerate(doc.get("desktops") or []):
        where = "desktops[%d]" % i
        if isinstance(d, str):
            d = {"name": d}
        if not isinstance(d, dict):
            errors.append("%s: expected a name or a mapping" % where)
            continue
        if not d.get("name"):
            errors.append("%s: missing name" % where)
        elif d["name"] in names:
            errors.append("%s: duplicate desktop %r" % (where, d["name"]))
        else:
            names.add(d["name"])
        if d.get("layout") is not None and d["layout"] not in ENGINE_NAMES:
            errors.append("%s: unknown layout %r" % (where, d["layout"]))
        wins = d.get("windows") or []
        if not isinstance(wins, list):
            errors.append("%s.windows: expected a list" % where)
            continue
        for j, w in enumerate(wins):
            errors += _check_window(w, "%s.windows[%d]" % (where, j), warnings)
    if doc.get("layout") is not None and doc["layout"] not in ENGINE_NAMES:
        errors.append("layout: unknown layout %r" % doc["layout"])
    if doc.get("windows") is not None and not isinstance(doc["windows"], list):
        errors.append("windows: expected a list")
    elif isinstance(doc.get("windows"), list):
        for j, w in enumerate(doc["windows"]):
            errors += _check_window(w, "windows[%d]" % j, warnings)
    seen_names = set()
    for d in _desktop_list(doc) if not errors else []:
        for w in d.get("windows") or []:
            n = w.get("name") if isinstance(w, dict) else None
            if n is not None:
                if str(n) in seen_names:
                    errors.append("duplicate window name %r (names identify windows, so they must be unique)" % n)
                seen_names.add(str(n))
    if doc.get("focus") and not any(d.get("name") == doc["focus"] for d in _desktop_list(doc)):
        errors.append("focus: no desktop named %r" % doc["focus"])
    return errors, warnings


def _check_window(w: Any, where: str, warnings: List[str]) -> List[str]:
    if not isinstance(w, dict):
        return ["%s: expected a mapping" % where]
    for k in w:
        if k not in WINDOW_KEYS:
            warnings.append("%s: unknown key %r" % (where, k))
    if not (w.get("cmd") or w.get("path") or w.get("text") or w.get("kind") or w.get("pipe")):
        return []            # an empty spec opens the default shell
    return []


def session_name(doc: dict, path: str) -> str:
    """The session for a project: ``name:``, else the directory name (sanitised)."""
    raw = doc.get("name") or os.path.basename(os.path.dirname(os.path.abspath(path))) or "project"
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(raw)).strip("-.") or "project"


def _window_spec(w: dict, base: str, env: dict) -> dict:
    spec = dict(w)
    cwd = spec.get("cwd")
    spec["cwd"] = os.path.normpath(os.path.join(base, os.path.expanduser(cwd))) if cwd else base
    if spec.get("path") and not os.path.isabs(os.path.expanduser(str(spec["path"]))):
        spec["path"] = os.path.normpath(os.path.join(base, os.path.expanduser(str(spec["path"]))))
    if env:
        merged = dict(env)
        merged.update(spec.get("env") or {})
        spec["env"] = merged
    return spec


def apply(wm, doc: dict, base: str, fresh: bool = False) -> Dict[str, List[str]]:
    """Create the desktops and windows of ``doc`` in ``wm`` (call with the WM lock held).  Idempotent.

    ``fresh`` says the session was just started for this project, so its untouched default shell window is replaced
    by the project's windows instead of being left next to them."""
    errors, _ = validate(doc)
    if errors:
        raise CommandError("; ".join(errors))
    env = {str(k): str(v) for k, v in (doc.get("env") or {}).items()}
    created: List[str] = []
    existing: List[str] = []
    made_windows = 0
    touched: List[int] = []
    first_index: Optional[int] = None
    focus_index: Optional[int] = None
    existing_names = {w.name for w in wm.windows.values() if getattr(w, "name", None)}
    default_wid = None
    if fresh and len(wm.windows) == 1:
        only = next(iter(wm.windows.values()))
        if not getattr(only, "spec", None):
            default_wid = only.id
    for d in _desktop_list(doc):
        name = str(d["name"])
        idx = next((i for i, x in enumerate(wm.desktops) if x.name == name), None)
        if idx is None:
            if len(wm.desktops) == 1 and set(wm.desktops[0].windows) <= {default_wid} \
                    and not getattr(wm, "_project_named", False):
                wm.desktops[0].name = name           # reuse the empty initial desktop instead of leaving it stray
                wm._project_named = True
                idx = 0
            else:
                wm.add_desktop(name, d.get("layout") or doc.get("layout"))
                idx = len(wm.desktops) - 1
            layout = d.get("layout") or doc.get("layout")
            if layout:
                wm.desktops[idx].layout = layout
            created.append("desktop " + name)
        else:
            existing.append("desktop " + name)
        first_index = idx if first_index is None else first_index
        if doc.get("focus") == name:
            focus_index = idx
        for w in d.get("windows") or []:
            wname = w.get("name")
            if wname and str(wname) in existing_names:
                existing.append("window " + str(wname))
                continue
            spec = _window_spec(w, base, env)
            spec["desktop"] = idx + 1                # desktops are numbered from 1
            spec.setdefault("focus", False)
            win = wm.create_window(spec)
            if wname:
                existing_names.add(str(wname))
            created.append("window %s" % (wname or win.id))
            made_windows += 1
            touched.append(idx)
    for idx in touched:                              # every desktop needs a focused window to receive keys
        d = wm.desktops[idx]
        if d.focus is None and d.windows:
            d.focus = d.windows[0]
    if default_wid is not None and made_windows and default_wid in wm.windows:
        wm.close_window(default_wid)
    target = focus_index if focus_index is not None else first_index
    if target is not None:
        wm.switch_desktop(wm.desktops[target])
        wm.compute_layout()
    return {"created": created, "existing": existing}
