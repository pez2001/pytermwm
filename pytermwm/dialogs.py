"""Modal and non-modal dialogs (message, confirm, input, menu)."""
from __future__ import annotations

import textwrap
from typing import Callable, List, Optional, Sequence, Tuple, Union

from .ansi import str_width, cells_from_text
from .colors import BOLD, REVERSE
from .draw import Canvas, truncate, pad, fuzzy_score
from .prompt import LineEditor

Action = Union[None, str, Callable]


class Dialog:
    kind = "message"
    autofocus = False        # non-modal dialogs that take keys as soon as they open (inputs, menus)

    def __init__(self, wm, title: str, modal: bool = True, x: Optional[int] = None, y: Optional[int] = None,
                 width: Optional[int] = None):
        self.wm = wm
        self.id = 0
        self.title = title
        self.modal = modal
        self.x, self.y = x, y
        self.width = width
        self.result = None
        self.closed = False
        self.rect = None      # (x, y, w, h) set by compositor for hit-testing

    # -- lifecycle
    def finish(self, result, action: Action = None):
        self.result = result
        self.closed = True
        self.wm.close_dialog(self)
        self.wm.emit("dialog_result", dialog=self, result=result)
        if callable(action):
            action(result)
        elif isinstance(action, str) and action:
            self.wm.run_command_line(action.replace("{}", str(result) if result is not None else ""), source="dialog")

    def handle_key(self, key: str) -> bool:
        if key in ("Esc", "C-c"):
            self.finish(None)
            return True
        return self.modal

    def handle_paste(self, text: str):
        pass

    def body_lines(self, max_w: int) -> List[str]:
        return []

    def buttons(self) -> List[str]:
        return []

    def describe(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "modal": self.modal, "result": self.result}

    # -- rendering
    def render(self, cols: int, rows: int, focused: bool = True) -> List[list]:
        th = self.wm.theme
        max_w = max(20, min(cols - 4, self.width or int(cols * 0.7)))
        body = self.body_lines(max_w - 4)
        want = max([str_width(l) for l in body] + [str_width(self.title) + 4, self._buttons_width() + 2, 24])
        inner_w = min(max_w - 2, max(want + 2, self.width or 0))
        w = inner_w + 2
        extra = self.extra_lines(inner_w)
        h = min(rows - 2, len(body) + len(extra) + 2 + (2 if self.buttons() else 0) + 1)
        fg, bg = th.c("dialog_fg"), th.c("dialog_bg")
        bset = th.border_set(dialog=True)
        bfg = th.c("dialog_border_fg")
        cv = Canvas(w, h, fg, bg)
        # frame
        cv.put(0, 0, bset["tl"] + bset["top"] * (w - 2) + bset["tr"], bfg, bg)
        cv.put(0, h - 1, bset["bl"] + bset["bottom"] * (w - 2) + bset["br"], bfg, bg)
        for yy in range(1, h - 1):
            cv.put(0, yy, bset["left"], bfg, bg)
            cv.put(w - 1, yy, bset["right"], bfg, bg)
        t = " " + truncate(self.title, w - 6) + " "
        tx = max(1, (w - str_width(t)) // 2)
        cv.put(tx, 0, t, th.c("dialog_title_fg"), th.c("dialog_title_bg"), BOLD)
        y = 1
        for line in body[:h - 3]:
            cv.put(2, y, line, fg, bg, max_w=w - 4)
            y += 1
        for line in extra:
            cv.put(1, y, line[0], line[1] or fg, line[2] or bg, line[3] if len(line) > 3 else 0, max_w=w - 2)
            y += 1
        btns = self.buttons()
        if btns:
            self._draw_buttons(cv, h - 2, w, btns)
        return cv.cells

    def extra_lines(self, width: int):
        return []

    def _buttons_width(self):
        return sum(str_width(b) + 6 for b in self.buttons())

    def sel_button(self) -> int:
        return getattr(self, "sel", 0)

    def _draw_buttons(self, cv: Canvas, y: int, w: int, btns: Sequence[str]):
        th = self.wm.theme
        total = sum(str_width(b) + 4 for b in btns) + (len(btns) - 1) * 2
        x = max(1, (w - total) // 2)
        for i, b in enumerate(btns):
            label = "[ %s ]" % b if not (th.name == "amiga") else " %s " % b
            if i == self.sel_button():
                cv.put(x, y, label, th.c("dialog_sel_fg"), th.c("dialog_sel_bg"), BOLD)
            else:
                cv.put(x, y, label, th.c("dialog_button_fg"), th.c("dialog_button_bg"))
            x += str_width(label) + 2


class MessageDialog(Dialog):
    kind = "message"

    def __init__(self, wm, title, text, buttons=("OK",), actions: Optional[Sequence[Action]] = None, **kw):
        super().__init__(wm, title, **kw)
        self.text = text
        self._buttons = list(buttons)
        self.actions = list(actions) if actions else [None] * len(self._buttons)
        self.sel = 0

    def buttons(self):
        return self._buttons

    def body_lines(self, max_w):
        out = []
        for para in str(self.text).split("\n"):
            out.extend(textwrap.wrap(para, max_w) or [""])
        return out

    def handle_key(self, key):
        n = len(self._buttons)
        if key in ("Left", "S-Tab", "h") and n:
            self.sel = (self.sel - 1) % n
        elif key in ("Right", "Tab", "l") and n:
            self.sel = (self.sel + 1) % n
        elif key in ("Enter", "Space"):
            self.finish(self._buttons[self.sel] if n else True, self.actions[self.sel] if n else None)
        elif key in ("Esc", "C-c"):
            self.finish(None)
        else:
            for i, b in enumerate(self._buttons):
                if len(key) == 1 and b and b[0].lower() == key.lower():
                    self.sel = i
                    self.finish(b, self.actions[i])
                    break
            else:
                return self.modal
        self.wm.dirty = True
        return True


class ConfirmDialog(MessageDialog):
    kind = "confirm"

    def __init__(self, wm, title, text, yes: Action = None, no: Action = None, **kw):
        super().__init__(wm, title, text, buttons=("Yes", "No"), actions=[yes, no], **kw)
        self.sel = 1 if kw.get("default_no") else 0


class InputDialog(Dialog):
    autofocus = True
    kind = "input"

    def __init__(self, wm, title, prompt, default="", action: Action = None, **kw):
        super().__init__(wm, title, **kw)
        self.prompt = prompt
        self.editor = LineEditor(default)
        self.action = action
        self.sel = 0

    def body_lines(self, max_w):
        return textwrap.wrap(self.prompt, max_w) or [""]

    def buttons(self):
        return ["OK", "Cancel"]

    def extra_lines(self, width):
        th = self.wm.theme
        text = self.editor.text
        shown = pad(text, width - 2)
        return [(" " + truncate(shown, width - 2) + " ", th.c("dialog_sel_fg"), th.c("dialog_button_bg"))]

    def handle_key(self, key):
        r = self.editor.handle(key)
        if r == "submit":
            self.finish(self.editor.text, self.action)
        elif r == "cancel":
            self.finish(None)
        self.wm.dirty = True
        return True

    def handle_paste(self, text):
        self.editor.insert(text.replace("\n", " "))
        self.wm.dirty = True


class MenuDialog(Dialog):
    autofocus = True
    kind = "menu"

    def __init__(self, wm, title, items: Sequence[Tuple[str, Action]], filterable=True, **kw):
        super().__init__(wm, title, **kw)
        self.items = list(items)
        self.filter = LineEditor("")
        self.filterable = filterable
        self.sel = 0
        self.view = list(self.items)

    def _refilter(self):
        q = self.filter.text
        if not q:
            self.view = list(self.items)
        else:
            sc = []
            for it in self.items:
                s = fuzzy_score(q, it[0])
                if s is not None:
                    sc.append((s, it))
            sc.sort(key=lambda t: -t[0])
            self.view = [it for _s, it in sc]
        self.sel = min(self.sel, max(0, len(self.view) - 1))

    def body_lines(self, max_w):
        return []

    def buttons(self):
        return []

    def extra_lines(self, width):
        th = self.wm.theme
        lines = []
        if self.filterable:
            lines.append(("/" + pad(self.filter.text, width - 2), th.c("dialog_fg"), th.c("dialog_button_bg")))
        top = max(0, self.sel - 9)
        for i, (label, _a) in enumerate(self.view[top:top + 10], start=top):
            txt = " " + pad(truncate(label, width - 2), width - 2)
            if i == self.sel:
                lines.append((txt, th.c("dialog_sel_fg"), th.c("dialog_sel_bg"), BOLD))
            else:
                lines.append((txt, None, None))
        if not self.view:
            lines.append((" (no matches)", th.c("status_dim_fg"), None))
        return lines

    def render(self, cols, rows, focused=True):
        self.width = max(self.width or 0, min(cols - 4, 50, max([str_width(l) + 6 for l, _ in self.items] + [30])))
        return super().render(cols, rows, focused)

    def handle_key(self, key):
        if key in ("Up", "C-p"):
            self.sel = max(0, self.sel - 1)
        elif key in ("Down", "C-n"):
            self.sel = min(len(self.view) - 1, self.sel + 1)
        elif key == "PageUp":
            self.sel = max(0, self.sel - 10)
        elif key == "PageDown":
            self.sel = min(len(self.view) - 1, self.sel + 10)
        elif key == "Enter":
            if self.view:
                label, act = self.view[self.sel]
                self.finish(label, act)
        elif key in ("Esc", "C-c"):
            self.finish(None)
        elif self.filterable:
            r = self.filter.handle(key)
            if r in ("changed", "moved"):
                self._refilter()
            elif r == "cancel":
                self.finish(None)
        self.wm.dirty = True
        return True

    def handle_paste(self, text):
        self.filter.insert(text.replace("\n", " "))
        self._refilter()


class DialogManager:
    """Keeps the dialog stack for a WindowManager."""

    def __init__(self, wm):
        self.wm = wm
        self.stack: List[Dialog] = []
        self._next = 1
        self.focus: Optional[int] = None    # id of a focused non-modal dialog

    def add(self, d: Dialog) -> Dialog:
        d.id = self._next
        self._next += 1
        self.stack.append(d)
        if not d.modal and d.autofocus:
            self.focus = d.id
        self.wm.dirty = True
        self.wm.emit("dialog_open", dialog=d)
        return d

    def remove(self, d: Dialog):
        if d in self.stack:
            self.stack.remove(d)
        if self.focus == d.id:
            self.focus = None
        self.wm.dirty = True

    def modal(self) -> Optional[Dialog]:
        for d in reversed(self.stack):
            if d.modal:
                return d
        return None

    def focused(self) -> Optional[Dialog]:
        if self.focus is not None:
            for d in self.stack:
                if d.id == self.focus:
                    return d
        return None

    def toggle_focus(self):
        nm = [d for d in self.stack if not d.modal]
        if not nm:
            return
        if self.focus is None:
            self.focus = nm[-1].id
        else:
            self.focus = None
        self.wm.dirty = True

    def get(self, did: int) -> Optional[Dialog]:
        for d in self.stack:
            if d.id == did:
                return d
        return None
