"""Notifications: the ``notify`` command (also usable as a rule action).

A notification is shown in the status line, published as a ``notify`` event (the web UI turns it into a browser
notification) and, on terminals that understand OSC 777 (foot, WezTerm, VTE-based terminals, Ghostty ...), forwarded to
attached terminal clients so the desktop shows it.  ``notify.command`` in the configuration can run any program too::

    notify:
      osc: true                                   # forward as OSC 777 to attached terminals (default true)
      command: "notify-send {title} {body}"       # optional; {title} {body} {style} are shell-quoted for you
"""
from __future__ import annotations

import re
import shlex
import subprocess
from typing import List, Optional

from . import compat

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")          # C0, DEL and C1 controls
STYLES = ("normal", "ok", "warn", "err")


def clean(text: str, limit: int = 200) -> str:
    """One line, no control characters (they could break out of the escape sequence)."""
    return _CTRL.sub(" ", str(text)).strip()[:limit]


def osc777(title: str, body: str) -> str:
    """The escape sequence that asks a terminal for a desktop notification."""
    return "\x1b]777;notify;%s;%s\x07" % (clean(title, 80).replace(";", ","), clean(body).replace(";", ","))


def settings(wm) -> dict:
    cfg = wm.cfg.get("notify") if isinstance(wm.cfg.get("notify"), dict) else {}
    return {"osc": cfg.get("osc", True), "command": cfg.get("command")}


def send(wm, body: str, title: str = "pytermwm", style: str = "normal") -> str:
    body, title = clean(body), clean(title, 80) or "pytermwm"
    if style not in STYLES:
        style = "normal"
    cfg = settings(wm)
    wm.message("%s: %s" % (title, body) if title != "pytermwm" else body, style, 5.0)
    wm.emit("notify", title=title, body=body, style=style)
    if cfg["osc"]:
        wm.pending_terminal_output.append(osc777(title, body))
        del wm.pending_terminal_output[:-20]
    cmd = cfg["command"]
    if cmd:
        _run(wm, str(cmd), {"title": title, "body": body, "style": style})
    return body


def _run(wm, template: str, values: dict):
    # one pass: a value that itself contains "{body}" must not be expanded again
    line = re.sub(r"\{(title|body|style)\}", lambda m: compat.quote_shell_arg(values[m.group(1)]), template)
    try:
        subprocess.Popen(line, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **compat.new_session_kwargs())
    except OSError as e:
        wm.log.warning("notify command failed: %s", e)


def validate(cfg) -> List[str]:
    if cfg is None:
        return []
    if not isinstance(cfg, dict):
        return ["notify: expected a mapping (osc, command)"]
    errs = []
    for k in cfg:
        if k not in ("osc", "command"):
            errs.append("notify: unknown key %r" % k)
    if "osc" in cfg and not isinstance(cfg["osc"], bool):
        errs.append("notify.osc: expected true/false")
    if cfg.get("command") is not None and not isinstance(cfg["command"], str):
        errs.append("notify.command: expected a string")
    return errs
