"""Online help topics (also rendered in the help window and by `pytermwm help`)."""
from __future__ import annotations

from typing import List, Tuple

TOPICS_STATIC = {
    "index": ("Overview", """\
pytermwm - a modern terminal window manager (think: modern tmux)

Topics (press n / p or the number to switch, q to close):
  0 index      this page
  1 keys       hotkeys and key modes
  2 commands   every command you can run (prompt, palette, CLI, API)
  3 layouts    tiling, grid, table, float and docking
  4 windows    window kinds, options, history, scrollbars, overflow
  5 prompt     the command prompt and palette
  6 themes     themes and border styles
  7 config     YAML configuration and live reload
  8 sessions   detach / attach / save / restore
  9 automation rules, scripting and plugins
 10 remote     CLI, HTTP API, web UI and MCP
 11 io         stdio routing, pipes and pv
 12 selection  mouse selection, copy mode, clipboard

Quick start
  M-Enter          new window            M-h/j/k/l   move focus
  M-f              fullscreen (zoom)     M-Space     next layout
  M-p              command palette       M-:         command prompt
  C-b d            detach                M-/         this help
"""),
    "layouts": ("Layouts", """\
Every desktop has its own layout engine; switch with `layout <name>` or M-Space.

  tile      recursive tiling tree. `split h|v` splits ANY window (also inside
            sub tiles). resize with M-r then h/j/k/l, `balance` equalises.
  master    one/more master windows on the left, the rest stacked on the right
            (`master-ratio`, `master-count`).
  spiral    fibonacci spiral.      columns / rows   equal columns or rows.
  centered  master in the middle, stacks on both sides.
  monocle   only the focused window is shown.
  grid      automatic grid (`layout-set grid_cols N`).
  table     rows x columns with weights and cell spans:
              layout-set cols [1,2,1]      layout-set rows [1,1]
              layout-set cells {"logs":[0,0,1,3]}   (row,col,rowspan,colspan by window name)
  float     every window has a free rectangle (drag titles with the mouse).

Independent of the engine:
  float [on|off|toggle]   a window floats above the layout
  dock left|right|top|bottom|off [size]   dock a window to an edge; the rest
                                           of the layout uses the remaining area
  zoom                    fullscreen the focused window
"""),
    "windows": ("Windows", """\
A window is a process on a pty (default: your shell), a piped process, a file /
FIFO follower or an internal view. Create with `new-window` (M-Enter):

  new-window [-n name] [-t title] [-d desktop] [-c cwd] [--float] [--pipe] [-p file]
             [-k kind] [-- command...]

Kinds: term (default), pipe, file, text, help, log, status, viewer, dirwatch,
chart, debug + plugin kinds (docker.containers, btop.cpu, mqtt.monitor, ...).

Options (`window-set OPTION VALUE`):
  border on|off        scrollbar auto|on|off   overflow wrap|clip|ellipsis
  cp437 on|off         history N               on_exit close|keep|restart
  shadow, follow, readonly, icon, stderr_color
`vsize 200x60` sets a virtual size larger than the window: scroll with the
mouse wheel / M-[ mode. `vsize auto` follows the window again.
CP437: `cp437 toggle` (M-c) converts old DOS/BBS art on the fly to UTF-8.
"""),
    "prompt": ("Prompt & palette", """\
M-: opens the command prompt IN the status line.
  text          runs as a shell command in a NEW window (status line shows the result)
  :command      runs a pytermwm command (e.g. `:layout grid`)
Tab completes commands, arguments, executables and paths; the grey text is a
history suggestion (Right/End accepts it). Up/Down browse history.

M-p opens the command palette: a temporary overlay with fuzzy search over
commands, windows, desktops and themes. Enter runs, Tab completes.
"""),
    "themes": ("Themes", """\
`theme next` / `theme NAME`. Builtin: default light modern hacker bbs mc c64 amiga nes matrix dos.
Define your own in the config:

  themes:
    mine:
      extends: default
      border: heavy
      focus_border: double
      colors: {focus_border_fg: "#ff8800"}

Border sets: %s
Options: title_decor, title_pad, focus_marker, titlebar, gadgets, shadow,
dim_unfocused, gradient, status_style (plain|pill|brackets), desktop_char.
"""),
    "config": ("Configuration", """\
YAML file (first found): $PYTERMWM_CONFIG, ./pytermwm.yaml, ~/.config/pytermwm/config.yaml.
Every change is applied immediately (invalid files are rejected and the
old state stays; see the log window). `include:` merges more files.
`pytermwm init-config` writes a commented starter file; the web UI has an editor.
Windows listed under `windows:` are kept in sync by name.
"""),
    "sessions": ("Sessions", """\
The server owns all processes, so sessions survive closing the terminal.
  pytermwm                start (or attach to) session `default`
  pytermwm -s work        named session       pytermwm ls    list sessions
  C-b d                   detach              pytermwm attach [-s name]
  pytermwm kill -s work   end a session
  session-save / pytermwm restore   save and restore the layout and commands
Autosave writes to ~/.local/state/pytermwm/sessions.
  pytermwm up             build desktops and windows from .pytermwm.yaml (safe to repeat)
  record -t WIN [file]    record a window as asciicast;  record-stop;  replay FILE plays one
"""),
    "selection": ("Selection & clipboard", """\
Mouse (PuTTY style): drag in a window to select, the text is copied on release.
  double click = word, triple click = line, Alt+drag = rectangle
  middle / right click = paste       typing or a plain click clears
Programs that use the mouse (vim, htop) keep it: hold Alt to select there.
Keyboard: M-y (C-b y) copy mode: h j k l, PgUp/PgDn, g G 0 $ w b, v / V / C-v start
char / line / rectangle, y or Enter copies, Esc quits.   M-v (C-b ]) pastes.
Commands: copy-mode copy-selection select-all paste clipboard [show|list|set]
Clipboard: paste buffer + OSC 52 to the terminal. PuTTY ignores OSC 52, so press
M-Y (C-b Y) for the COPY VIEW: only this window's text, mouse off, so PuTTY's own
drag-to-copy gets clean text. Any key returns.
Config: selection: {copy_on_release, modifier, paste_buttons, osc52, command}
"""),
    "automation": ("Automation & plugins", """\
Rules in the config react to events:

  rules:
    - name: zoom-on-error
      when: {window: build, output_matches: "ERROR (.*)"}
      do: ["focus $window", "zoom on", "message $1"]
      undo_after: 30
Triggers: output_matches, output_changed, idle, exit, interval, event, status.
Variables: $window $title $line $1..$9 $code $key $value.
Actions are any pytermwm commands; `shell: ...` runs a shell command.
`notify MESSAGE` shows a notification (status line, web UI, desktop via OSC 777 or notify.command).
Python scripts (scripts: [file.py]) and plugins (plugins: [docker, btop, ...])
can register commands, status segments, window kinds and hotkeys.
"""),
    "remote": ("Remote control", """\
  pytermwm ctl COMMAND...       run any command on the session
  pytermwm ls|new|send|capture  shortcuts;    pytermwm mcp   MCP server (stdio)
HTTP API + web UI: enable `web:` in the config, open http://127.0.0.1:8765/?token=...
  GET /api/state   POST /api/command {"command": "layout grid"}   GET /api/frame
  GET /api/windows/ID/text    POST /api/windows/ID/input {"keys": "ls Enter"}
MCP: POST /mcp (streamable HTTP) or `pytermwm mcp` (stdio) - tools to list/create
windows, send input, read output and run commands.
web.tokens: extra tokens with scope `read` (view only) or `agent` (drive windows tagged `agent`).
"""),
    "io": ("Pipes & pv", """\
  cmd | pytermwm pipe [-t title]     show any stream in a viewer window
  pytermwm pv < file > out           progress viewer: shown in the status line
  route WIN out|err window:ID|stdin:ID|file:PATH|none|reset   re-route output on the fly
  pipe SRC DST                       feed SRC's output into DST's stdin
Windows created with --pipe have a separate stderr that can be routed too.
"""),
}


def topic_names(wm) -> List[str]:
    order = ["index", "keys", "commands", "layouts", "windows", "prompt", "themes", "config", "sessions", "automation", "remote", "io"]
    return order


def get_topic(wm, name: str) -> Tuple[str, str]:
    name = name or "index"
    if name == "keys":
        lines = ["Key bindings (prefix: %s)" % (wm.keymap.prefix if wm else "C-b"), ""]
        if wm:
            cur = None
            for tbl, key, cmd in wm.keymap.bindings():
                if tbl != cur:
                    lines.append("")
                    lines.append({"direct": "Direct (no prefix):", "prefix": "After the prefix:"}.get(tbl, tbl + " mode:"))
                    cur = tbl
                lines.append("  %-16s %s" % (key, cmd))
        return "Keys", "\n".join(lines)
    if name == "commands":
        lines = ["All commands (run with M-: then :cmd, the palette, `pytermwm ctl`, HTTP or MCP)", ""]
        if wm:
            by_cat = {}
            for c in wm.commands.commands.values():
                if not c.hidden:
                    by_cat.setdefault(c.category, []).append(c)
            for cat in sorted(by_cat):
                lines.append(cat.upper())
                for c in sorted(by_cat[cat], key=lambda c: c.name):
                    lines.append("  %s" % c.usage)
                    if c.help:
                        lines.append("      %s" % c.help)
                lines.append("")
        return "Commands", "\n".join(lines)
    if name == "themes":
        from .theme import BORDER_SETS
        title, text = TOPICS_STATIC["themes"]
        return title, text % ", ".join(sorted(BORDER_SETS))
    if name in TOPICS_STATIC:
        return TOPICS_STATIC[name]
    for n in topic_names(wm):
        if n.startswith(name):
            return get_topic(wm, n)
    return "Unknown topic", "No help topic %r.\nAvailable: %s" % (name, ", ".join(topic_names(wm)))


def all_help_text(wm) -> str:
    out = []
    for n in topic_names(wm):
        t, body = get_topic(wm, n)
        out.append("=" * 70)
        out.append(t)
        out.append("=" * 70)
        out.append(body)
    return "\n".join(out)
