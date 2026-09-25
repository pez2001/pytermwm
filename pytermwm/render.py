"""Compositor (WindowManager state -> frame) and ANSI diff writer."""
from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from .ansi import Cell, char_width, str_width
from .colors import BOLD, DIM, REVERSE, WIDE, TAIL, blend, sgr
from .draw import Canvas, truncate
from .layout import Rect


class Frame:
    __slots__ = ("cells", "cursor", "title", "cols", "rows", "cursor_shape")

    def __init__(self, cells, cursor=None, title="", cursor_shape=0):
        self.cells = cells
        self.cursor = cursor
        self.title = title
        self.rows = len(cells)
        self.cols = len(cells[0]) if cells else 0
        self.cursor_shape = cursor_shape

    def text(self) -> str:
        return "\n".join("".join(c[0] for c in row if not c[3] & TAIL).rstrip() for row in self.cells)


class Compositor:
    def __init__(self, wm):
        self.wm = wm

    # -------------------------------------------------------------------- main
    def compose(self, cols: int, rows: int) -> Frame:
        wm = self.wm
        th = wm.theme
        if wm.copy_view is not None:
            view = self._compose_copy_view(cols, rows)
            if view is not None:
                return view
            wm.copy_view = None
        cv = Canvas(cols, rows, th.c("desktop_fg"), th.c("desktop_bg"), th.o("desktop_char"))
        now = time.time()
        if wm.background is not None:
            try:
                bgc = wm.background.render(cols, rows, now, th)
                cv.blit(bgc, 0, 0)
            except Exception as e:
                wm.log.error("background effect failed: %s", e)
                wm.background = None
        d = wm.desk
        cursor = None
        focus_cursor = None
        for wid in d.order:
            w = wm.windows.get(wid)
            r = d.rects.get(wid)
            if not w or r is None:
                continue
            focused = (wid == d.focus)
            cur = self.draw_window(cv, w, r, focused)
            if focused:
                focus_cursor = cur
        if not d.windows:
            msg = "no windows - press %s c (or M-Enter) to create one, %s : for the prompt" % (wm.keymap.prefix, wm.keymap.prefix)
            area = wm.content_area()
            cv.put(max(0, area.x + (area.w - str_width(msg)) // 2), area.y + area.h // 2, msg,
                   th.c("status_dim_fg"), th.c("desktop_bg"))
        cursor = focus_cursor
        title = ""
        f = wm.focused
        if f:
            title = f.title
        # dialogs
        dlg_cursor = None
        for dlg in wm.dialogs.stack:
            if dlg.modal:
                cv.shade(0, 0, cols, rows, dim=True)
            cells = dlg.render(cols, rows, dlg is wm.dialogs.modal() or dlg.id == wm.dialogs.focus)
            h = len(cells)
            w_ = len(cells[0]) if cells else 0
            x = dlg.x if dlg.x is not None else (cols - w_) // 2
            y = dlg.y if dlg.y is not None else max(0, (rows - h) // 2)
            dlg.rect = (x, y, w_, h)
            if th.o("shadow"):
                self._shadow(cv, Rect(x, y, w_, h))
            cv.blit(cells, x, y)
        # palette
        if wm.palette.active:
            dlg_cursor = self.draw_palette(cv)
        # status line
        row = wm.status_row()
        if row is not None:
            cells_row, cx = wm.statusline.render(cols)
            cv.blit([cells_row], 0, row)
            if cx is not None:
                dlg_cursor = (cx, row)
        if wm.prompt.active and wm.prompt.completions:
            self.draw_completions(cv, row if row is not None else rows - 1)
        if dlg_cursor is not None:
            cursor = dlg_cursor
        elif wm.dialogs.modal() or wm.keymap.mode in ("scroll", "copy"):
            cursor = None if wm.dialogs.modal() else cursor
        return Frame(cv.cells, cursor, title)

    def _compose_copy_view(self, cols: int, rows: int) -> Optional[Frame]:
        """One window's text, no borders or neighbours, at the top left: what a terminal's own mouse selection needs to copy
        clean text (used with terminals that cannot receive clipboard data from the program, e.g. PuTTY)."""
        wm = self.wm
        w = wm.windows.get(wm.copy_view["wid"])
        r = w.desktop.rects.get(w.id) if w is not None and w.desktop is not None else None
        if w is None or r is None:
            return None
        th = wm.theme
        cv = Canvas(cols, rows, th.c("desktop_fg"), th.c("desktop_bg"), th.o("desktop_char"))
        inner = wm.inner_rect(w, r)
        lines = self._content(w, Rect(0, 0, min(inner.w, cols), min(inner.h, max(1, rows - 1))), True)
        cv.blit(lines, 0, 0)
        hint = " COPY VIEW - drag with the mouse to copy (PuTTY copies on release) | j/k PgUp/PgDn scroll | any other key returns "
        cv.put(0, rows - 1, truncate(hint, cols), th.c("status_fg"), th.c("status_bg"))
        return Frame(cv.cells, None, w.title)

    # -------------------------------------------------------------------- windows
    def _content(self, w, inner: Rect, focused: bool):
        th = self.wm.theme
        if getattr(w, "dirty", False) and hasattr(w, "refresh"):
            w.refresh()          # internal windows: never show stale/blank content
        key = (w.screen.version, w.scroll_y, w.scroll_x, inner.w, inner.h, focused, id(th), w.opts["overflow"], getattr(w, "dirty", 0) and 0)
        cached = getattr(w, "_rc", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        lines = w.visible_lines(inner.w, inner.h)
        wfg, wbg = th.c("window_fg"), th.c("window_bg")
        dimit = th.o("dim_unfocused") and not focused
        if wfg is not None or wbg is not None or dimit:
            out = []
            for l in lines:
                out.append([(c[0], c[1] if c[1] is not None else wfg, c[2] if c[2] is not None else wbg,
                             (c[3] | DIM) if dimit else c[3]) for c in l])
            lines = out
        w._rc = (key, lines)
        return lines

    def draw_window(self, cv: Canvas, w, r: Rect, focused: bool) -> Optional[Tuple[int, int]]:
        wm = self.wm
        th = wm.theme
        inner = wm.inner_rect(w, r)
        if (w.floating or w.opts["shadow"]) and th.o("shadow") and w.opts["border"]:
            self._shadow(cv, r)
        lines = self._content(w, inner, focused)
        sel = wm.active_selection()
        if sel is not None and sel.wid == w.id:
            lines = sel.apply(w, lines, inner.w, inner.h)
        cv.blit(lines, inner.x, inner.y)
        if w.opts["border"] and r.w >= 2 and r.h >= 2:
            self.draw_border(cv, w, r, inner, focused)
        cur = None
        if focused and not (wm.prompt.active or wm.palette.active):
            p = w.cursor_position(inner.w, inner.h)
            if p is not None:
                cur = (inner.x + p[0], inner.y + p[1])
        return cur

    def _shadow(self, cv: Canvas, r: Rect):
        th = self.wm.theme
        sb = th.c("shadow_bg")
        cv.shade(r.x + 2, r.y2, r.w, 1, bg=sb, dim=True)
        cv.shade(r.x2, r.y + 1, 2, r.h, bg=sb, dim=True)

    def draw_border(self, cv: Canvas, w, r: Rect, inner: Rect, focused: bool):
        wm = self.wm
        th = wm.theme
        bs = th.border_set(focused)
        fg = th.c("focus_border_fg" if focused else "border_fg")
        bg = th.c("focus_border_bg" if focused else "border_bg")
        fl = BOLD if (focused and th.o("border_bold_focus")) else 0
        tfg = th.c("focus_title_fg" if focused else "title_fg")
        tbg = th.c("focus_title_bg" if focused else "title_bg")
        x, y, wd, ht = r
        grad = th.o("gradient") if focused else None

        def gcolor(i):
            if not grad or wd <= 1:
                return fg
            return blend(grad[0], grad[1], i / float(wd - 1))

        titlebar = th.o("titlebar")
        # top edge
        if titlebar:
            cv.fill(x, y, wd, 1, " ", tfg, tbg)
        else:
            row = bs["tl"] + bs["top"] * (wd - 2) + bs["tr"]
            if grad:
                for i, ch in enumerate(row):
                    cv.put(x + i, y, ch, gcolor(i), bg, fl)
            else:
                cv.put(x, y, row, fg, bg, fl)
        # bottom edge
        if ht >= 2:
            cv.put(x, y + ht - 1, bs["bl"] + bs["bottom"] * (wd - 2) + bs["br"], fg, bg, fl)
        # sides
        for yy in range(y + 1, y + ht - 1):
            cv.put(x, yy, bs["left"], fg, bg, fl)
            cv.put(x + wd - 1, yy, bs["right"], fg, bg, fl)
        # title
        if wd >= 8:
            self._draw_title(cv, w, r, focused, fg, bg, tfg, tbg, fl)
        # scrollbars
        self._scrollbars(cv, w, r, inner, focused, fg, bg)

    def _draw_title(self, cv, w, r, focused, fg, bg, tfg, tbg, fl):
        th = self.wm.theme
        x, y, wd, ht = r
        titlebar = th.o("titlebar")
        deco_l, deco_r = th.o("title_decor")
        pad = th.o("title_pad")
        marker = th.o("focus_marker") if focused else ""
        text = marker + w.title
        # right indicators
        ind = []
        if w.exited:
            ind.append("exit %s" % (w.exit_code if w.exit_code is not None else "?"))
        if w.scroll_y:
            ind.append("↑%d" % w.scroll_y)
        if w.opts["cp437"]:
            ind.append("437")
        if w.activity and not focused:
            ind.append("●")
        if w.bell and not focused:
            ind.append("␇")
        if w.vsize:
            ind.append("%dx%d" % w.vsize)
        if w.desktop is not None and w.desktop.zoom == w.id:
            ind.append("zoom")
        if w.floating:
            ind.append("float")
        icon = w.opts.get("icon")
        if icon:
            text = icon + " " + text
        maxw = wd - 4
        ind_txt = " ".join(ind)
        if ind_txt and str_width(ind_txt) + 4 < maxw // 2 + 8:
            pass
        else:
            ind_txt = ""
        gl, gr = th.o("gadgets")
        if titlebar:
            avail = wd - 2 - (2 if gl else 0) - (2 if gr else 0) - (str_width(ind_txt) + 1 if ind_txt else 0)
            t = truncate(text, max(1, avail))
            tf = tfg
            align = th.o("title_align")
            if align == "center":
                tx = x + max(1 + (2 if gl else 0), (wd - str_width(t)) // 2)
            else:
                tx = x + 1 + (2 if gl else 0)
            cv.put(tx, y, pad + t + pad if pad else t, tfg, tbg, BOLD if focused and th.o("bold_focus_title") else 0)
            if gl:
                cv.put(x + 1, y, gl, tfg, tbg, BOLD)
            if gr:
                cv.put(x + wd - 2, y, gr, tfg, tbg, BOLD)
            if ind_txt:
                cv.put(x + wd - 2 - (2 if gr else 0) - str_width(ind_txt), y, ind_txt, tfg, tbg)
            return
        room = maxw - str_width(deco_l) - str_width(deco_r) - 2 * str_width(pad)
        if ind_txt:
            room -= 0
        t = truncate(text, max(1, room))
        full = deco_l + pad + t + pad + deco_r
        align = th.o("title_align")
        fx = x + 2 if align != "center" else x + max(2, (wd - str_width(full)) // 2)
        bold = BOLD if (focused and th.o("bold_focus_title")) else 0
        # decorators keep the border colors, text uses title colors
        dx = fx
        if deco_l:
            dx += cv.put(dx, y, deco_l, fg, bg, fl)
        dx += cv.put(dx, y, pad + t + pad, tfg, tbg, bold)
        if deco_r:
            cv.put(dx, y, deco_r, fg, bg, fl)
        if ind_txt:
            ix = x + wd - 2 - str_width(ind_txt) - 2
            if ix > dx + 1:
                cv.put(ix, y, " " + ind_txt + " ", tfg if focused else th.c("title_fg"), bg if not focused else tbg or bg)

    def _scrollbars(self, cv, w, r, inner: Rect, focused: bool, fg, bg):
        th = self.wm.theme
        mode = w.opts["scrollbar"]
        if mode == "off":
            return
        vh = inner.h
        total, first = w.scroll_info(vh)
        show_v = mode == "on" or (mode == "auto" and total > vh and (w.scroll_y > 0 or len(w.screen.history) > 0 or w.vsize is not None) and w.kind != "internal")
        if isinstance(w, __import__("pytermwm.window", fromlist=["TextWindow"]).TextWindow) and mode == "auto":
            show_v = total > vh
        if show_v and vh >= 3 and total > 0:
            thumb = max(1, int(round(vh * vh / float(max(total, vh)))))
            span = max(1, vh - thumb)
            maxfirst = max(1, total - vh)
            pos = int(round(min(first, maxfirst) * span / float(maxfirst)))
            track = th.o("scroll_track")
            tfgc = th.c("scroll_thumb_fg")
            sfg = th.c("scroll_fg")
            x = r.x + r.w - 1
            for i in range(vh):
                y = inner.y + i
                if pos <= i < pos + thumb:
                    cv.put(x, y, th.o("scroll_thumb"), tfgc, bg)
                elif track:
                    cv.put(x, y, track, sfg, bg)
        cols_total, sx = w.hscroll_info(inner.w)
        if (mode == "on" or mode == "auto") and cols_total > inner.w and inner.w >= 4:
            thumb = max(1, int(round(inner.w * inner.w / float(cols_total))))
            span = max(1, inner.w - thumb)
            pos = int(round(sx * span / float(max(1, cols_total - inner.w))))
            y = r.y + r.h - 1
            for i in range(inner.w):
                if pos <= i < pos + thumb:
                    cv.put(inner.x + i, y, th.o("hscroll_thumb"), th.c("scroll_thumb_fg"), bg)

    # -------------------------------------------------------------------- overlays
    def draw_palette(self, cv: Canvas) -> Tuple[int, int]:
        wm = self.wm
        th = wm.theme
        pal = wm.palette
        cols, rows = cv.cols, cv.rows
        w = min(cols - 4, 76)
        n = min(len(pal.filtered), pal.max_rows)
        h = n + 3
        x = (cols - w) // 2
        y = max(1, rows // 6)
        fg, bg = th.c("dialog_fg"), th.c("dialog_bg")
        bset = th.border_set(dialog=True)
        bfg = th.c("dialog_border_fg")
        if th.o("shadow"):
            self._shadow(cv, Rect(x, y, w, h))
        cv.fill(x, y, w, h, " ", fg, bg)
        cv.put(x, y, bset["tl"] + bset["top"] * (w - 2) + bset["tr"], bfg, bg)
        cv.put(x, y + h - 1, bset["bl"] + bset["bottom"] * (w - 2) + bset["br"], bfg, bg)
        for yy in range(y + 1, y + h - 1):
            cv.put(x, yy, bset["left"], bfg, bg)
            cv.put(x + w - 1, yy, bset["right"], bfg, bg)
        title = " command palette%s " % ("" if pal.scope == "all" else ": " + pal.scope)
        cv.put(x + 2, y, title, th.c("dialog_title_fg"), th.c("dialog_title_bg"), BOLD)
        cv.put(x + 2, y + 1, "› " + pal.editor.text, fg, bg, BOLD, max_w=w - 4)
        top = max(0, pal.sel - (pal.max_rows - 1))
        for i, (label, detail, cmd) in enumerate(pal.filtered[top:top + n]):
            yy = y + 2 + i
            sel = (top + i == pal.sel)
            f, b = (th.c("dialog_sel_fg"), th.c("dialog_sel_bg")) if sel else (fg, bg)
            cv.fill(x + 1, yy, w - 2, 1, " ", f, b)
            cv.put(x + 2, yy, truncate(label, w // 2 + 4), f, b, BOLD if sel else 0)
            if detail:
                dt = truncate(detail, max(0, w - 6 - min(str_width(label), w // 2 + 4)))
                cv.put(x + w - 2 - str_width(dt), yy, dt, f if sel else th.c("status_dim_fg"), b)
        if not pal.filtered:
            cv.put(x + 2, y + 2, "no matches - Enter runs the text as a command", th.c("status_dim_fg"), bg)
        return (x + 4 + pal.editor.pos, y + 1)

    def draw_completions(self, cv: Canvas, status_row: int):
        wm = self.wm
        th = wm.theme
        pr = wm.prompt
        items = pr.completions[:10]
        w = min(cv.cols, max(str_width(c) for c in items) + 4)
        h = len(items)
        y0 = status_row - h if status_row > h else status_row + 1
        x0 = 0
        for i, c in enumerate(items):
            sel = i == pr.comp_index
            cv.put(x0, y0 + i, (" " + c).ljust(w), th.c("dialog_sel_fg") if sel else th.c("dialog_fg"),
                   th.c("dialog_sel_bg") if sel else th.c("dialog_bg"))


# ---------------------------------------------------------------------------- writer
class FrameWriter:
    """Turns frames into minimal ANSI updates for one terminal."""

    def __init__(self, depth: int = 24):
        self.depth = depth
        self.prev: Optional[List[List[Cell]]] = None
        self.prev_cursor = None
        self.prev_title = None
        self.cursor_visible = None

    def invalidate(self):
        self.prev = None
        self.prev_cursor = None
        self.cursor_visible = None

    def write(self, frame: Frame, force: bool = False) -> str:
        out: List[str] = []
        cells = frame.cells
        prev = None if force else self.prev
        if prev is not None and (len(prev) != len(cells) or (cells and len(prev[0]) != len(cells[0]))):
            prev = None
        depth = self.depth
        if prev is None:
            out.append("\x1b[0m\x1b[2J")
        out.append("\x1b[?25l")
        cur_style = None
        for y, row in enumerate(cells):
            prow = prev[y] if prev is not None else None
            if prow is not None and prow == row:
                continue
            n = len(row)
            if prow is None:
                first, last = 0, n - 1
            else:
                first = 0
                while first < n and prow[first] == row[first]:
                    first += 1
                last = n - 1
                while last > first and prow[last] == row[last]:
                    last -= 1
            if first > 0 and row[first][3] & TAIL:
                first -= 1
            if last + 1 < n and row[last][3] & WIDE:
                last += 1
            out.append("\x1b[%d;%dH" % (y + 1, first + 1))
            x = first
            buf: List[str] = []
            while x <= last:
                c = row[x]
                if c[3] & TAIL:
                    x += 1
                    continue
                st = (c[1], c[2], c[3] & 0xFF)
                if st != cur_style:
                    if buf:
                        out.append("".join(buf))
                        buf = []
                    out.append(sgr(st[0], st[1], st[2], depth))
                    cur_style = st
                ch = c[0]
                buf.append(ch if ch else " ")
                x += 1
            if buf:
                out.append("".join(buf))
        if cur_style is not None:
            out.append("\x1b[0m")
        if frame.cursor is not None:
            cx, cy = frame.cursor
            out.append("\x1b[%d;%dH\x1b[?25h" % (cy + 1, cx + 1))
        if frame.title != self.prev_title:
            t = frame.title.replace("\x1b", "").replace("\x07", "")
            out.append("\x1b]0;pytermwm: %s\x07" % t if t else "\x1b]0;pytermwm\x07")
            self.prev_title = frame.title
        self.prev = [list(r) for r in cells]
        self.prev_cursor = frame.cursor
        return "".join(out)


def crop_frame(frame: Frame, cols: int, rows: int, status_row: Optional[int] = None) -> Frame:
    """Crop/pad a frame for a client with a different size.

    With ``status_row`` (the row of the status line in ``frame``) that row stays visible at the
    top or bottom edge of the smaller client so it never loses the status line/prompt."""
    cells = []
    blank = (" ", None, None, 0)
    for y in range(rows):
        if y < frame.rows:
            row = frame.cells[y][:cols]
            if len(row) < cols:
                row = list(row) + [blank] * (cols - len(row))
        else:
            row = [blank] * cols
        cells.append(row)
    cur = frame.cursor
    if status_row is not None and 0 <= status_row < frame.rows and rows < frame.rows:
        dst = 0 if status_row == 0 else rows - 1
        row = frame.cells[status_row][:cols]
        if len(row) < cols:
            row = list(row) + [(" ", None, None, 0)] * (cols - len(row))
        cells[dst] = row
        if cur and cur[1] == status_row:
            cur = (cur[0], dst) + tuple(cur[2:])
        elif cur and dst == rows - 1 and cur[1] >= rows - 1:
            cur = None
    if cur and (cur[0] >= cols or cur[1] >= rows):
        cur = None
    return Frame(cells, cur, frame.title)
