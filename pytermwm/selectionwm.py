"""Window-manager side of text selection: mouse, copy mode, paste buffer and the terminal-clipboard fallback view."""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional

from . import compat
from .commands import CommandError
from .window import TextWindow
from .selection import (DEFAULT_WORD_CHARS, Selection, base_id, cell_to_pos, clamp_pos, index_of, line_at, line_count, osc52,
                        view_first_id, word_span)

DEFAULT_SELECTION = {
    "copy_on_release": True,               # PuTTY behaviour: releasing the mouse copies
    "modifier": "alt",                     # held while dragging to select in a window whose app uses the mouse
    "paste_buttons": ["middle", "right"],
    "osc52": True,                         # also send the copy to the terminal's clipboard (OSC 52)
    "command": None,                       # optional: pipe the copy into a program, e.g. "clip" / "wl-copy" / "xclip -sel c"
    "word_chars": DEFAULT_WORD_CHARS,
}
MODS = {"alt": "M-", "shift": "S-", "ctrl": "C-"}
BUTTONS = {"left": 1, "middle": 2, "right": 3}
DOUBLE_CLICK = 0.45
PASTE_HISTORY = 20


class SelectionMixin:
    # ------------------------------------------------------------------ state
    def _init_selection(self):
        self.selection: Optional[Selection] = None
        self.paste_buffer = ""
        self.paste_history = []
        self.copy_view: Optional[dict] = None
        self.sel_drag: Optional[dict] = None
        self._click = None

    def sel_cfg(self) -> dict:
        c = dict(DEFAULT_SELECTION)
        c.update(self.cfg.get("selection") or {})
        return c

    def active_selection(self) -> Optional[Selection]:
        s = self.selection
        if s is not None and not s.valid(self):
            self.selection = None
            self.sel_drag = None
            if self.keymap.mode == "copy":
                self.keymap.mode = "normal"
            return None
        return s

    def clear_selection(self):
        if self.selection is not None:
            self.selection = None
            self.sel_drag = None
            self.dirty = True

    def mouse_effective(self) -> bool:
        return bool(self.cfg.get("mouse", True)) and self.copy_view is None

    # ------------------------------------------------------------------ copy / paste buffer
    def copy_text(self, text: str, quiet: bool = False) -> int:
        """Put ``text`` in the paste buffer and on the clipboards we can reach.  Returns the number of characters."""
        if not text:
            return 0
        self.paste_buffer = text
        self.paste_history = [text] + [t for t in self.paste_history if t != text][:PASTE_HISTORY - 1]
        cfg = self.sel_cfg()
        note = ""
        if cfg.get("osc52", True):
            seq = osc52(text)
            if seq is None:
                note = " (too large for the terminal clipboard)"
            else:
                self.pending_terminal_output.append(seq)
                del self.pending_terminal_output[:-5]
        if cfg.get("command"):
            self._pipe_to_command(str(cfg["command"]), text)
        self.emit("clipboard", length=len(text))
        if not quiet:
            self.message("copied %d characters%s" % (len(text), note), "ok", 2.5)
        self.dirty = True
        return len(text)

    def _pipe_to_command(self, cmd: str, text: str):
        try:
            p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **compat.new_session_kwargs())
        except OSError as e:
            self.log.warning("selection.command failed: %s", e)
            return

        def feed():
            try:
                p.stdin.write(text.encode("utf-8", "replace"))
                p.stdin.close()
                p.wait(timeout=10)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()

    def copy_selection(self) -> str:
        s = self.active_selection()
        if s is None:
            raise CommandError("nothing selected")
        w = self.windows[s.wid]
        text = s.text(w.screen)
        if not text.strip("\n "):
            raise CommandError("nothing selected")
        self.copy_text(text)
        return text

    def paste_text(self, text: Optional[str] = None):
        text = self.paste_buffer if text is None else text
        if not text:
            self.message("paste buffer is empty", "warn", 2.0)
            return
        self.handle_paste(text)
        self.dirty = True

    # ------------------------------------------------------------------ mouse
    def selectable(self, w, m: dict) -> bool:
        """Does a drag in ``w`` select text (as opposed to going to the program running in it)?"""
        if not (w.screen.mouse_mode and w.source):
            return True
        mod = str(self.sel_cfg().get("modifier") or "").lower()
        token = MODS.get(mod)
        return bool(token and token in (m.get("mods") or ""))

    def selection_press(self, m: dict, w, inner, in_inner: bool) -> bool:
        """Called for a mouse press.  Returns True when the selection code took it."""
        if not in_inner or not self.selectable(w, m):
            return False
        cfg = self.sel_cfg()
        button = m["button"]
        if button == 1:
            if self.desk.focus != w.id:
                self.focus_window(w.id)
            self._sel_press(w, inner, m, cfg)
            return True
        wanted = [BUTTONS.get(str(b).lower()) for b in (cfg.get("paste_buttons") or [])]
        if button in wanted:
            if self.desk.focus != w.id:
                self.focus_window(w.id)
            self.paste_text()
            return True
        return False

    def _sel_press(self, w, inner, m: dict, cfg: dict):
        now = time.time()
        x, y = m["x"], m["y"]
        c = self._click
        count = 1
        if c and c["wid"] == w.id and (c["x"], c["y"]) == (x, y) and now - c["t"] < DOUBLE_CLICK:
            count = c["n"] % 3 + 1
        self._click = {"t": now, "wid": w.id, "x": x, "y": y, "n": count}
        pos = cell_to_pos(w, x - inner.x, y - inner.y, inner.h)
        unit = ("char", "word", "line")[count - 1]
        sel = Selection(w.id, w.screen, pos, unit, rect="M-" in (m.get("mods") or ""), word_chars=str(cfg.get("word_chars") or DEFAULT_WORD_CHARS))
        sel.visible = count > 1                       # a plain click selects nothing until the mouse moves
        sel.dragging = True
        self.selection = sel
        self.sel_drag = {"wid": w.id, "auto": 0, "vx": x - inner.x, "count": count, "last_auto": now}
        self.dirty = True

    def selection_drag(self, m: dict):
        """Mouse move / release while a selection drag is in progress (works outside the window too)."""
        drag = self.sel_drag
        sel = self.active_selection()
        if drag is None or sel is None:
            self.sel_drag = None
            return
        w = self.windows[drag["wid"]]
        r = self.desk.rects.get(w.id)
        if r is None:
            self.sel_drag = None
            return
        inner = self.inner_rect(w, r)
        vx = max(0, min(inner.w - 1, m["x"] - inner.x))
        vy = m["y"] - inner.y
        drag["auto"] = -1 if vy >= inner.h else (1 if vy < 0 else 0)      # +1 scrolls back (up)
        drag["vx"] = vx
        vy = max(0, min(inner.h - 1, vy))
        if drag["auto"] and m["kind"] == "move":
            w.scroll(drag["auto"])
        sel.head = cell_to_pos(w, vx, vy, inner.h)
        if m["kind"] == "move":
            sel.visible = True
        self.dirty = True
        if m["kind"] == "release":
            self._sel_release(w, sel, drag)

    def _sel_release(self, w, sel: Selection, drag: dict):
        self.sel_drag = None
        sel.dragging = False
        if not sel.visible:
            self.selection = None
            return
        if self.sel_cfg().get("copy_on_release", True):
            text = sel.text(w.screen)
            if text.strip("\n "):
                self.copy_text(text)

    def selection_tick(self, now: float):
        """Keep scrolling while the button is held outside the window."""
        drag = self.sel_drag
        if not drag or not drag.get("auto") or now - drag["last_auto"] < 0.05:
            return
        sel = self.active_selection()
        w = self.windows.get(drag["wid"])
        r = self.desk.rects.get(drag["wid"]) if w else None
        if sel is None or w is None or r is None:
            return
        drag["last_auto"] = now
        inner = self.inner_rect(w, r)
        w.scroll(drag["auto"])
        vy = 0 if drag["auto"] > 0 else inner.h - 1
        sel.head = cell_to_pos(w, drag["vx"], vy, inner.h)
        self.dirty = True

    # ------------------------------------------------------------------ keyboard copy mode
    def copy_mode_enter(self, w=None):
        w = w or self.resolve_window(None)
        scr = w.screen
        vh = max(1, w.viewport[1])
        if w.view_offset() == 0 and scr.cursor_visible:
            pos = (base_id(scr) + len(scr.history) + scr.y, scr.x)
        else:
            pos = (view_first_id(w, vh), 0)
        pos = clamp_pos(scr, pos)
        sel = Selection(w.id, scr, pos)
        sel.copy_mode = True
        sel.selecting = False
        self.selection = sel
        self.sel_drag = None
        self.keymap.mode = "copy"
        self.dirty = True

    def _copy_sel(self) -> Selection:
        s = self.active_selection()
        if s is None or not s.copy_mode:
            raise CommandError("not in copy mode (copy-mode)")
        return s

    def copy_move(self, what: str, count: int = 1):
        s = self._copy_sel()
        w = self.windows[s.wid]
        scr = w.screen
        lid, col = s.head
        vh = max(1, w.viewport[1])
        lo = base_id(scr)
        hi = lo + line_count(scr) - 1
        n = max(1, count)
        if what == "left":
            col -= n
        elif what == "right":
            col += n
        elif what == "up":
            lid -= n
        elif what == "down":
            lid += n
        elif what == "page-up":
            lid -= max(1, vh - 1) * n
        elif what == "page-down":
            lid += max(1, vh - 1) * n
        elif what == "top":
            lid = lo
        elif what == "bottom":
            lid = hi
        elif what == "home":
            col = 0
        elif what == "end":
            line = line_at(scr, lid)
            col = max([i for i, c in enumerate(line or []) if c[0].strip()] or [0])
        elif what == "first":
            line = line_at(scr, lid)
            col = next((i for i, c in enumerate(line or []) if c[0].strip()), 0)
        elif what in ("word-next", "word-prev"):
            for _ in range(n):
                lid, col = self._word_step(scr, lid, col, forward=(what == "word-next"))
        else:
            raise CommandError("copy-move: unknown movement %r" % what)
        s.head = clamp_pos(scr, (lid, col))
        self._reveal(w, s.head, vh)
        self.dirty = True

    def _word_step(self, scr, lid, col, forward):
        wc = self.sel_cfg().get("word_chars") or DEFAULT_WORD_CHARS
        line = line_at(scr, lid) or []
        a, b = word_span(scr, (lid, col), wc)
        if forward:
            col = b[1] + 1
            while True:
                line = line_at(scr, lid) or []
                while col < len(line) and not line[col][0].strip():
                    col += 1
                if col < len(line):
                    return lid, col
                if lid >= base_id(scr) + line_count(scr) - 1:
                    return lid, max(0, len(line) - 1)
                lid, col = lid + 1, 0
        if col > a[1]:
            return lid, a[1]                        # inside a word: go to its start first
        col = a[1] - 1
        while True:
            line = line_at(scr, lid) or []
            while col >= 0 and not line[col][0].strip():
                col -= 1
            if col >= 0:
                return (lid, word_span(scr, (lid, col), wc)[0][1])
            if lid <= base_id(scr):
                return lid, 0
            lid -= 1
            col = scr.cols - 1

    def _reveal(self, w, head, vh):
        """Scroll the window so the copy-mode cursor row is visible."""
        scr = w.screen
        idx = index_of(scr, head[0])
        if idx is None or isinstance(w, TextWindow):
            return
        total = line_count(scr)
        end = total - w.scroll_y
        start = end - vh
        if idx < start:
            w.scroll_y = max(0, total - (idx + vh))
        elif idx >= end:
            w.scroll_y = max(0, total - (idx + 1))
        w._clamp_scroll()

    def copy_select(self, unit: str = "char"):
        s = self._copy_sel()
        if unit not in ("char", "line", "rect"):
            raise CommandError("copy-select: char, line or rect")
        key = ("line", False) if unit == "line" else (("char", True) if unit == "rect" else ("char", False))
        if s.selecting and (s.unit, s.rect) == key:
            s.selecting = False                       # same key again: back to just moving the cursor
        else:
            s.selecting = True
            s.anchor = s.head
            s.unit, s.rect = key
            s._a_span = s._span(self.windows[s.wid].screen, s.anchor)
        s.visible = True
        self.dirty = True

    def copy_yank(self) -> str:
        s = self._copy_sel()
        w = self.windows[s.wid]
        if not s.selecting:
            raise CommandError("nothing selected (v starts a selection, y copies it)")
        text = s.text(w.screen)
        self.copy_cancel()
        if not text.strip("\n "):
            raise CommandError("nothing selected")
        self.copy_text(text)
        return text

    def copy_cancel(self):
        self.selection = None
        if self.keymap.mode == "copy":
            self.keymap.mode = "normal"
        self.dirty = True

    # ------------------------------------------------------------------ copy view (clipboard for terminals without OSC 52)
    def copy_view_enter(self, w=None):
        w = w or self.resolve_window(None)
        self.copy_view = {"wid": w.id}
        self.clear_selection()
        self.dirty = True
        self.message("copy view: drag with the mouse to copy", "normal", 3.0)

    def copy_view_exit(self):
        if self.copy_view is not None:
            self.copy_view = None
            self.dirty = True

    def copy_view_key(self, name: str):
        w = self.windows.get(self.copy_view["wid"]) if self.copy_view else None
        if w is None:
            self.copy_view_exit()
            return
        vh = max(1, w.viewport[1])
        moves = {"k": 1, "Up": 1, "j": -1, "Down": -1, "PageUp": vh - 1, "C-u": vh - 1, "PageDown": -(vh - 1), "C-d": -(vh - 1)}
        if name in moves:
            w.scroll(moves[name])
        elif name in ("g", "Home"):
            w.scroll_to_top()
        elif name in ("G", "End"):
            w.scroll_to_bottom()
        else:
            self.copy_view_exit()
        self.dirty = True

    # ------------------------------------------------------------------ description (API)
    def selection_info(self) -> dict:
        s = self.active_selection()
        text = ""
        if s is not None:
            text = s.text(self.windows[s.wid].screen)
        return {"selection": text, "window": s.wid if s is not None else None, "paste_buffer": self.paste_buffer,
                "history": len(self.paste_history), "copy_mode": self.keymap.mode == "copy"}
