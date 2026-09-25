"""Theme engine: colors, border glyph sets and builtin themes."""
from __future__ import annotations

import copy
import os
from typing import Dict, Optional

from .colors import parse_color, Color

# ------------------------------------------------------------------ border sets
def _bs(tl, tr, bl, br, top, bottom, left, right):
    return dict(tl=tl, tr=tr, bl=bl, br=br, top=top, bottom=bottom, left=left, right=right)


BORDER_SETS: Dict[str, dict] = {
    "none": _bs(" ", " ", " ", " ", " ", " ", " ", " "),
    "single": _bs("┌", "┐", "└", "┘", "─", "─", "│", "│"),
    "rounded": _bs("╭", "╮", "╰", "╯", "─", "─", "│", "│"),
    "double": _bs("╔", "╗", "╚", "╝", "═", "═", "║", "║"),
    "heavy": _bs("┏", "┓", "┗", "┛", "━", "━", "┃", "┃"),
    "dashed": _bs("┌", "┐", "└", "┘", "╌", "╌", "┆", "┆"),
    "ascii": _bs("+", "+", "+", "+", "-", "-", "|", "|"),
    # modern "inner line" look using half blocks: the line hugs the content
    "block": _bs("▗", "▖", "▝", "▘", "▄", "▀", "▐", "▌"),
    # thick outer line using half blocks (line on the outside edge)
    "outer_block": _bs("▛", "▜", "▙", "▟", "▀", "▄", "▌", "▐"),
    "full": _bs("█", "█", "█", "█", "█", "█", "█", "█"),
    "mixed": _bs("╒", "╕", "╘", "╛", "═", "═", "│", "│"),
    "shadow": _bs("┌", "┐", "└", "┘", "─", "─", "│", "│"),
}

# ------------------------------------------------------------------ theme model
COLOR_KEYS = (
    "window_fg", "window_bg", "desktop_fg", "desktop_bg",
    "border_fg", "border_bg", "focus_border_fg", "focus_border_bg",
    "title_fg", "title_bg", "focus_title_fg", "focus_title_bg",
    "scroll_fg", "scroll_thumb_fg",
    "status_fg", "status_bg", "status_accent_fg", "status_accent_bg",
    "status_dim_fg", "status_ok_fg", "status_warn_fg", "status_err_fg",
    "prompt_fg", "prompt_bg",
    "dialog_fg", "dialog_bg", "dialog_border_fg", "dialog_title_fg", "dialog_title_bg",
    "dialog_button_fg", "dialog_button_bg", "dialog_sel_fg", "dialog_sel_bg",
    "shadow_bg", "accent", "dead_fg",
)

DEFAULTS = dict(
    name="default",
    border="rounded",              # unfocused border set
    focus_border="rounded",        # focused border set
    dialog_border="rounded",
    titlebar=False,                # amiga style filled title bar
    title_align="left",
    title_decor=("", ""),          # text drawn around the title text e.g. ("┤ ", " ├")
    title_pad=" ",
    focus_marker="",
    title_focus_bold=True,
    border_bold_focus=True,
    gadgets=("", ""),              # (left, right) glyphs in the title bar
    desktop_char=" ",
    shadow="",                     # "", "dim", "block"
    dim_unfocused=False,
    scroll_thumb="█",
    scroll_track=None,             # None -> border glyph
    hscroll_thumb="▬",
    status_sep=" ",
    status_style="plain",          # plain | powerline | pill | brackets
    status_prefix="",
    gradient=None,                 # (from_color, to_color) for focus border/title (truecolor)
    cp437_borders=False,
    bold_focus_title=True,
)

DEFAULT_COLORS = dict(
    window_fg=None, window_bg=None, desktop_fg="#3b4252", desktop_bg=None,
    border_fg="#4c566a", border_bg=None, focus_border_fg="#88c0d0", focus_border_bg=None,
    title_fg="#81a1c1", title_bg=None, focus_title_fg="#eceff4", focus_title_bg=None,
    scroll_fg="#4c566a", scroll_thumb_fg="#88c0d0",
    status_fg="#d8dee9", status_bg="#2e3440", status_accent_fg="#2e3440", status_accent_bg="#88c0d0",
    status_dim_fg="#616e88", status_ok_fg="#a3be8c", status_warn_fg="#ebcb8b", status_err_fg="#bf616a",
    prompt_fg="#eceff4", prompt_bg="#3b4252",
    dialog_fg="#eceff4", dialog_bg="#2e3440", dialog_border_fg="#88c0d0",
    dialog_title_fg="#2e3440", dialog_title_bg="#88c0d0",
    dialog_button_fg="#d8dee9", dialog_button_bg="#434c5e", dialog_sel_fg="#2e3440", dialog_sel_bg="#ebcb8b",
    shadow_bg="#1a1e26", accent="#88c0d0", dead_fg="#bf616a",
)


class Theme:
    def __init__(self, data: Optional[dict] = None):
        self.opts = copy.deepcopy(DEFAULTS)
        self.colors: Dict[str, Color] = {k: parse_color(v) for k, v in DEFAULT_COLORS.items()}
        if data:
            self.update(data)

    # -------------------------------------------------------------- loading
    def update(self, data: dict):
        data = dict(data)
        colors = data.pop("colors", {}) or {}
        for k, v in data.items():
            if k not in DEFAULTS:
                raise ValueError("unknown theme option: %s" % k)
            if k in ("border", "focus_border", "dialog_border"):
                if v not in BORDER_SETS:
                    raise ValueError("unknown border set %r (choose from %s)" % (v, ", ".join(sorted(BORDER_SETS))))
            if k in ("title_decor", "gadgets") and not isinstance(v, (list, tuple)):
                raise ValueError("%s must be a pair of strings" % k)
            if k == "gradient" and v is not None:
                v = (parse_color(v[0]), parse_color(v[1]))
            if k in ("title_decor", "gadgets"):
                v = tuple(v)
            self.opts[k] = v
        for k, v in colors.items():
            if k not in COLOR_KEYS:
                raise ValueError("unknown theme color: %s" % k)
            self.colors[k] = parse_color(v)

    @staticmethod
    def from_dict(data: dict) -> "Theme":
        data = dict(data)
        base = data.pop("extends", None)
        if base:
            t = get_theme(base)
            t.update(data)
            return t
        return Theme(data)

    # -------------------------------------------------------------- accessors
    @property
    def name(self) -> str:
        return self.opts["name"]

    def c(self, key: str) -> Color:
        return self.colors.get(key)

    def o(self, key: str):
        return self.opts[key]

    def border_set(self, focused: bool = False, dialog: bool = False) -> dict:
        if dialog:
            return BORDER_SETS[self.opts["dialog_border"]]
        return BORDER_SETS[self.opts["focus_border" if focused else "border"]]

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.opts.items()}
        d["colors"] = {k: (v if not isinstance(v, tuple) else "#%02x%02x%02x" % v) for k, v in self.colors.items()}
        return d


# ------------------------------------------------------------------ builtin themes
BUILTIN: Dict[str, dict] = {
    "default": {"name": "default"},

    "light": {
        "name": "light", "border": "rounded", "focus_border": "rounded",
        "colors": dict(
            window_fg=None, window_bg=None, desktop_fg="#d8dee9", border_fg="#9aa5b8", focus_border_fg="#2e6fb5",
            title_fg="#5e6b80", focus_title_fg="#1b3a5c", scroll_fg="#c0c8d4", scroll_thumb_fg="#2e6fb5",
            status_fg="#2e3440", status_bg="#e5e9f0", status_accent_fg="#ffffff", status_accent_bg="#2e6fb5",
            status_dim_fg="#7b88a1", prompt_fg="#2e3440", prompt_bg="#d8dee9",
            dialog_fg="#2e3440", dialog_bg="#eceff4", dialog_border_fg="#2e6fb5",
            dialog_title_fg="#ffffff", dialog_title_bg="#2e6fb5", dialog_button_fg="#2e3440",
            dialog_button_bg="#d8dee9", accent="#2e6fb5"),
    },

    "modern": {
        "name": "modern", "border": "block", "focus_border": "block", "shadow": "dim",
        "title_decor": ("▐ ", " ▌"), "title_pad": "", "focus_marker": "● ",
        "gradient": ("#88c0d0", "#b48ead"), "status_style": "pill", "dim_unfocused": True,
        "colors": dict(border_fg="#3b4252", focus_border_fg="#88c0d0", title_fg="#7b88a1", focus_title_fg="#eceff4"),
    },

    "hacker": {
        "name": "hacker", "border": "single", "focus_border": "heavy",
        "title_decor": ("[ ", " ]"), "title_pad": "", "desktop_char": "·",
        "focus_marker": "> ", "status_prefix": "root@pytermwm:~# ", "dialog_border": "heavy",
        "colors": dict(
            window_fg="#00d030", window_bg="#000000", desktop_fg="#003a10", desktop_bg="#000000",
            border_fg="#007a1c", border_bg="#000000", focus_border_fg="#39ff14", focus_border_bg="#000000",
            title_fg="#00a028", title_bg="#000000", focus_title_fg="#000000", focus_title_bg="#39ff14",
            scroll_fg="#004d12", scroll_thumb_fg="#39ff14",
            status_fg="#00d030", status_bg="#001a06", status_accent_fg="#000000", status_accent_bg="#39ff14",
            status_dim_fg="#006618", status_ok_fg="#39ff14", status_warn_fg="#d0ff00", status_err_fg="#ff2a2a",
            prompt_fg="#39ff14", prompt_bg="#000000",
            dialog_fg="#39ff14", dialog_bg="#000a02", dialog_border_fg="#39ff14",
            dialog_title_fg="#000000", dialog_title_bg="#39ff14", dialog_button_fg="#00d030", dialog_button_bg="#002a0c",
            dialog_sel_fg="#000000", dialog_sel_bg="#39ff14", shadow_bg="#000000", accent="#39ff14", dead_fg="#ff2a2a"),
    },

    "bbs": {   # 1990 BBS: VGA 16 colors, double lines, blocks, CP437 look
        "name": "bbs", "border": "double", "focus_border": "double", "dialog_border": "double",
        "title_decor": ("╡ ", " ╞"), "title_pad": "", "desktop_char": "▒",
        "shadow": "block", "cp437_borders": True, "status_style": "brackets",
        "scroll_thumb": "█", "scroll_track": "░",
        "colors": dict(
            window_fg="#aaaaaa", window_bg="#0000aa", desktop_fg="#0000aa", desktop_bg="#000055",
            border_fg="#00aaaa", border_bg="#0000aa", focus_border_fg="#ffffff", focus_border_bg="#0000aa",
            title_fg="#00aaaa", title_bg="#0000aa", focus_title_fg="#ffff55", focus_title_bg="#0000aa",
            scroll_fg="#00aaaa", scroll_thumb_fg="#ffffff",
            status_fg="#000000", status_bg="#aaaaaa", status_accent_fg="#ffff55", status_accent_bg="#aa0000",
            status_dim_fg="#555555", status_ok_fg="#00aa00", status_warn_fg="#aa5500", status_err_fg="#aa0000",
            prompt_fg="#ffff55", prompt_bg="#0000aa",
            dialog_fg="#000000", dialog_bg="#aaaaaa", dialog_border_fg="#ffffff",
            dialog_title_fg="#ffffff", dialog_title_bg="#aa0000", dialog_button_fg="#000000",
            dialog_button_bg="#00aaaa", dialog_sel_fg="#ffff55", dialog_sel_bg="#aa0000",
            shadow_bg="#000000", accent="#ffff55", dead_fg="#aa0000"),
    },

    "mc": {    # midnight commander
        "name": "mc", "border": "single", "focus_border": "double", "dialog_border": "single",
        "title_decor": ("┤ ", " ├"), "title_pad": "", "shadow": "dim", "status_style": "brackets",
        "scroll_thumb": "█", "scroll_track": "░",
        "colors": dict(
            window_fg="#aaaaaa", window_bg="#0000aa", desktop_fg="#0000aa", desktop_bg="#0000aa",
            border_fg="#aaaaaa", border_bg="#0000aa", focus_border_fg="#ffffff", focus_border_bg="#0000aa",
            title_fg="#ffff55", title_bg="#0000aa", focus_title_fg="#000000", focus_title_bg="#00aaaa",
            scroll_fg="#00aaaa", scroll_thumb_fg="#ffffff",
            status_fg="#000000", status_bg="#00aaaa", status_accent_fg="#ffffff", status_accent_bg="#000000",
            status_dim_fg="#005555", status_ok_fg="#005500", status_warn_fg="#555500", status_err_fg="#aa0000",
            prompt_fg="#000000", prompt_bg="#00aaaa",
            dialog_fg="#000000", dialog_bg="#aaaaaa", dialog_border_fg="#000000",
            dialog_title_fg="#ffff55", dialog_title_bg="#aaaaaa", dialog_button_fg="#000000",
            dialog_button_bg="#aaaaaa", dialog_sel_fg="#000000", dialog_sel_bg="#00aaaa",
            shadow_bg="#000000", accent="#00aaaa", dead_fg="#ff5555"),
    },

    "c64": {   # Commodore 64: light blue on blue, block frame, READY.
        "name": "c64", "border": "full", "focus_border": "full", "dialog_border": "full",
        "title_decor": ("", ""), "title_pad": " ", "desktop_char": " ", "status_prefix": "READY. ",
        "focus_marker": "", "status_style": "plain", "scroll_thumb": "█", "scroll_track": "▒",
        "colors": dict(
            window_fg="#7869c4", window_bg="#40318d", desktop_fg="#40318d", desktop_bg="#40318d",
            border_fg="#7869c4", border_bg="#7869c4", focus_border_fg="#a59aef", focus_border_bg="#a59aef",
            title_fg="#40318d", title_bg="#7869c4", focus_title_fg="#40318d", focus_title_bg="#a59aef",
            scroll_fg="#40318d", scroll_thumb_fg="#ffffff",
            status_fg="#40318d", status_bg="#7869c4", status_accent_fg="#7869c4", status_accent_bg="#40318d",
            status_dim_fg="#50459b", status_ok_fg="#55a049", status_warn_fg="#bfce72", status_err_fg="#9f4e44",
            prompt_fg="#40318d", prompt_bg="#a59aef",
            dialog_fg="#40318d", dialog_bg="#7869c4", dialog_border_fg="#a59aef",
            dialog_title_fg="#7869c4", dialog_title_bg="#40318d", dialog_button_fg="#40318d",
            dialog_button_bg="#a59aef", dialog_sel_fg="#7869c4", dialog_sel_bg="#40318d",
            shadow_bg="#40318d", accent="#a59aef", dead_fg="#9f4e44"),
    },

    "amiga": {  # Workbench 1.3: blue/white/orange/black, title bars with gadgets
        "name": "amiga", "border": "single", "focus_border": "single", "dialog_border": "single",
        "titlebar": True, "gadgets": ("□", "▣"), "title_align": "center", "title_pad": " ",
        "shadow": "", "status_style": "plain", "status_prefix": "Workbench Screen  ",
        "scroll_thumb": "█", "scroll_track": "▒",
        "colors": dict(
            window_fg="#ffffff", window_bg="#0055aa", desktop_fg="#0055aa", desktop_bg="#0055aa",
            border_fg="#ffffff", border_bg="#0055aa", focus_border_fg="#ff8800", focus_border_bg="#0055aa",
            title_fg="#0055aa", title_bg="#ffffff", focus_title_fg="#000000", focus_title_bg="#ff8800",
            scroll_fg="#ffffff", scroll_thumb_fg="#ff8800",
            status_fg="#0055aa", status_bg="#ffffff", status_accent_fg="#ffffff", status_accent_bg="#0055aa",
            status_dim_fg="#7799bb", status_ok_fg="#00aa00", status_warn_fg="#ff8800", status_err_fg="#cc0000",
            prompt_fg="#000000", prompt_bg="#ff8800",
            dialog_fg="#000000", dialog_bg="#ffffff", dialog_border_fg="#000000",
            dialog_title_fg="#ffffff", dialog_title_bg="#000000", dialog_button_fg="#000000",
            dialog_button_bg="#aaaaaa", dialog_sel_fg="#ffffff", dialog_sel_bg="#ff8800",
            shadow_bg="#000000", accent="#ff8800", dead_fg="#cc0000"),
    },

    "nes": {   # Nintendo Famicom / NES: the console's red and gold on cream, a dark gray cartridge slot, chunky frames
        "name": "nes", "border": "outer_block", "focus_border": "outer_block", "dialog_border": "heavy",
        "title_decor": ("▌", "▐"), "title_pad": " ", "desktop_char": "▚", "focus_marker": "▶ ",
        "status_prefix": "PLAYER 1  ", "status_style": "pill", "scroll_thumb": "█", "scroll_track": "▒",
        "colors": dict(
            window_fg="#fcfcfc", window_bg="#000000", desktop_fg="#3c3c3c", desktop_bg="#242424",
            border_fg="#7c7c7c", border_bg="#242424", focus_border_fg="#b8001c", focus_border_bg="#242424",
            title_fg="#f0e6c8", title_bg="#7c7c7c", focus_title_fg="#f0e6c8", focus_title_bg="#b8001c",
            scroll_fg="#7c7c7c", scroll_thumb_fg="#d8a038",
            status_fg="#242424", status_bg="#f0e6c8", status_accent_fg="#f0e6c8", status_accent_bg="#b8001c",
            status_dim_fg="#7c7c7c", status_ok_fg="#00a800", status_warn_fg="#d8a038", status_err_fg="#b8001c",
            prompt_fg="#242424", prompt_bg="#d8a038",
            dialog_fg="#fcfcfc", dialog_bg="#000000", dialog_border_fg="#fcfcfc",
            dialog_title_fg="#000000", dialog_title_bg="#fcfcfc", dialog_button_fg="#fcfcfc",
            dialog_button_bg="#3c3c3c", dialog_sel_fg="#f0e6c8", dialog_sel_bg="#b8001c",
            shadow_bg="#000000", accent="#d8a038", dead_fg="#b8001c"),
    },

    "matrix": {  # green rain on black: glowing phosphor frames; goes well with `effect matrix`
        "name": "matrix", "border": "single", "focus_border": "single", "dialog_border": "single",
        "title_decor": ("┤ ", " ├"), "title_pad": "", "desktop_char": " ", "focus_marker": "ｦ ",
        "status_prefix": "wake up, neo...  ", "status_style": "plain", "dim_unfocused": True,
        "gradient": ("#00ff41", "#008f11"), "scroll_thumb": "┃",
        "colors": dict(
            window_fg="#00ff41", window_bg="#000000", desktop_fg="#003b00", desktop_bg="#000000",
            border_fg="#005f10", border_bg="#000000", focus_border_fg="#00ff41", focus_border_bg="#000000",
            title_fg="#008f11", title_bg="#000000", focus_title_fg="#d0ffd8", focus_title_bg="#000000",
            scroll_fg="#003b00", scroll_thumb_fg="#00ff41",
            status_fg="#00ff41", status_bg="#000000", status_accent_fg="#000000", status_accent_bg="#00ff41",
            status_dim_fg="#008f11", status_ok_fg="#00ff41", status_warn_fg="#b8ff5a", status_err_fg="#ff3030",
            prompt_fg="#d0ffd8", prompt_bg="#001a05",
            dialog_fg="#00ff41", dialog_bg="#000800", dialog_border_fg="#00ff41",
            dialog_title_fg="#000000", dialog_title_bg="#00ff41", dialog_button_fg="#00ff41",
            dialog_button_bg="#002a08", dialog_sel_fg="#000000", dialog_sel_bg="#00ff41",
            shadow_bg="#000000", accent="#00ff41", dead_fg="#ff3030"),
    },

    "dos": {   # MS-DOS: light gray on black, CGA colors, C:\> in the status line, EDIT.COM style dialogs
        "name": "dos", "border": "single", "focus_border": "double", "dialog_border": "double",
        "title_decor": ("[ ", " ]"), "title_pad": "", "desktop_char": " ", "cp437_borders": True,
        "status_prefix": "C:\\> ", "status_style": "plain", "shadow": "block",
        "scroll_thumb": "█", "scroll_track": "░",
        "colors": dict(
            window_fg="#aaaaaa", window_bg="#000000", desktop_fg="#555555", desktop_bg="#000000",
            border_fg="#aaaaaa", border_bg="#000000", focus_border_fg="#ffffff", focus_border_bg="#000000",
            title_fg="#aaaaaa", title_bg="#000000", focus_title_fg="#000000", focus_title_bg="#aaaaaa",
            scroll_fg="#555555", scroll_thumb_fg="#aaaaaa",
            status_fg="#aaaaaa", status_bg="#000000", status_accent_fg="#000000", status_accent_bg="#aaaaaa",
            status_dim_fg="#555555", status_ok_fg="#55ff55", status_warn_fg="#ffff55", status_err_fg="#ff5555",
            prompt_fg="#ffffff", prompt_bg="#000000",
            dialog_fg="#000000", dialog_bg="#aaaaaa", dialog_border_fg="#ffffff",
            dialog_title_fg="#ffffff", dialog_title_bg="#0000aa", dialog_button_fg="#000000",
            dialog_button_bg="#aaaaaa", dialog_sel_fg="#ffffff", dialog_sel_bg="#0000aa",
            shadow_bg="#000000", accent="#ffffff", dead_fg="#ff5555"),
    },
}


def theme_names(extra_dirs=()):
    names = list(BUILTIN)
    for d in extra_dirs:
        try:
            for f in sorted(os.listdir(d)):
                if f.endswith((".yaml", ".yml")):
                    n = f.rsplit(".", 1)[0]
                    if n not in names:
                        names.append(n)
        except OSError:
            pass
    return names


_user_themes: Dict[str, dict] = {}


def register_theme(name: str, data: dict):
    """Register a user/plugin defined theme (dict as in yaml)."""
    d = dict(data)
    d.setdefault("name", name)
    _user_themes[name] = d


def get_theme(name: str) -> Theme:
    if name in _user_themes:
        return Theme.from_dict(_user_themes[name])
    if name in BUILTIN:
        return Theme.from_dict(BUILTIN[name])
    raise KeyError("unknown theme: %s (available: %s)" % (name, ", ".join(list(BUILTIN) + sorted(_user_themes))))


def all_theme_names():
    return list(BUILTIN) + [n for n in _user_themes if n not in BUILTIN]
