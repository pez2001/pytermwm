"""Nested pytermwm: a pytermwm running inside a window of another one (locally, or after ``ssh`` in a window).

Environment variables do not survive ``ssh``, so the levels talk to each other in-band, through the terminal:

* the inner server asks its terminal ``OSC 7790 ; ?``; a pytermwm window answers ``OSC 7790 ; depth=N ; session=S``
  (N: the depth of the asker, 1 for the first level inside another pytermwm) and remembers that the program in that
  window is a nested pytermwm; any other terminal ignores the unknown OSC;
* **passthrough**: one explicit key (``F12`` by default) makes the outer level hand every key and mouse event to the
  focused window, the inner pytermwm, until the same key is pressed again;
* **clipboard**: the inner level's OSC 52 copy travels up through every level to the real terminal (the outer level
  takes it quietly, and only the outermost level pipes it into ``selection.command``); pasting at an inner level asks
  the level above with an OSC 52 query, which a nested pytermwm is always allowed to answer.  The outermost level asks
  the real terminal the same way (``selection.terminal_clipboard``), so a paste brings in what was copied in other
  programs; a terminal that never answers is not asked again until a terminal attaches anew.
"""
from __future__ import annotations

import base64
import re
import time
from typing import List, Optional, Tuple

from .commands import CommandError
from .keys import key_to_bytes

OSC_NEST = "7790"
NEST_QUERY = "\x1b]%s;?\x07" % OSC_NEST
CLIPBOARD_QUERY = "\x1b]52;c;?\x07"

DEFAULT_NESTING = {
    "passthrough_key": None,        # the toggle key; default F12 at the outermost level, S-F12, C-F12, M-F12 further in
    "pull_clipboard": True,         # nested: paste asks the level above for its paste buffer first
    "nested_can_read": True,        # a nested pytermwm in a window may read the paste buffer (OSC 52 query)
    "command_when_nested": False,   # run selection.command also when this pytermwm is itself nested
}
LEVEL_KEYS = ("F12", "S-F12", "C-F12", "M-F12")
PULL_TIMEOUT = 0.6                  # seconds to wait for the level above to answer a clipboard query

_START = re.compile(rb"\x1b\](?:%s|52);" % OSC_NEST.encode())
_END = re.compile(rb"\x07|\x1b\\")
MAX_HELD = (1 << 20) + 4096


class ReplyFilter:
    """Takes the answers to our OSC 7790 / OSC 52 queries out of the terminal input; everything else passes."""

    def __init__(self):
        self.held = b""

    def feed(self, data: bytes) -> Tuple[bytes, List[Tuple[str, str]]]:
        buf = self.held + data
        self.held = b""
        out = bytearray()
        replies: List[Tuple[str, str]] = []
        while buf:
            m = _START.search(buf)
            if m is None:
                out += buf
                break
            out += buf[:m.start()]
            e = _END.search(buf, m.end())
            if e is None:
                if len(buf) - m.start() > MAX_HELD:      # never ends: it was not a reply
                    out += buf[m.start():]
                else:
                    self.held = buf[m.start():]
                break
            body = buf[m.start() + 2:e.start()].decode("utf-8", "replace")
            cmd, _, arg = body.partition(";")
            replies.append((cmd, arg))
            buf = buf[e.end():]
        return bytes(out), replies

    def pending(self) -> bool:
        return bool(self.held)


class NestingMixin:
    def _init_nesting(self):
        self.nest_depth = 0                 # 0: the outermost pytermwm; N: N levels inside other ones
        self.nest_hint = 0                  # what the environment of the attaching client says
        self.passthrough = False
        self._pull: Optional[float] = None  # deadline of a clipboard query to the level above
        self._pull_terminal = False         # ... that query went to the real terminal (outermost level)
        self.terminal_clipboard_ok: Optional[bool] = None   # does the real terminal answer clipboard queries?

    def nesting_cfg(self) -> dict:
        c = dict(DEFAULT_NESTING)
        c.update(self.cfg.get("nesting") or {})
        return c

    # ------------------------------------------------------------------ depth
    def set_nest_hint(self, hint) -> None:
        try:
            self.nest_hint = max(0, int(hint or 0))
        except (TypeError, ValueError):
            self.nest_hint = 0
        self.nest_depth = self.nest_hint
        self.terminal_clipboard_ok = None   # a (re)attached terminal may be a different one: ask it again
        self.dirty = True

    def nested_query(self, w) -> str:
        """A nested pytermwm in window ``w`` asks who is outside: answer with its depth."""
        w.nested_client = True
        return "depth=%d;session=%s" % (self.nest_depth + 1, str(self.session_name or "").replace(";", "_"))

    def nesting_reply(self, cmd: str, arg: str) -> None:
        """An answer from the terminal above (already taken out of the input stream)."""
        if cmd == OSC_NEST:
            fields = dict(p.partition("=")[::2] for p in arg.split(";") if "=" in p)
            try:
                self.nest_depth = max(self.nest_hint, int(fields.get("depth", 0)))
            except ValueError:
                return
            self.dirty = True
        elif cmd == "52":
            if self._pull is None:
                return
            self._pull = None
            if self._pull_terminal:
                self.terminal_clipboard_ok = True
            _, _, data = arg.partition(";")
            try:
                text = base64.b64decode(data, validate=True).decode("utf-8", "replace") if data and data != "?" else ""
            except (ValueError, TypeError):
                text = ""
            if text:
                self.paste_buffer = text
                self.paste_history = [text] + [t for t in self.paste_history if t != text][:19]
            self.paste_text(None, pull=False)

    # ------------------------------------------------------------------ clipboard
    def request_outer_clipboard(self) -> bool:
        """Ask the level above (or, outermost, the real terminal) for its clipboard; the answer pastes (see
        ``nesting_reply``).  False: not applicable - paste the own buffer right away."""
        if self.nest_depth > 0:
            if not self.nesting_cfg().get("pull_clipboard", True):
                return False
            self._pull_terminal = False
        else:
            mode = self.sel_cfg().get("terminal_clipboard", "auto")
            if mode is False or (mode == "auto" and self.terminal_clipboard_ok is False):
                return False
            if not self.terminal_attached():          # web UI only, scripts, tests: nobody could answer
                return False
            self._pull_terminal = True
        self._pull = time.time() + PULL_TIMEOUT
        self.pending_terminal_output.append(CLIPBOARD_QUERY)
        del self.pending_terminal_output[:-5]
        self.dirty = True
        return True

    def terminal_attached(self) -> bool:
        """Is a real terminal attached that a query could reach?"""
        server = getattr(self, "server", None)
        return any(getattr(c, "hello", None) and getattr(c, "writer", None) for c in getattr(server, "clients", None) or ())

    def nesting_tick(self, now: float) -> None:
        if self._pull is not None and now >= self._pull:
            self._pull = None
            if self._pull_terminal and self.terminal_clipboard_ok is None:
                self.terminal_clipboard_ok = False  # this terminal does not answer: stop making every paste wait
            self.paste_text(None, pull=False)       # the level above did not answer: use what we have
        if self.passthrough:
            w = self.focused
            if w is None or w.source is None or w.exited:
                self.set_passthrough(False)

    # ------------------------------------------------------------------ passthrough
    def passthrough_key(self) -> Optional[str]:
        k = self.nesting_cfg().get("passthrough_key")
        if k is None:
            return LEVEL_KEYS[self.nest_depth % len(LEVEL_KEYS)]
        k = str(k)
        return None if k.lower() in ("", "none", "off") else k

    def set_passthrough(self, on: bool) -> None:
        if on == self.passthrough:
            return
        if on:
            w = self.focused
            if w is None or w.source is None or w.exited:
                raise CommandError("passthrough needs a focused window with a running program")
        self.passthrough = on
        self.keymap.reset()
        if on:
            self.message("passthrough: keys go to window %d (%s ends it)" % (self.focused.id, self.passthrough_key() or "passthrough off"),
                         "normal", 3.0)
        else:
            self.message("passthrough off", "normal", 2.0)
        self.dirty = True

    def passthrough_key_event(self, name: str, raw: bytes) -> bool:
        """The toggle key, and in passthrough every other key too.  True: the key was handled."""
        if name == self.passthrough_key():
            self.set_passthrough(not self.passthrough)
            return True
        if not self.passthrough:
            return False
        w = self.focused
        if w is None or w.source is None or w.exited:
            self.set_passthrough(False)
            return False
        if not raw:
            raw = key_to_bytes(name, w.screen.app_cursor) or b""
        elif w.screen.app_cursor and raw.startswith(b"\x1b[") and len(raw) == 3 and raw[2:3] in b"ABCDHF":
            raw = b"\x1bO" + raw[2:3]
        if raw:
            w.write_input(raw)
        return True

    def passthrough_mouse(self, m: dict) -> None:
        """Mouse events in passthrough: to the focused window when it asked for them, nothing else."""
        w = self.focused
        r = self.desk.rects.get(w.id) if w else None
        if w is None or r is None:
            return
        inner = self.inner_rect(w, r)
        if inner.contains(m["x"], m["y"]):
            self._forward_mouse(w, inner, m)
