"""Builtin window manager commands (registered into the global registry on import)."""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from .commands import CommandError, command
from .keys import keys_to_bytes
from .layout import ENGINE_NAMES
from .theme import all_theme_names, get_theme, BORDER_SETS
from .window import OPTION_SPECS, InternalWindow, PtySource


# ----------------------------------------------------------------------------- helpers
def parse_flags(args: List[str], spec: Dict[str, int], intermixed: bool = False) -> Tuple[Dict[str, object], List[str]]:
    """Tiny flag parser.  spec maps flag -> number of values (0 or 1).

    Stops at ``--`` or the first non-flag, unless ``intermixed`` is set, in which
    case flags may appear anywhere and non-flags are collected in order."""
    flags: Dict[str, object] = {}
    rest: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest = args[i + 1:]
            break
        if a in spec:
            if spec[a] == 0:
                flags[a] = True
            else:
                if i + 1 >= len(args):
                    raise CommandError("option %s needs a value" % a)
                flags[a] = args[i + 1]
                i += 1
        elif a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
            raise CommandError("unknown option: %s" % a)
        elif intermixed:
            rest.append(a)
        else:
            rest = args[i:]
            break
        i += 1
    return flags, rest


def truthy(s: str, current: Optional[bool] = None) -> bool:
    s = str(s).lower()
    if s == "toggle":
        return not current
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    raise CommandError("expected on/off/toggle, got %r" % s)


def cmd_string(rest: List[str]):
    """`-- CMD` (one word: a command line for the shell) or `-- PROG ARG...` (an argv, run as is without a shell: joining
    it into a POSIX-quoted string would be read wrongly by PowerShell / cmd.exe on Windows)."""
    if not rest:
        return None
    if len(rest) == 1:
        return rest[0]
    return list(rest)


# completers
def _c_windows(wm, prev, partial):
    out = [str(w.id) for w in wm.windows.values()] + [w.name for w in wm.windows.values() if w.name]
    return out


def _c_desktops(wm, prev, partial):
    return ["next", "prev", "last", "new"] + [d.name for d in wm.desktops] + [str(i + 1) for i in range(len(wm.desktops))]


def _c_layouts(wm, prev, partial):
    return list(ENGINE_NAMES) + ["next", "prev", "balance", "list"]


def _c_themes(wm, prev, partial):
    return all_theme_names() + ["next", "prev", "list"]


def _c_dirs(wm, prev, partial):
    return ["left", "right", "up", "down"]


def _c_focus(wm, prev, partial):
    return ["next", "prev", "last", "left", "right", "up", "down"] + _c_windows(wm, prev, partial)


def _c_options(wm, prev, partial):
    if not prev:
        return list(OPTION_SPECS)
    spec = OPTION_SPECS.get(prev[0])
    if spec and spec[0] == "bool":
        return ["on", "off", "toggle"]
    if spec and spec[0] == "choice":
        return list(spec[2])
    return []


def _c_topics(wm, prev, partial):
    from .helpdata import topic_names
    return topic_names(wm)


def _c_paths(wm, prev, partial):
    from .completion import complete_path
    return complete_path(partial)


# ----------------------------------------------------------------------------- selection and clipboard
@command("copy-mode", usage="copy-mode", help="Keyboard selection: move with hjkl/arrows, v selects, y copies, Esc cancels",
         category="window")
def c_copy_mode(wm, args):
    wm.copy_mode_enter()


@command("copy-move", usage="copy-move left|right|up|down|page-up|page-down|top|bottom|home|end|first|word-next|word-prev [N]",
         help="Move the copy-mode cursor", category="window", hidden=True)
def c_copy_move(wm, args):
    wm.copy_move(args[0] if args else "down", int(args[1]) if len(args) > 1 else 1)


@command("copy-select", usage="copy-select [char|line|rect]", help="Start (or stop) selecting in copy mode", category="window",
         hidden=True)
def c_copy_select(wm, args):
    wm.copy_select(args[0] if args else "char")


@command("copy-yank", usage="copy-yank", help="Copy the copy-mode selection and leave copy mode", category="window", hidden=True)
def c_copy_yank(wm, args):
    wm.copy_yank()


@command("copy-cancel", usage="copy-cancel", help="Leave copy mode without copying", category="window", hidden=True)
def c_copy_cancel(wm, args):
    wm.copy_cancel()


@command("copy-selection", usage="copy-selection", help="Copy the current mouse selection (when selection.copy_on_release is off)",
         aliases=("copy",), category="window")
def c_copy_selection(wm, args):
    text = wm.copy_selection()
    return "copied %d characters" % len(text)


@command("select-all", usage="select-all [window]", help="Select everything in a window, including its scrollback", category="window")
def c_select_all(wm, args):
    from .selection import Selection, base_id, line_count
    w = wm.resolve_window(args[0] if args else None)
    scr = w.screen
    first = (base_id(scr), 0)
    sel = Selection(w.id, scr, first)
    sel.head = (base_id(scr) + line_count(scr) - 1, scr.cols - 1)
    wm.selection = sel
    wm.dirty = True
    if wm.sel_cfg().get("copy_on_release", True):
        wm.copy_selection()


@command("paste", usage="paste [text...]", help="Paste the paste buffer (or the given text) into the focused window",
         aliases=("paste-buffer",), category="window")
def c_paste(wm, args):
    wm.paste_text(" ".join(args) if args else None)


@command("clipboard", usage="clipboard [show|set TEXT...|list]", help="Show or set the paste buffer", category="window")
def c_clipboard(wm, args):
    a = args[0] if args else "show"
    if a == "show":
        return wm.paste_buffer
    if a == "list":
        return "\n".join("%d: %s" % (i, t.replace("\n", "\\n")[:70]) for i, t in enumerate(wm.paste_history))
    if a == "set" and len(args) > 1:
        return "%d characters" % wm.copy_text(" ".join(args[1:]))
    raise CommandError("usage: clipboard [show|list|set TEXT]")


@command("copy-view", usage="copy-view", help="Show only the focused window's text with the mouse off, so the terminal's own "
                                               "selection (PuTTY: drag to copy) gets clean text; any other key returns",
         category="window")
def c_copy_view(wm, args):
    if wm.copy_view is not None:
        wm.copy_view_exit()
    else:
        wm.copy_view_enter()


# ----------------------------------------------------------------------------- notifications
@command("notify", usage="notify [-t TITLE] [-s normal|ok|warn|err] MESSAGE...",
         help="Show a notification (status line, web UI, desktop via the terminal or `notify.command`)", category="misc")
def c_notify(wm, args):
    from . import notify
    f, rest = parse_flags(args, {"-t": 1, "-s": 1}, intermixed=True)
    if not rest:
        raise CommandError("usage: notify [-t TITLE] MESSAGE")
    style = f.get("-s", "normal")
    if style not in notify.STYLES:
        raise CommandError("style must be one of: %s" % ", ".join(notify.STYLES))
    return notify.send(wm, " ".join(rest), f.get("-t", "pytermwm"), style)


# ----------------------------------------------------------------------------- recording
@command("record", usage="record [-t WINDOW] [-i] [-f] [FILE]",
         help="Record a window to an asciicast (.cast) file; -i also records what is typed (passwords too!)",
         category="session")
def c_record(wm, args):
    from . import recording
    f, rest = parse_flags(args, {"-t": 1, "-i": 0, "-f": 0}, intermixed=True)
    w = wm.resolve_window(f.get("-t"))
    rec = recording.start(wm, w, rest[0] if rest else None, capture_input="-i" in f, overwrite="-f" in f)
    wm.message("recording window %s to %s" % (w.id, rec.path), "ok", 4.0)
    return "recording window %s to %s" % (w.id, rec.path)


@command("record-stop", usage="record-stop [-t WINDOW]", help="Stop recording a window", category="session")
def c_record_stop(wm, args):
    from . import recording
    f, _ = parse_flags(args, {"-t": 1})
    w = wm.resolve_window(f.get("-t"))
    info = recording.stop(w)
    if info is None:
        raise CommandError("window %s is not being recorded" % w.id)
    msg = "saved %s (%d events, %.0f s)" % (info["path"], info["events"], info["seconds"])
    wm.message(msg, "ok", 4.0)
    return msg


@command("record-mark", usage="record-mark [-t WINDOW] [LABEL]", help="Add a marker to the recording of a window",
         category="session")
def c_record_mark(wm, args):
    f, rest = parse_flags(args, {"-t": 1})
    w = wm.resolve_window(f.get("-t"))
    if w.recorder is None:
        raise CommandError("window %s is not being recorded" % w.id)
    w.recorder.marker(" ".join(rest))
    return "marker added"


@command("screenshot", usage="screenshot [-f] [FILE.svg|.ans|.txt]",
         help="Save the whole screen as an SVG picture, ANSI art or plain text (default: an .svg in the state directory)",
         category="session")
def c_screenshot(wm, args):
    from . import screenshot
    f, rest = parse_flags(args, {"-f": 0}, intermixed=True)
    path = os.path.abspath(os.path.expanduser(rest[0])) if rest else screenshot.default_path("screenshots", ".svg")
    if os.path.exists(path) and "-f" not in f:
        raise CommandError("%s already exists (use screenshot -f to overwrite it)" % path)
    try:
        path = screenshot.screenshot(wm, path)
    except (ValueError, OSError) as e:
        raise CommandError(str(e))
    wm.message("screenshot saved to %s" % path, "ok", 4.0)
    return path


@command("record-screen", usage="record-screen [-f] [FILE.cast]",
         help="Record the whole screen (all windows, borders, status line) to an asciicast v2 file", category="session")
def c_record_screen(wm, args):
    from . import screenshot
    f, rest = parse_flags(args, {"-f": 0}, intermixed=True)
    try:
        rec = screenshot.start_recording(wm, rest[0] if rest else None, overwrite="-f" in f)
    except (ValueError, OSError) as e:
        raise CommandError(str(e))
    wm.message("recording the screen to %s" % rec.path, "ok", 4.0)
    return "recording the screen to %s" % rec.path


@command("record-screen-stop", usage="record-screen-stop", help="Stop the screen recording", category="session")
def c_record_screen_stop(wm, args):
    from . import screenshot
    info = screenshot.stop_recording(wm)
    if info is None:
        raise CommandError("the screen is not being recorded")
    msg = "saved %s (%d frames, %.0f s)" % (info["path"], info["events"], info["seconds"])
    wm.message(msg, "ok", 4.0)
    return msg


@command("replay", usage="replay FILE [--speed N] [--idle SECONDS] [--loop] [--float]",
         help="Play an asciicast (.cast) file in a window (keys: Space pause, +/- speed, r restart, arrows skip)",
         category="window")
def c_replay(wm, args):
    f, rest = parse_flags(args, {"--speed": 1, "--idle": 1, "--loop": 0, "--float": 0, "-t": 1}, intermixed=True)
    if not rest:
        raise CommandError("usage: replay FILE")
    spec = {"kind": "cast", "path": os.path.expanduser(rest[0])}
    if "--speed" in f:
        spec["speed"] = float(f["--speed"])
    if "--idle" in f:
        spec["idle"] = float(f["--idle"])
    if "--loop" in f:
        spec["loop"] = True
    if "--float" in f:
        spec["floating"] = True
    if "-t" in f:
        spec["title"] = f["-t"]
    w = wm.create_window(spec)
    return "window %d" % w.id


# ----------------------------------------------------------------------------- projects
@command("up", usage="up [--fresh] [FILE]", help="Build the desktops and windows described by a project file (.pytermwm.yaml)",
         category="session")
def c_up(wm, args):
    from . import project
    from .config import ConfigError
    fresh = "--fresh" in args
    args = [a for a in args if a != "--fresh"]
    path = os.path.abspath(os.path.expanduser(args[0])) if args else project.find_project_file(os.getcwd())
    if not path or not os.path.isfile(path):
        raise CommandError("no project file found (create .pytermwm.yaml, see docs/user_guide.md#project-files)")
    try:
        doc = project.load(path)
    except ConfigError as e:
        raise CommandError(str(e))
    res = project.apply(wm, doc, os.path.dirname(path), fresh=fresh)
    msg = "up: %d created, %d already there" % (len(res["created"]), len(res["existing"]))
    wm.message(msg, "ok", 3.0)
    return msg + ("\n" + "\n".join("  + " + c for c in res["created"]) if res["created"] else "")


# ----------------------------------------------------------------------------- windows
@command("new-window", usage="new-window [-n name] [-t title] [-d desktop] [-c cwd] [-k kind] [-p file] [--pipe] [--float] "
                             "[--split h|v] [--keep|--close] [--no-focus] [-- command...]",
         help="Create a window (default: your shell)", aliases=("new", "nw"), category="window")
def c_new_window(wm, args):
    f, rest = parse_flags(args, {"-n": 1, "-t": 1, "-d": 1, "-c": 1, "-k": 1, "-p": 1, "--pipe": 0, "--float": 0,
                                 "--split": 1, "--keep": 0, "--close": 0, "--no-focus": 0, "--dock": 1, "--rect": 1})
    spec = {}
    if "-n" in f:
        spec["name"] = f["-n"]
    if "-t" in f:
        spec["title"] = f["-t"]
    if "-d" in f:
        spec["desktop"] = f["-d"]
    if "-c" in f:
        spec["cwd"] = f["-c"]
    if "-k" in f:
        spec["kind"] = f["-k"]
    if "-p" in f:
        spec["path"] = os.path.expanduser(f["-p"])
    if "--pipe" in f:
        spec["pipe"] = True
    if "--float" in f:
        spec["floating"] = True
    if "--split" in f:
        spec["split"] = f["--split"]
    if "--keep" in f:
        spec["keep"] = True
    if "--close" in f:
        spec["keep"] = False
        spec["on_exit"] = "close"
    if "--no-focus" in f:
        spec["focus"] = False
    if "--dock" in f:
        spec["dock"] = f["--dock"]
    if "--rect" in f:
        spec["rect"] = [int(x) for x in f["--rect"].split(",")]
    cmd = cmd_string(rest)
    if cmd:
        spec["cmd"] = cmd
        spec.setdefault("title", cmd.split()[0] if isinstance(cmd, str) else cmd[0])
    w = wm.create_window(spec)
    return {"id": w.id, "title": w.title}


@command("split", usage="split h|v [-- command...]", help="Split the focused window (h = side by side, v = stacked)",
         completer=lambda wm, p, x: ["h", "v"] if not p else [], category="window")
def c_split(wm, args):
    if not args or args[0] not in ("h", "v", "horizontal", "vertical"):
        raise CommandError("usage: split h|v [command...]")
    d = "h" if args[0] in ("h", "horizontal") else "v"
    if wm.desk.layout != "tile":
        wm.set_layout("tile")
    return c_new_window(wm, ["--split", d] + args[1:])


@command("close-window", usage="close-window [window]", help="Close a window", aliases=("close", "kill-window"),
         completer=lambda wm, p, x: _c_windows(wm, p, x), category="window")
def c_close(wm, args):
    w = wm.resolve_window(args[0] if args else None)
    wm.close_window(w.id)
    return "closed %d" % w.id


@command("focus", usage="focus next|prev|last|left|right|up|down|<window>", help="Move focus",
         completer=lambda wm, p, x: _c_focus(wm, p, x), category="window")
def c_focus(wm, args):
    if not args:
        raise CommandError("usage: focus <target>")
    a = args[0]
    if a in ("next", "prev", "last", "left", "right", "up", "down"):
        wm.focus_dir(a)
    else:
        wm.focus_window(a)


@command("move", usage="move left|right|up|down", help="Swap the focused window with its neighbour",
         completer=lambda wm, p, x: _c_dirs(wm, p, x), category="window")
def c_move(wm, args):
    if not args or args[0] not in ("left", "right", "up", "down"):
        raise CommandError("usage: move left|right|up|down")
    wm.swap_dir(args[0])


@command("swap", usage="swap <window> <window>", help="Swap two windows in the current desktop", category="window")
def c_swap(wm, args):
    if len(args) != 2:
        raise CommandError("usage: swap <a> <b>")
    a, b = wm.resolve_window(args[0]), wm.resolve_window(args[1])
    if a.desktop is not wm.desk or b.desktop is not wm.desk:
        raise CommandError("both windows must be on the current desktop")
    wm.swap_windows(a.id, b.id)


@command("zoom", usage="zoom [on|off|toggle]", help="Fullscreen the focused window", aliases=("fullscreen",), category="window",
         completer=lambda wm, p, x: ["on", "off", "toggle"])
def c_zoom(wm, args):
    d = wm.desk
    w = wm.focused
    mode = args[0] if args else "toggle"
    want = truthy(mode, d.zoom is not None)
    if want:
        if not w:
            raise CommandError("no focused window")
        d.zoom = w.id
    else:
        d.zoom = None
    wm.relayout()


@command("float", usage="float [on|off|toggle] [window]", help="Make a window floating / tiled", category="window",
         completer=lambda wm, p, x: ["on", "off", "toggle"] if not p else _c_windows(wm, p, x))
def c_float(wm, args):
    w = wm.resolve_window(args[1] if len(args) > 1 else None)
    want = truthy(args[0] if args else "toggle", w.floating)
    d = w.desktop
    if want and not w.floating:
        r = d.rects.get(w.id)
        w.floating = True
        if r and w.frect is None:
            area = wm.content_area()
            nw, nh = max(20, int(r.w * 0.8)), max(6, int(r.h * 0.8))
            w.frect = (r.x + (r.w - nw) // 2, r.y + (r.h - nh) // 2, nw, nh)
        d.tree.remove(w.id)
    elif not want and w.floating:
        w.floating = False
        wm._tree_insert(d, w, None)
    wm.relayout()
    return "floating" if w.floating else "tiled"


@command("float-move", usage="float-move dx dy", help="Move the focused floating window", category="window", aliases=("fmove",))
def c_float_move(wm, args):
    if len(args) < 2:
        raise CommandError("usage: float-move dx dy")
    w = wm.resolve_window(None)
    r = w.desktop.rects.get(w.id)
    if not w.floating:
        wm.run_command_line("float on", raise_errors=True)
        r = w.desktop.rects.get(w.id)
    x, y, ww, hh = w.frect or tuple(r)
    w.frect = (x + int(args[0]), y + int(args[1]), ww, hh)
    wm.relayout()


@command("float-resize", usage="float-resize dw dh", help="Resize the focused floating window", category="window", aliases=("fresize",))
def c_float_resize(wm, args):
    if len(args) < 2:
        raise CommandError("usage: float-resize dw dh")
    w = wm.resolve_window(None)
    if not w.floating:
        wm.run_command_line("float on", raise_errors=True)
    x, y, ww, hh = w.frect
    w.frect = (x, y, ww + int(args[0]), hh + int(args[1]))
    wm.relayout()


@command("float-rect", usage="float-rect x y w h [window]", help="Set the rectangle of a floating window", category="window")
def c_float_rect(wm, args):
    if len(args) < 4:
        raise CommandError("usage: float-rect x y w h [window]")
    w = wm.resolve_window(args[4] if len(args) > 4 else None)
    w.frect = tuple(int(a) for a in args[:4])
    if not w.floating:
        w.floating = True
        w.desktop.tree.remove(w.id)
    wm.relayout()


@command("dock", usage="dock left|right|top|bottom|off [size]", help="Dock the focused window to a screen edge",
         completer=lambda wm, p, x: ["left", "right", "top", "bottom", "off"] if not p else [], category="window")
def c_dock(wm, args):
    w = wm.resolve_window(None)
    if not args:
        raise CommandError("usage: dock left|right|top|bottom|off [size]")
    d = w.desktop
    if args[0] == "off":
        w.dock = None
        wm._tree_insert(d, w, None)
    elif args[0] in ("left", "right", "top", "bottom"):
        size = float(args[1]) if len(args) > 1 else (30 if args[0] in ("left", "right") else 10)
        w.dock = (args[0], size)
        d.tree.remove(w.id)
    else:
        raise CommandError("dock: expected left|right|top|bottom|off")
    wm.relayout()


@command("window-set", usage="window-set <option> <value> [window]", help="Change a window option (border, scrollbar, overflow, cp437, history, on_exit, ...)",
         aliases=("wset",), completer=lambda wm, p, x: _c_options(wm, p, x), category="window")
def c_window_set(wm, args):
    if len(args) < 2:
        raise CommandError("usage: window-set <option> <value> [window]  (options: %s)" % ", ".join(OPTION_SPECS))
    w = wm.resolve_window(args[2] if len(args) > 2 else None)
    name, value = args[0], args[1]
    try:
        if value == "toggle":
            value = not w.opts.get(name)
        v = w.set_option(name, value)
    except KeyError as e:
        raise CommandError(str(e.args[0]))
    except ValueError as e:
        raise CommandError(str(e))
    wm.relayout()
    return "%s=%s" % (name, v)


@command("cp437", usage="cp437 [on|off|toggle] [window]", help="Toggle on-the-fly CP437 -> UTF-8 conversion", category="window",
         completer=lambda wm, p, x: ["on", "off", "toggle"])
def c_cp437(wm, args):
    return c_window_set(wm, ["cp437", args[0] if args else "toggle"] + args[1:2])


@command("rename", usage="rename <title...>", help="Set the title of the focused window", category="window")
def c_rename(wm, args):
    w = wm.resolve_window(None)
    w.title_text = " ".join(args)
    wm.dirty = True


@command("name", usage="name <name> [window]", help="Give a window a unique name usable in commands", category="window")
def c_name(wm, args):
    if not args:
        raise CommandError("usage: name <name> [window]")
    w = wm.resolve_window(args[1] if len(args) > 1 else None)
    w.name = args[0]
    wm.dirty = True


@command("vsize", usage="vsize <COLSxROWS|auto> [window]", help="Set the virtual size of a window (auto = follow the window)", category="window")
def c_vsize(wm, args):
    if not args:
        raise CommandError("usage: vsize <COLSxROWS|auto>")
    w = wm.resolve_window(args[1] if len(args) > 1 else None)
    if args[0] == "auto":
        w.set_vsize(None)
    else:
        try:
            c, _, r = args[0].lower().partition("x")
            size = (int(c), int(r))
        except ValueError:
            raise CommandError("vsize: expected COLSxROWS")
        if not (1 <= size[0] <= 1000 and 1 <= size[1] <= 1000):
            raise CommandError("vsize: 1..1000")
        w.set_vsize(size)
    wm.relayout()


@command("scroll", usage="scroll up|down|page-up|page-down|top|bottom|left|right|<n>", help="Scroll the focused window's history", category="window",
         completer=lambda wm, p, x: ["up", "down", "page-up", "page-down", "top", "bottom", "left", "right"])
def c_scroll(wm, args):
    w = wm.resolve_window(None)
    a = args[0] if args else "up"
    vh = max(1, w.viewport[1])
    if a == "up":
        w.scroll(1)
    elif a == "down":
        w.scroll(-1)
    elif a == "page-up":
        w.scroll(max(1, vh - 1))
    elif a == "page-down":
        w.scroll(-max(1, vh - 1))
    elif a == "top":
        w.scroll_to_top() if not isinstance(w, InternalWindow) else w.scroll(1 << 20)
    elif a == "bottom":
        w.scroll_to_bottom() if not isinstance(w, InternalWindow) else w.scroll(-(1 << 20))
    elif a == "left":
        w.scroll(0, -4)
    elif a == "right":
        w.scroll(0, 4)
    else:
        try:
            w.scroll(int(a))
        except ValueError:
            raise CommandError("scroll: unknown direction %r" % a)
    wm.dirty = True


@command("clear-history", usage="clear-history [window]", help="Drop the scrollback of a window", category="window")
def c_clear_history(wm, args):
    w = wm.resolve_window(args[0] if args else None)
    w.screen.history.clear()
    w.scroll_y = 0
    wm.dirty = True


@command("send-keys", usage="send-keys [-t window] key|text...", help="Send key names (Enter, C-c, Up, ...) or literal text to a window",
         aliases=("send",), category="window")
def c_send_keys(wm, args):
    f, rest = parse_flags(args, {"-t": 1, "-l": 0})
    w = wm.resolve_window(f.get("-t"))
    if "-l" in f:
        data = " ".join(rest).encode()
    else:
        data = keys_to_bytes(rest, w.screen.app_cursor)
    w.write_input(data)
    return "sent %d bytes" % len(data)


@command("send-text", usage="send-text [-t window] [-n] text...", help="Type text into a window (-n adds Enter)", category="window")
def c_send_text(wm, args):
    f, rest = parse_flags(args, {"-t": 1, "-n": 0})
    w = wm.resolve_window(f.get("-t"))
    data = " ".join(rest).encode() + (b"\r" if "-n" in f else b"")
    w.write_input(data)
    return "sent %d bytes" % len(data)


@command("respawn", usage="respawn [window]", help="Restart the process of a window", category="window")
def c_respawn(wm, args):
    w = wm.resolve_window(args[0] if args else None)
    wm.restart_window(w)


@command("list-windows", usage="list-windows", help="List all windows", aliases=("ls", "windows"), category="window")
def c_list_windows(wm, args):
    rows = []
    for i, d in enumerate(wm.desktops):
        for wid in d.windows:
            w = wm.windows[wid]
            rows.append("%s%3d  desktop %-8s %-6s %-30s %s" % ("*" if wid == d.focus and i == wm.cur else " ", wid, d.name, w.kind,
                                                                w.title[:30], "exited(%s)" % w.exit_code if w.exited else ""))
    return "\n".join(rows) if rows else "(no windows)"


@command("window-info", usage="window-info [window]", help="Detailed info about a window (JSON)", category="window")
def c_window_info(wm, args):
    return wm.resolve_window(args[0] if args else None).describe()


@command("capture", usage="capture [-t window] [-H]", help="Return the text content of a window (-H includes history)", category="window")
def c_capture(wm, args):
    f, rest = parse_flags(args, {"-t": 1, "-H": 0})
    w = wm.resolve_window(f.get("-t"))
    return w.text(history="-H" in f)


# ----------------------------------------------------------------------------- layout
@command("layout", usage="layout <%s|next|prev|balance>" % "|".join(ENGINE_NAMES), help="Change the layout engine of the current desktop",
         completer=lambda wm, p, x: _c_layouts(wm, p, x), category="layout")
def c_layout(wm, args):
    if not args:
        return wm.desk.layout
    a = args[0]
    if a == "next":
        wm.cycle_layout(1)
    elif a == "prev":
        wm.cycle_layout(-1)
    elif a == "list":
        return list(ENGINE_NAMES)
    elif a == "balance":
        c_balance(wm, [])
    elif a == "zoom-master":
        d = wm.desk
        w = wm.focused
        if w and d.windows and d.windows[0] != w.id:
            wm.swap_windows(d.windows[0], w.id)
    else:
        wm.set_layout(a)
    return wm.desk.layout


@command("balance", usage="balance", help="Equalize sizes in the current layout", category="layout")
def c_balance(wm, args):
    d = wm.desk

    def walk(n):
        if not n.leaf:
            n.weights = [1.0] * len(n.children)
            for c in n.children:
                walk(c)
    if d.tree.root:
        walk(d.tree.root)
    d.params["master_ratio"] = 0.55
    wm.relayout()


@command("resize", usage="resize left|right|up|down [cells]", help="Resize the focused window", category="layout",
         completer=lambda wm, p, x: _c_dirs(wm, p, x))
def c_resize(wm, args):
    if not args or args[0] not in ("left", "right", "up", "down"):
        raise CommandError("usage: resize left|right|up|down [cells]")
    wm.resize_focused(args[0], int(args[1]) if len(args) > 1 else 2)


@command("layout-set", usage="layout-set <master_ratio|master_count|grid_cols|gap|cols|rows|cells> <value>",
         help="Set a parameter of the current layout (cols/rows/cells take JSON)", category="layout")
def c_layout_set(wm, args):
    if len(args) < 2:
        raise CommandError("usage: layout-set <key> <value>")
    k, v = args[0], " ".join(args[1:])
    d = wm.desk
    if k in ("master_ratio",):
        d.params[k] = max(0.1, min(0.9, float(v)))
    elif k in ("master_count", "grid_cols", "gap"):
        d.params[k] = int(v) if v != "auto" else None
    elif k in ("cols", "rows", "cells"):
        try:
            d.params[k] = json.loads(v)
        except ValueError:
            raise CommandError("%s expects JSON" % k)
    else:
        raise CommandError("unknown layout parameter: %s" % k)
    wm.relayout()


@command("master-ratio", usage="master-ratio <0.1-0.9>", help="Size of the master area", category="layout")
def c_master_ratio(wm, args):
    return c_layout_set(wm, ["master_ratio"] + args)


@command("master-count", usage="master-count <n>", help="Number of master windows", category="layout")
def c_master_count(wm, args):
    return c_layout_set(wm, ["master_count"] + args)


@command("gap", usage="gap <n>", help="Gap between tiled windows", category="layout")
def c_gap(wm, args):
    return c_layout_set(wm, ["gap"] + args)


@command("rotate", usage="rotate", help="Flip the split direction around the focused window (tile layout)", category="layout")
def c_rotate(wm, args):
    w = wm.resolve_window(None)
    w.desktop.tree.rotate(w.id)
    wm.relayout()


# ----------------------------------------------------------------------------- desktops
@command("desktop", usage="desktop <n|name|next|prev|last|new [name]>", help="Switch desktop", aliases=("ws",),
         completer=lambda wm, p, x: _c_desktops(wm, p, x), category="desktop")
def c_desktop(wm, args):
    if not args:
        return wm.desk.name
    if args[0] == "new":
        return c_new_desktop(wm, args[1:])
    a = args[0]
    n = int(a) if a.isdigit() else a
    if isinstance(n, int) and n > len(wm.desktops) and n <= 9 and wm.opt("auto_create_desktops", True):
        while len(wm.desktops) < n:
            wm.add_desktop()
    wm._prev_desktop = wm.cur
    wm.switch_desktop(n)
    return wm.desk.name


@command("new-desktop", usage="new-desktop [name]", help="Create and switch to a new desktop", category="desktop")
def c_new_desktop(wm, args):
    d = wm.add_desktop(args[0] if args else None)
    wm._prev_desktop = wm.cur
    wm.switch_desktop(d)
    if wm.opt("desktop_start_window", True):
        wm.create_window({})
    return d.name


@command("close-desktop", usage="close-desktop [n]", help="Close a desktop and all its windows", category="desktop")
def c_close_desktop(wm, args):
    idx = wm.resolve_desktop(args[0]) if args else wm.cur
    wm.close_desktop(idx)


@command("rename-desktop", usage="rename-desktop <name>", help="Rename the current desktop", category="desktop")
def c_rename_desktop(wm, args):
    if not args:
        raise CommandError("usage: rename-desktop <name>")
    wm.desk.name = args[0]
    wm.dirty = True


@command("send-to-desktop", usage="send-to-desktop <n|name> [window]", help="Move a window to another desktop",
         completer=lambda wm, p, x: _c_desktops(wm, p, x), category="desktop")
def c_send_to_desktop(wm, args):
    if not args:
        raise CommandError("usage: send-to-desktop <n|name> [window]")
    w = wm.resolve_window(args[1] if len(args) > 1 else None)
    wm.send_to_desktop(w, int(args[0]) if args[0].isdigit() else args[0])


@command("list-desktops", usage="list-desktops", help="List desktops", aliases=("desktops",), category="desktop")
def c_list_desktops(wm, args):
    return "\n".join("%s%d %-10s layout=%-8s windows=%d" % ("*" if i == wm.cur else " ", i + 1, d.name, d.layout, len(d.windows))
                     for i, d in enumerate(wm.desktops))


# ----------------------------------------------------------------------------- UI
@command("theme", usage="theme <name|next|prev|list>", help="Switch the theme", completer=lambda wm, p, x: _c_themes(wm, p, x), category="ui")
def c_theme(wm, args):
    names = all_theme_names()
    if not args:
        return wm.theme.name
    a = args[0]
    if a == "list":
        return names
    if a in ("next", "prev"):
        i = names.index(wm.theme.name) if wm.theme.name in names else 0
        a = names[(i + (1 if a == "next" else -1)) % len(names)]
    try:
        wm.set_theme(a)
    except KeyError as e:
        raise CommandError(str(e.args[0]))
    return wm.theme.name


@command("prompt", usage="prompt [command|shell|search] [prefill...]", help="Open the command prompt in the status line", category="ui",
         completer=lambda wm, p, x: ["command", "shell", "search", "rename"] if not p else [])
def c_prompt(wm, args):
    mode = "auto"
    pre = ""
    if args and args[0] in ("command", "shell", "search"):
        mode = {"command": "command", "shell": "auto", "search": "search"}[args[0]]
        pre = " ".join(args[1:])
        if mode == "command" and len(args) == 2:
            pre += " "
    elif args:
        # "prompt rename foo": prefilled command mode (a lone word is a command name: keep the argument gap)
        mode = "command"
        pre = " ".join(args) + (" " if len(args) == 1 else "")
    wm.prompt.open(pre, mode)


@command("palette", usage="palette [all|commands|windows|desktops|themes|sessions]", help="Open the command palette overlay", category="ui",
         completer=lambda wm, p, x: ["all", "commands", "windows", "desktops", "themes", "sessions"])
def c_palette(wm, args):
    wm.palette.open(args[0] if args else "all")


@command("help", usage="help [topic]", help="Open the help window", aliases=("?",), completer=lambda wm, p, x: _c_topics(wm, p, x), category="ui")
def c_help(wm, args):
    topic = args[0] if args else "index"
    for w in wm.windows.values():
        if w.kind == "help":
            w.load(topic)
            wm.focus_window(w.id)
            return
    wm.create_window({"kind": "help", "topic": topic, "floating": True, "rect": _center_rect(wm, 0.8, 0.8)})


def _center_rect(wm, fw, fh):
    a = wm.content_area()
    w, h = int(a.w * fw), int(a.h * fh)
    return [a.x + (a.w - w) // 2, a.y + (a.h - h) // 2, w, h]


@command("log", usage="log [level]", help="Open the log window", completer=lambda wm, p, x: ["debug", "info", "warning", "error"], category="ui")
def c_log(wm, args):
    wm.create_window({"kind": "log", "level": args[0] if args else "info", "floating": True, "rect": _center_rect(wm, 0.9, 0.5)})


@command("new-status", usage="new-status", help="Open a status viewer window", category="ui")
def c_new_status(wm, args):
    return {"id": wm.create_window({"kind": "status"}).id}


@command("new-viewer", usage="new-viewer [title]", help="Open a stdin/web input viewer window", category="ui")
def c_new_viewer(wm, args):
    return {"id": wm.create_window({"kind": "viewer", "title": " ".join(args) or "viewer"}).id}


@command("new-watch", usage="new-watch <path>", help="Open a directory watcher window", completer=lambda wm, p, x: _c_paths(wm, p, x), category="ui")
def c_new_watch(wm, args):
    if not args:
        raise CommandError("usage: new-watch <path>")
    return {"id": wm.create_window({"kind": "dirwatch", "path": args[0]}).id}


@command("new-chart", usage="new-chart <cpu|mem|load|net_rx|net_tx|disk|push> [line|area|bar|spark|gauge|hist]",
         help="Open a chart window", completer=lambda wm, p, x: (["cpu", "mem", "load", "net_rx", "net_tx", "disk", "push"] if not p else ["line", "area", "bar", "spark", "gauge", "hist"]),
         category="ui")
def c_new_chart(wm, args):
    return {"id": wm.create_window({"kind": "chart", "source": args[0] if args else "cpu", "chart": args[1] if len(args) > 1 else "line"}).id}


@command("debug", usage="debug [console|events|state]", help="Open the debugger/console window", category="debug",
         completer=lambda wm, p, x: ["console", "events", "state"])
def c_debug(wm, args):
    src = getattr(wm, "_source", "api")
    if src in ("api", "mcp", "script") and not wm.opt("allow_remote_debug", False):
        raise CommandError("debug console is only available from the local keyboard (set allow_remote_debug: true)")
    wm.create_window({"kind": "debug", "mode": args[0] if args else "console", "floating": True, "rect": _center_rect(wm, 0.85, 0.7)})


@command("eval", usage="eval <python expression>", help="Evaluate a python expression with `wm` in scope (needs allow_eval: true)", category="debug")
def c_eval(wm, args):
    if not wm.opt("allow_eval", False):
        raise CommandError("eval is disabled (set allow_eval: true in the config)")
    return repr(eval(" ".join(args), {"wm": wm}))


@command("viewer-set", usage="viewer-set <window> pause|follow|wrap|numbers|filter|exclude|highlight|clear [value]",
         help="Control a viewer window (scriptable)", category="ui",
         completer=lambda wm, p, x: _c_windows(wm, p, x) if not p else ["pause", "follow", "wrap", "numbers", "filter", "exclude", "highlight", "clear"])
def c_viewer_set(wm, args):
    if len(args) < 2:
        raise CommandError("usage: viewer-set <window> <option> [value]")
    w = wm.resolve_window(args[0])
    if w.kind != "viewer":
        raise CommandError("window %d is not a viewer" % w.id)
    w.option(args[1], " ".join(args[2:]) if len(args) > 2 else "toggle" if args[1] != "clear" else "")


@command("viewer-feed", usage="viewer-feed <window> <text...>", help="Push a line of text into a viewer window", category="ui")
def c_viewer_feed(wm, args):
    if len(args) < 2:
        raise CommandError("usage: viewer-feed <window> <text...>")
    w = wm.resolve_window(args[0])
    w.feed_bytes((" ".join(args[1:]) + "\n").encode())


@command("chart-push", usage="chart-push <window> <value>", help="Push a value into a chart window", category="ui")
def c_chart_push(wm, args):
    if len(args) < 2:
        raise CommandError("usage: chart-push <window> <value>")
    w = wm.resolve_window(args[0])
    if w.kind != "chart":
        raise CommandError("window %d is not a chart" % w.id)
    w.push(float(args[1]))


@command("status-set", usage="status-set <key> <value...> [--label L] [--style normal|accent|dim|ok|warn|err] [--ttl seconds]",
         help="Set a status line item", category="status")
def c_status_set(wm, args):
    label = None
    style = "normal"
    ttl = None
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--label" and i + 1 < len(args):
            label = args[i + 1]
            i += 2
        elif a == "--style" and i + 1 < len(args):
            style = args[i + 1]
            i += 2
        elif a == "--ttl" and i + 1 < len(args):
            ttl = float(args[i + 1])
            i += 2
        else:
            rest.append(a)
            i += 1
    if len(rest) < 2:
        raise CommandError("usage: status-set <key> <value...>")
    wm.set_status(rest[0], " ".join(rest[1:]), label, style, ttl)


@command("status-clear", usage="status-clear [key]", help="Remove status items", category="status")
def c_status_clear(wm, args):
    wm.clear_status(args[0] if args else None)


@command("progress", usage="progress <name> <current> [total] [label...]", help="Report progress (used by the internal pv)", category="status")
def c_progress(wm, args):
    if len(args) < 2:
        raise CommandError("usage: progress <name> <current> [total] [label]")
    wm.update_progress(args[0], float(args[1]), float(args[2]) if len(args) > 2 else 0, " ".join(args[3:]) if len(args) > 3 else "")


@command("statusline", usage="statusline top|bottom|off", help="Move or hide the status line", category="status",
         completer=lambda wm, p, x: ["top", "bottom", "off"])
def c_statusline(wm, args):
    if not args or args[0] not in ("top", "bottom", "off"):
        raise CommandError("usage: statusline top|bottom|off")
    wm.statusline.config["position"] = args[0]
    wm.relayout()


@command("mode", usage="mode normal|resize|move|scroll|copy", help="Switch key mode", category="ui",
         completer=lambda wm, p, x: ["normal"] + list(wm.keymap.cfg["modes"]))
def c_mode(wm, args):
    m = args[0] if args else "normal"
    if m != "normal" and m not in wm.keymap.cfg["modes"]:
        raise CommandError("unknown mode: %s" % m)
    wm.keymap.mode = m
    wm.keymap.prefix_active = False
    wm.dirty = True


@command("message", usage="message <text...>", help="Show a message in the status line", category="ui")
def c_message(wm, args):
    wm.message(" ".join(args), "normal", 4.0)


@command("keys", usage="keys", help="List key bindings", category="ui")
def c_keys(wm, args):
    return "\n".join("%-10s %-18s %s" % (t, k, v) for t, k, v in wm.keymap.bindings())


@command("effect", usage="effect <name|off|list> [...]", help="Background effect in unused screen areas; `effect matrix [katakana|ascii|binary|hex] [color]`, `effect ansi FILE|DIR [hold=15 scroll=4 pause=2 order=name|random align=center|top dim=1]`", category="ui",
         completer=lambda wm, p, x: (["off", "list", "matrix", "plasma", "starfield", "fire", "rain", "ansi"] if not p else
                                     ["katakana", "ascii", "binary", "hex"] if len(p) == 1 and p[0] == "matrix" else
                                     ["green", "red", "blue", "cyan", "amber", "white", "purple"] if len(p) == 2 and p[0] == "matrix" else []))
def c_effect(wm, args):
    eff = wm.extra.get("effects_command")
    if eff is None:
        wm.load_plugin_by_name("effects")
        eff = wm.extra.get("effects_command")
    if eff is None:
        raise CommandError("effects plugin not available")
    return eff(wm, args)


# ----------------------------------------------------------------------------- dialogs
@command("dialog", usage="dialog message|confirm|input|menu|close|list ...", help="Show a dialog",
         completer=lambda wm, p, x: ["message", "confirm", "input", "menu", "close", "list", "focus"] if not p else [], category="ui")
def c_dialog(wm, args):
    from .dialogs import ConfirmDialog, InputDialog, MenuDialog, MessageDialog
    if not args:
        raise CommandError("usage: dialog message|confirm|input|menu|close|list ...")
    kind, rest = args[0], args[1:]
    modal = True
    if "--modeless" in rest:
        modal = False
        rest = [r for r in rest if r != "--modeless"]
    if kind == "message":
        if not rest:
            raise CommandError("usage: dialog message <text> [title]")
        d = MessageDialog(wm, rest[1] if len(rest) > 1 else "Message", rest[0], modal=modal)
    elif kind == "confirm":
        if not rest:
            raise CommandError("usage: dialog confirm <question> [command-if-yes] [command-if-no]")
        d = ConfirmDialog(wm, "Confirm", rest[0], rest[1] if len(rest) > 1 else None, rest[2] if len(rest) > 2 else None, modal=modal)
    elif kind == "input":
        if not rest:
            raise CommandError("usage: dialog input <prompt> [command with {} placeholder] [default]")
        d = InputDialog(wm, "Input", rest[0], rest[2] if len(rest) > 2 else "", rest[1] if len(rest) > 1 else None, modal=modal)
    elif kind == "menu":
        if not rest:
            raise CommandError("usage: dialog menu <title> label=command ...")
        items = []
        for it in rest[1:]:
            label, _, cmd = it.partition("=")
            items.append((label, cmd or None))
        d = MenuDialog(wm, rest[0], items, modal=modal)
    elif kind == "close":
        if wm.dialogs.stack:
            wm.dialogs.stack[-1].finish(None)
        return
    elif kind == "focus":
        wm.dialogs.toggle_focus()
        return
    elif kind == "list":
        return [x.describe() for x in wm.dialogs.stack]
    else:
        raise CommandError("unknown dialog kind: %s" % kind)
    wm.open_dialog(d)
    return {"dialog": d.id}


@command("quit-dialog", usage="quit-dialog", help="Ask before quitting", category="session", hidden=True)
def c_quit_dialog(wm, args):
    from .dialogs import ConfirmDialog
    wm.open_dialog(ConfirmDialog(wm, "Quit", "Quit pytermwm and close all windows?", "quit -f", None, default_no=True))


# ----------------------------------------------------------------------------- routing
@command("route", usage="route <window> <out|err> <window:ID|stdin:ID|file:PATH|none|reset> [--mute] [--replace]",
         help="Re-route a window's stdout/stderr to another window's display or stdin, or a file, on the fly", category="io",
         completer=lambda wm, p, x: _c_windows(wm, p, x) if not p else (["out", "err"] if len(p) == 1 else ["window:", "stdin:", "file:", "none", "reset"]))
def c_route(wm, args):
    f, rest = parse_flags(args, {"--mute": 0, "--replace": 0}, intermixed=True)
    if len(rest) < 3:
        raise CommandError("usage: route <window> <out|err> <sink> [--mute] [--replace]")
    src = wm.resolve_window(rest[0])
    stream = rest[1]
    if stream not in ("out", "err"):
        raise CommandError("stream must be out or err")
    if stream == "err" and (src.source is None or src.source.tty):
        raise CommandError("window %d has no separate stderr (start it with new-window --pipe)" % src.id)
    sink = rest[2]
    if sink == "reset":
        wm.set_route(src, stream, None)
        return "route reset"
    if sink == "none":
        wm.set_route(src, stream, "discard", None, append=False)
        return "output discarded"
    kind, _, target = sink.partition(":")
    if kind in ("window", "stdin"):
        dst = wm.resolve_window(target)
        if dst.id == src.id:
            raise CommandError("cannot route a window to itself")
        wm.set_route(src, stream, "display" if kind == "window" else "input", dst.id, mute="--mute" in f, append="--replace" not in f)
    elif kind == "file":
        wm.set_route(src, stream, "file", os.path.expanduser(target), mute="--mute" in f, append="--replace" not in f)
    else:
        raise CommandError("sink must be window:ID, stdin:ID, file:PATH, none or reset")
    return "routed %d.%s -> %s" % (src.id, stream, sink)


@command("pipe", usage="pipe <src window> <dst window> [--mute]", help="Connect the output of one window to the input of another",
         category="io", completer=lambda wm, p, x: _c_windows(wm, p, x))
def c_pipe(wm, args):
    f, rest = parse_flags(args, {"--mute": 0}, intermixed=True)
    if len(rest) != 2:
        raise CommandError("usage: pipe <src> <dst> [--mute]")
    return c_route(wm, [rest[0], "out", "stdin:" + rest[1]] + (["--mute"] if "--mute" in f else []))


@command("routes", usage="routes", help="Show active routes", category="io")
def c_routes(wm, args):
    out = []
    for w in wm.windows.values():
        for stream, r in w.routes.items():
            out.append("%d.%s -> %s%s" % (w.id, stream, ", ".join("%s:%s" % s for s in r["sinks"]) or "-", "" if r["self"] else " (muted)"))
    return "\n".join(out) if out else "(no routes)"


# ----------------------------------------------------------------------------- session / server
@command("redraw", usage="redraw", help="Repaint the whole screen of every attached client (when the terminal shows garbage)", category="ui")
def c_redraw(wm, args):
    wm.redraw_requested = True
    wm.dirty = True


@command("detach", usage="detach", help="Detach this client (session keeps running)", category="session")
def c_detach(wm, args):
    wm.detach_requested = True


@command("quit", usage="quit [-f]", help="Close everything and end the session", aliases=("exit",), category="session")
def c_quit(wm, args):
    if wm.opt("confirm_quit", False) and "-f" not in args and getattr(wm, "_source", "") in ("key", "prompt", "palette"):
        return c_quit_dialog(wm, [])
    wm.quit_requested = True


@command("session-save", usage="session-save [file]", help="Save the layout/session to disk", category="session")
def c_session_save(wm, args):
    from .session import save_session
    path = save_session(wm, args[0] if args else None)
    wm.message("session saved: %s" % path, "ok")
    return path


@command("session-info", usage="session-info", help="Session information", category="session")
def c_session_info(wm, args):
    return wm.state()


@command("reload", usage="reload", help="Reload the YAML configuration now", category="config")
def c_reload(wm, args):
    from .config import reload_config
    return reload_config(wm)


@command("config-get", usage="config-get [key]", help="Show the active configuration (or one key)", category="config")
def c_config_get(wm, args):
    if not args:
        return wm.cfg
    cur = wm.cfg
    for part in args[0].split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise CommandError("no such config key: %s" % args[0])
    return cur


@command("source", usage="source <file>", help="Run commands from a file (one per line, # comments)", category="config",
         completer=lambda wm, p, x: _c_paths(wm, p, x))
def c_source(wm, args):
    if not args:
        raise CommandError("usage: source <file>")
    path = os.path.expanduser(args[0])
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise CommandError(str(e))
    n = 0
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        wm.run_command_line(ln, source="source", raise_errors=True)
        n += 1
    return "ran %d commands" % n


@command("commands", usage="commands", help="List all commands", category="config")
def c_commands(wm, args):
    return "\n".join("%-16s %s" % (c.name, c.help) for c in sorted(wm.commands.commands.values(), key=lambda c: c.name) if not c.hidden)


# ----------------------------------------------------------------------------- plugins / rules
@command("plugin", usage="plugin list|load|unload|reload [name]", help="Manage plugins", category="plugin",
         completer=lambda wm, p, x: ["list", "load", "unload", "reload"] if not p else (wm.plugins.names() if wm.plugins else []))
def c_plugin(wm, args):
    wm.ensure_plugins()
    if not args or args[0] == "list":
        rows = wm.plugins.describe()
        if getattr(wm, "_source", "api") not in wm.INTERACTIVE_SOURCES:
            return rows                    # API / MCP / scripts get the structured list
        out = []
        for r in rows:                     # prompt, palette, hotkeys: something a person can read
            if r.get("loaded"):
                extra = ", ".join(filter(None, [
                    "commands: " + " ".join(r["commands"]) if r.get("commands") else "",
                    "kinds: " + " ".join(r["kinds"]) if r.get("kinds") else "",
                    "segments: " + " ".join(r["segments"]) if r.get("segments") else ""]))
                out.append("%-10s loaded%s  %s" % (r["name"], " (built in)" if r.get("builtin") else "", extra))
            else:
                out.append("%-10s not loaded%s" % (r["name"], "  ERROR: %s" % r["error"] if r.get("error") else "  (plugin load %s)" % r["name"]))
        return "\n".join(out) or "no plugins"
    if len(args) < 2:
        raise CommandError("usage: plugin %s <name>" % args[0])
    if args[0] == "load":
        wm.plugins.load(args[1], {})
    elif args[0] == "unload":
        wm.plugins.unload(args[1])
    elif args[0] == "reload":
        wm.plugins.reload(args[1])
    else:
        raise CommandError("usage: plugin list|load|unload|reload [name]")
    return "ok"


@command("rule", usage="rule list|enable|disable|fire|reload [name]", help="Inspect and control automation rules", category="plugin",
         completer=lambda wm, p, x: ["list", "enable", "disable", "fire", "reload"] if not p else (wm.rules.names() if wm.rules else []))
def c_rule(wm, args):
    wm.ensure_rules()
    if not args or args[0] == "list":
        rows = wm.rules.describe()
        if getattr(wm, "_source", "api") not in wm.INTERACTIVE_SOURCES:
            return rows
        out = []
        for r in rows:
            if r.get("script"):
                out.append("%-22s script  %s" % (r["name"], ("ERROR: %s" % r["error"]) if r.get("error") else "ok"))
            else:
                out.append("%-22s %-8s trigger=%s fired=%d%s" % (r["name"], "on" if r["enabled"] else "off", r["trigger"], r["fired"],
                                                                 ("  ERROR: %s" % r["error"]) if r.get("error") else ""))
        return "\n".join(out) or "no rules"
    sub = args[0]
    if sub == "reload":
        from .config import reload_config
        return reload_config(wm)
    if len(args) < 2:
        raise CommandError("usage: rule %s <name>" % sub)
    if sub == "enable":
        wm.rules.set_enabled(args[1], True)
    elif sub == "disable":
        wm.rules.set_enabled(args[1], False)
    elif sub == "fire":
        wm.rules.fire(args[1])
    else:
        raise CommandError("usage: rule list|enable|disable|fire|reload [name]")
    return "ok"


@command("web-info", usage="web-info", help="Show the URL (with access token) of the web interface", category="session")
def c_web_info(wm, args):
    srv = getattr(wm, "server", None)
    web = getattr(srv, "web", None)
    if web is None:
        raise CommandError("the web interface is not enabled (config: web: {enabled: true} or start with --web PORT)")
    return {"url": web.url, "token": web.token, "port": web.port}
