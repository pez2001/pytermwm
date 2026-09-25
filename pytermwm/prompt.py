"""Line editor, command prompt (shares the status line) and command palette."""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from .draw import fuzzy_score


class LineEditor:
    def __init__(self, text: str = "", history: Optional[List[str]] = None):
        self.text = text
        self.pos = len(text)
        self.history: List[str] = history if history is not None else []
        self.hist_idx: Optional[int] = None
        self._draft = ""
        self.kill = ""

    def set(self, text: str):
        self.text = text
        self.pos = len(text)

    def insert(self, s: str):
        self.text = self.text[:self.pos] + s + self.text[self.pos:]
        self.pos += len(s)

    def _word_start(self) -> int:
        i = self.pos
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        return i

    def _word_end(self) -> int:
        i = self.pos
        n = len(self.text)
        while i < n and self.text[i].isspace():
            i += 1
        while i < n and not self.text[i].isspace():
            i += 1
        return i

    def history_prev(self):
        if not self.history:
            return
        if self.hist_idx is None:
            self._draft = self.text
            self.hist_idx = len(self.history)
        if self.hist_idx > 0:
            self.hist_idx -= 1
            self.set(self.history[self.hist_idx])

    def history_next(self):
        if self.hist_idx is None:
            return
        self.hist_idx += 1
        if self.hist_idx >= len(self.history):
            self.hist_idx = None
            self.set(self._draft)
        else:
            self.set(self.history[self.hist_idx])

    def handle(self, key: str) -> Optional[str]:
        """Returns 'submit', 'cancel', 'complete', 'changed' or None (ignored)."""
        if key == "Enter":
            return "submit"
        if key in ("Esc", "C-c", "C-g"):
            return "cancel"
        if key == "Tab":
            return "complete"
        if key == "S-Tab":
            return "complete-back"
        if key == "Backspace" or key == "C-h":
            if self.pos > 0:
                self.text = self.text[:self.pos - 1] + self.text[self.pos:]
                self.pos -= 1
            elif not self.text:
                return "cancel"
            return "changed"
        if key == "Delete":
            self.text = self.text[:self.pos] + self.text[self.pos + 1:]
            return "changed"
        if key == "C-d":
            if not self.text:
                return "cancel"
            self.text = self.text[:self.pos] + self.text[self.pos + 1:]
            return "changed"
        if key in ("Left", "C-b"):
            self.pos = max(0, self.pos - 1)
            return "moved"
        if key in ("Right", "C-f"):
            if self.pos >= len(self.text):
                return "accept-suggestion"
            self.pos += 1
            return "moved"
        if key in ("Home", "C-a"):
            self.pos = 0
            return "moved"
        if key in ("End", "C-e"):
            if self.pos >= len(self.text):
                return "accept-suggestion"
            self.pos = len(self.text)
            return "moved"
        if key in ("C-Left", "M-b"):
            self.pos = self._word_start()
            return "moved"
        if key in ("C-Right", "M-f"):
            self.pos = self._word_end()
            return "moved"
        if key == "C-w" or key == "M-Backspace":
            s = self._word_start()
            self.kill = self.text[s:self.pos]
            self.text = self.text[:s] + self.text[self.pos:]
            self.pos = s
            return "changed"
        if key == "M-d":
            e = self._word_end()
            self.kill = self.text[self.pos:e]
            self.text = self.text[:self.pos] + self.text[e:]
            return "changed"
        if key == "C-u":
            self.kill = self.text[:self.pos]
            self.text = self.text[self.pos:]
            self.pos = 0
            return "changed"
        if key == "C-k":
            self.kill = self.text[self.pos:]
            self.text = self.text[:self.pos]
            return "changed"
        if key == "C-y":
            self.insert(self.kill)
            return "changed"
        if key in ("Up", "C-p"):
            self.history_prev()
            return "changed"
        if key in ("Down", "C-n"):
            self.history_next()
            return "changed"
        if key == "Space":
            self.insert(" ")
            return "changed"
        if len(key) == 1 or (len(key) > 1 and not key.startswith(("C-", "M-", "S-")) and len(key.encode()) <= 4 and len(key) == 1):
            self.insert(key)
            return "changed"
        return None


class Prompt:
    """The command prompt: rendered in the status line while active."""

    def __init__(self, wm):
        self.wm = wm
        self.active = False
        self.editor = LineEditor()
        self.history: List[str] = []
        self.mode = "auto"           # auto | command | search | custom
        self.label = "$"
        self.on_submit: Optional[Callable[[str], None]] = None
        self.completions: List[str] = []
        self.comp_index = -1
        self.comp_start = 0
        self.comp_base = ""
        self.suggestion = ""
        self.hint = ""

    # ----------------------------------------------------------- persistence (enabled by the server)
    history_path: Optional[str] = None

    def enable_persistence(self, path: str):
        """Load the command history from ``path`` and keep it updated on every submit."""
        import json
        self.history_path = path
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.history[:] = [str(x) for x in data if isinstance(x, str)][-500:]
        except (OSError, ValueError):
            pass

    def save_history(self):
        if not self.history_path:
            return
        import json
        import os
        try:
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.history[-500:], f)
            os.replace(tmp, self.history_path)
        except OSError:
            pass

    # ----------------------------------------------------------- state
    def open(self, prefill: str = "", mode: str = "auto", label: Optional[str] = None,
             on_submit: Optional[Callable[[str], None]] = None):
        self.active = True
        self.mode = mode
        self.editor = LineEditor(prefill, self.history)
        self.on_submit = on_submit
        self.label = label or ("❯" if mode == "auto" else ":" if mode == "command" else "/" if mode == "search" else "?")
        self.completions = []
        self.comp_index = -1
        self._update_suggestion()
        self.wm.dirty = True
        self.wm.emit("prompt_open", prompt=self)

    def close(self):
        self.active = False
        self.completions = []
        self.comp_index = -1
        self.wm.dirty = True

    @property
    def effective_mode(self) -> str:
        if self.mode == "auto":
            return "command" if self.editor.text.startswith(":") else "shell"
        return self.mode

    @property
    def text(self) -> str:
        return self.editor.text

    # ----------------------------------------------------------- input
    def handle_key(self, key: str):
        ed = self.editor
        if key == "C-r":
            self._history_search()
            return
        res = ed.handle(key)
        if res == "submit":
            text = ed.text
            cb = self.on_submit
            mode = self.mode
            self.close()
            if text.strip():
                if not self.history or self.history[-1] != text:
                    self.history.append(text)
                del self.history[:-500]
                self.save_history()
            if cb:
                cb(text)
            else:
                self.wm.run_prompt_line(text, mode)
            return
        if res == "cancel":
            self.close()
            return
        if res in ("complete", "complete-back"):
            self._complete(back=(res == "complete-back"))
            return
        if res == "accept-suggestion":
            if self.suggestion:
                ed.set(ed.text + self.suggestion)
                self.suggestion = ""
            return
        if res in ("changed", "moved"):
            self.completions = []
            self.comp_index = -1
            self._update_suggestion()
            self.wm.dirty = True

    def handle_paste(self, text: str):
        self.editor.insert(text.replace("\n", " "))
        self._update_suggestion()
        self.wm.dirty = True

    def _history_search(self):
        q = self.editor.text
        for h in reversed(self.history):
            if q and q in h and h != q:
                self.editor.set(h)
                break
        self._update_suggestion()

    # ----------------------------------------------------------- completion
    def _complete(self, back: bool = False):
        from .completion import complete
        ed = self.editor
        if self.completions:
            step = -1 if back else 1
            self.comp_index = (self.comp_index + step) % len(self.completions)
            cand = self.completions[self.comp_index]
            ed.set(self.comp_base + cand)
            return
        start, cands, common = complete(self.wm, ed.text[:ed.pos], self.effective_mode)
        rest = ed.text[ed.pos:]
        if not cands:
            self.wm.message("no completions", ttl=1.0)
            return
        if len(cands) == 1:
            ed.set(ed.text[:start] + cands[0] + rest)
            ed.pos = start + len(cands[0])
            return
        self.comp_base = ed.text[:start]
        if common and len(common) > len(ed.text) - start:
            ed.set(ed.text[:start] + common + rest)
            ed.pos = start + len(common)
        self.completions = cands
        self.comp_index = -1
        self.wm.dirty = True

    def _update_suggestion(self):
        t = self.editor.text
        self.suggestion = ""
        if not t or self.editor.pos < len(t):
            return
        for h in reversed(self.history):
            if h.startswith(t) and h != t:
                self.suggestion = h[len(t):]
                break


class Palette:
    """Modern command window: fuzzy search overlay for commands / windows / desktops / themes."""

    def __init__(self, wm):
        self.wm = wm
        self.active = False
        self.editor = LineEditor()
        self.scope = "all"
        self.items: List[Tuple[str, str, str]] = []    # (label, detail, command)
        self.filtered: List[Tuple[str, str, str]] = []
        self.sel = 0
        self.max_rows = 12

    def open(self, scope: str = "all", prefill: str = ""):
        self.active = True
        self.scope = scope
        self.editor = LineEditor(prefill)
        self.items = self.wm.palette_items(scope)
        self.sel = 0
        self._filter()
        self.wm.dirty = True

    def close(self):
        self.active = False
        self.wm.dirty = True

    def _filter(self):
        q = self.editor.text.strip()
        scored = []
        cmd_word = q.split(" ", 1)[0] if q else ""
        for it in self.items:
            label, detail, cmd = it
            s = fuzzy_score(q, label)
            if s is None:
                s2 = fuzzy_score(q, detail)
                if s2 is not None:
                    s = s2 - 50
            if s is not None:
                scored.append((s, it))
        scored.sort(key=lambda t: -t[0])
        self.filtered = [it for _s, it in scored]
        # when the query already looks like "command args", offer arg completions first
        if q and " " in q:
            from .completion import complete_command_args
            for cand in complete_command_args(self.wm, q):
                self.filtered.insert(0, (cand, "", cand))
        self.sel = min(self.sel, max(0, len(self.filtered) - 1))

    def handle_key(self, key: str):
        if key in ("Up", "C-p", "S-Tab"):
            self.sel = max(0, self.sel - 1)
            self.wm.dirty = True
            return
        if key in ("Down", "C-n"):
            self.sel = min(len(self.filtered) - 1, self.sel + 1)
            self.wm.dirty = True
            return
        if key == "Tab":
            if self.filtered:
                self.editor.set(self.filtered[self.sel][2])
                self._filter()
                self.wm.dirty = True
            return
        if key == "Enter":
            q = self.editor.text.strip()
            if self.filtered and (not q or " " not in q or self.filtered[0][2] != q):
                cmd = self.filtered[self.sel][2]
            else:
                cmd = q
            self.close()
            if cmd:
                self.wm.run_command_line(cmd, source="palette")
            return
        if key in ("Esc", "C-c", "C-g"):
            self.close()
            return
        res = self.editor.handle(key)
        if res in ("changed", "moved"):
            self.sel = 0
            self._filter()
            self.wm.dirty = True
        elif res == "cancel":
            self.close()
