# User guide

## Concepts

* **Session** - a background server (`pytermwm -s NAME start`) that owns all ptys. Clients attach and detach; the
  processes keep running. `--standalone` skips the server. Several clients can attach to one session; the one you typed
  in last decides the size, the others see a cropped frame that keeps the status line.
* **Window** - a process on a pty (default: your shell), a piped process, a file/FIFO follower, or an internal view.
* **Desktop** - a named set of windows with its own layout. `M-1`..`M-9` switch, `M-!`.. move the focused window.
* **Layout** - how a desktop arranges its tiled windows; floating and docked windows sit on top or at the edges.
* **Command** - everything is a command from one registry: `layout grid`, `new-window -- htop`, `theme bbs`...
  Hotkeys, the prompt, the CLI, HTTP, MCP, rules and scripts all run the same commands.

## Keys

Direct keys (no prefix, i3-like): `M-Enter` new window, `M-h/j/k/l` focus, `M-H/J/K/L` move, `M-q` close, `M-f` zoom,
`M-t` float, `M-Space` next layout, `M-\` / `M--` split, `M-r` resize mode, `M-[` scroll mode, `M-p` palette,
`M-:` prompt, `M-/` help, `M-1..9` desktops, `M-c` toggle CP437. With the prefix (`C-b`) tmux-like keys also work:
`C-b c`, `C-b d` detach, `C-b x`, `C-b |`, `C-b -`, `C-b w` window list. Everything is rebindable under `keys:` in the
config; `help keys` shows the live table and `keys` the same on the command line.

The mouse focuses windows, scrolls history, selects text (see below), and is forwarded to applications that enable mouse
reporting.

## Selection and clipboard

pytermwm captures the mouse, so your terminal's own selection is switched off; pytermwm therefore has its own, modelled on PuTTY:

* **Drag** with the left button inside a window to select; the text is **copied when you release** the button. Selection stays inside that window (borders are never copied) and follows scrollback while output scrolls. Dragging above or below the window scrolls it.
* **Double click** selects a word (path characters like `/ . - _` count as part of it), **triple click** a line; keep dragging to extend by words or lines. **Alt+drag** selects a rectangle.
* **Middle or right click** pastes the paste buffer into the window (bracketed paste when the program asked for it).
* A program that uses the mouse itself (`vim`, `htop`, ...) keeps it. Hold **Alt** to select in such a window (`selection.modifier` changes the key; terminals reserve Shift for their own selection).
* Typing, or a plain click, clears the selection.

Keyboard copy mode (`M-y`, `C-b y` or `v` in scroll mode): move with `h j k l`, arrows, `PageUp/PageDown`, `C-u/C-d`, `g/G`, `0/$`, `w/b`; `v` or `Space` starts a selection, `V` selects lines, `C-v` a rectangle; `y` or `Enter` copies, `Esc` or `q` leaves. `M-v` (or `C-b ]`) pastes. Commands: `copy-mode`, `copy-selection`, `select-all`, `paste`, `clipboard [show|list|set TEXT]`.

**Where does the copy go?** Always into pytermwm's paste buffer (`M-v` pastes it in any window). In addition pytermwm sends an OSC 52 sequence so terminals that accept it (kitty, WezTerm, foot, Windows Terminal, iTerm2, tmux with `set-clipboard on`, xterm with `allowWindowOps`) put it on the system clipboard. **PuTTY does not support OSC 52**, so with PuTTY use one of these:

1. **Copy view** (`M-Y` or `C-b Y`, command `copy-view`): shows only the focused window's text at the top left, without borders and with mouse reporting off, so PuTTY's own drag-to-copy (copy on release) copies clean text into the Windows clipboard. `j k PageUp PageDown g G` scroll, any other key returns.
2. `selection.command`, a program that receives the text on stdin, for example `clip` on Windows, `wl-copy` or `xclip -selection clipboard` on Linux, `pbcopy` on macOS (this runs on the machine where the session runs, which is the remote host when you are logged in through PuTTY, so it does not help there).

You can test your terminal with `printf '\e]52;c;%s\a' $(printf hello | base64)` and then paste somewhere.

Known limits: soft-wrapped lines are copied with a line break at each wrap; the web UI does not have pytermwm selection yet (use the browser's own).

```yaml
selection:
  copy_on_release: true      # false: copy with y / copy-selection
  modifier: alt              # alt | shift | ctrl | none, used for windows whose program uses the mouse
  paste_buttons: [middle, right]
  osc52: true
  command: "xclip -selection clipboard"   # optional
```

## The prompt and palette

Commands that return text (`plugin list`, `effect list`, `keys`, `windows`, ...) show it: one line in the status line, longer output in a
floating text window (scroll with `j/k`, close with `q` or `Esc`).

`M-:` turns the **status line into a prompt**. Text is run as a shell command in a *new window*; `:cmd` runs a WM
command. Tab completes commands, arguments, executables and paths; grey text is a history suggestion (Right accepts);
Up/Down browse history and Ctrl-R searches it (history is kept in the state directory between runs). `M-p` opens the palette: fuzzy search over commands, windows, desktops and
themes.

## Layouts

`tile` (recursive splits, `split h|v`, `resize`, `swap`, `rotate`, `balance`), `master` / `spiral` / `columns` / `rows` /
`centered` / `monocle` (auto tilers, `master-count`, `master-ratio`), `grid`, `table` (row/column weights and cell
spans), `float`. Any window can float (`float toggle`, `float-move`, `float-resize`) or dock to an edge
(`dock left 30`). `gap N` adds space between tiles. Dragging a floating window by its title bar (or its bottom-right
corner to resize) temporarily replaces the title with its top-left position (while moving) or its content size in
columns x rows (while resizing), reverting to the real title on release.

## Windows

```
new-window [-n name] [-t title] [-d desktop] [-c cwd] [--float] [--pipe] [-p file] [-k kind] [-- command...]
window-set border on|off | scrollbar auto|on|off | overflow wrap|clip|ellipsis | cp437 on|off | history N | on_exit close|keep|restart
vsize 200x60          # virtual size larger than the window: scroll with wheel or M-[ ; `vsize auto` to follow again
```

Kinds: `term`, `pipe`, `file`, `text`, `help`, `log`, `status`, `viewer`, `dirwatch`, `chart`, `debug`, plus plugin kinds
such as `docker.containers` or `btop.cpu`.

Useful internal windows: `help [topic]`, `log`, `new-status` (all status items), `new-watch DIR` (change list),
`new-viewer` (a filterable, pausable, followable text viewer you can feed with `viewer-feed` or `cmd | pytermwm pipe`),
`new-chart cpu|mem|load|net_rx|net_tx|disk|push [line|area|bar|spark|gauge|hist]` (sparklines, bars, braille lines, gauges; `push` charts values you send with `chart-push`), `debug` (event tracer, state inspector; python console
needs `allow_eval: true`).

## Stdio re-routing

`route <window> out|err <window:ID|stdin:ID|file:PATH|none|reset> [--mute] [--replace]` re-routes a window's stdout or
stderr while it runs: to another window's display, to another window's *stdin*, or to a file. `routes` lists active
routes, `reset` restores the default. `pipe <src> <dst> [--mute]` is the short form for "feed src's output into dst's
input". From a shell, `journalctl -f | pytermwm pipe` shows stdin in a new viewer window.

## Progress reporting

`long-command | pytermwm pv` (or `pytermwm pv -s SIZE file`) shows a progress bar and rate in the status line of the
session while data flows; `pytermwm status KEY VALUE` and the `progress` / `status-set` commands let any script set status items.

## Sessions

```
pytermwm -s work start   # background session        pytermwm sessions   # list
pytermwm -s work attach  # attach (C-b d to detach)   pytermwm -s work kill
pytermwm -s work save|restore   # snapshot layout + windows; `autosave:` in the config does it periodically
```

Inside a pytermwm window (`$PYTERMWM_WINDOW` is set there) the commands that would take over the terminal refuse to
run: attaching to the session you are in would show the session inside itself without end. Attaching to another
session, or `--standalone`, works with `--nested`. Commands that only talk to the session (`run`, `send`, `ctl`,
`capture`, `status`, ...) work from inside as usual, and `pytermwm up` there builds the project into the current
session.

## Themes

`theme NAME` (or `theme next`, `C-b T`). Built in: `default light modern hacker bbs mc c64 amiga`. Define your own under
`themes:` in the config (or register one from a plugin with `api.theme`); a theme can select the border glyph set (rounded, heavy,
double, block, powerline, ascii), title decoration, colors, shadows, focus glow, gradient titles (truecolor), and a CP437
"BBS" look. Colors are downgraded automatically to what the terminal supports (truecolor/256/16).

## Charts and the status line

The CPU segment's sparkline, and other bars/gauges (task and download progress, chart windows), draw with fine 1/8-cell
Unicode block characters by default. Some fonts don't have them -- most notably the Linux virtual console's built-in
font, and often PuTTY's -- and show them as small boxes instead, even though the same font is otherwise fine. Attaching
from the actual Linux console is detected automatically (`TERM=linux`) and falls back to the four classic block shades
(`  ░▒▓█`), which are in virtually every terminal font ever made. PuTTY can't be detected this way (it reports itself as
an ordinary `xterm`), so set it by hand if you see the same boxes there:

```yaml
charts:
  glyphs: blocks   # unicode (default) | blocks (safe on the Linux console and most fonts) | ascii (no 8-bit characters at all)
```

`ascii` is the fallback of last resort for a terminal or font that doesn't even have the code page 437 shades. This
setting doesn't affect anything else pytermwm draws (borders, themes, ...), only sparklines and bars.

## Colour depth on limited terminals

Theme colors are truecolor and get downgraded automatically to what the attaching terminal supports (24-bit / 256 /
16 colors), detected the same way as the glyph fallback above. Two things follow from a 16-colour downgrade:

* **PuTTY** (and some other terminals) usually reports itself as a plain `xterm` with no `COLORTERM`, so it gets
  guessed down to 16 colors even when it can actually do 256 or truecolor -- the same detection gap as the chart
  glyphs. If themes look flat or wrong there, set the real depth by hand before attaching:

  ```sh
  PYTERMWM_DEPTH=truecolor pytermwm attach   # or: 256 / truecolor / 24 / 8 / 4 / 1 (mono)
  ```

* **The Linux virtual console** (`TERM=linux`) really is limited to 16 colors, and unlike PuTTY it renders those 16
  SGR codes through its own built-in palette, which is often quite different (and duller) than the colors pytermwm
  assumes when picking the closest of the 16 -- so different theme accents can collapse onto colors that all look
  similar, and specific colors can look outright wrong. To fix this at the source rather than just live with a poor
  16-color approximation, pytermwm reprograms the console's own palette (a private escape the Linux console alone
  understands) to match the RGB values it already assumes, so the existing "closest of 16" choice renders exactly as
  intended; it restores the console's normal palette when you detach. This is automatic for `TERM=linux` (never for a
  remote terminal like PuTTY, which can't set that) and can be turned off with `PYTERMWM_CONSOLE_PALETTE=0` if it
  ever causes trouble.

## Config and live reload

`pytermwm init-config` writes a commented starter to `~/.config/pytermwm/config.yaml`. The file is watched: save it and
the running session reconciles windows, keys, themes, rules and plugins **without a restart**. Errors are reported in the
status line and log; the previous good configuration stays active. See [configuration.md](configuration.md).

## Web UI

`pytermwm --web 8765` (or `web: {enabled: true}` in the config) then `pytermwm web` prints the URL. Tabs: **Screen**
(live, keyboard/mouse/paste), **Windows**, **Console** (any command, with completion), **Config** (validated editor),
**Automation** (rule table + rule builder), **Scripts**, **Plugins**, **Logs**. The browser sizes the session to itself
when no terminal is attached. When one *is* attached (a real console decides the size, e.g. `pytermwm attach`), the
Screen tab cannot resize the session, so it shrinks the font instead to make the fixed grid fit the window without a
scrollbar; uncheck **fit** next to the font size to turn that off and get a normal scrollbar at a fixed font size.

The **desktops** row above the screen is a taskbar-style switcher when more than one desktop is in use: click a
desktop to switch to it, double-click to rename it, use the **×** on a button to close it (and its windows, with a
confirmation if it has any), and **+ desktop** to create a new one (you're asked for an optional name). The
**Windows** tab's table has a **desktop** column with a dropdown per window to move it to another desktop, or to
`+ new…` to create one on the fly -- the same as the `desktop`, `new-desktop`, `rename-desktop`, `close-desktop` and
`send-to-desktop` commands (see [reference/commands.md](reference/commands.md#desktop)).

## Recipes

* Full-screen the build when it fails, back after 20 s: rule `when: {window: build, output_matches: "ERROR|FAILED"}`,
  `do: ["focus $window", "zoom on"]`, `undo_after: 20`.
* Dashboard desktop: `windows:` with `kind: chart` and `source: cpu|mem|disk|net`, layout `grid`.
* Follow a log: `new-window -k file -p /var/log/syslog`.
* Let an agent work in your terminal: `pytermwm mcp` in your agent's MCP config (see [api.md](api.md)).

## Running on Windows

Requirements: Windows 10 1809 or newer (ConPTY), Python 3.9+, `pip install -r requirements.txt`. Use Windows Terminal
(or any terminal with ANSI support). Start with `ptw.cmd` from the checkout, or `python -m pytermwm`.

* Run `ptw.cmd doctor` first; it starts a real pseudo console and tells you what is missing.
* Windows have no SIGWINCH, so the client polls the console size; resizing works but reacts within a quarter second.
  It also re-checks the size right after entering raw mode (some consoles only settle their reported window size once
  something makes them recompute it) and syncs the console's screen buffer to that window size, so attaching from a
  terminal that starts out bigger than the session's last size should no longer show garbled content until a manual
  resize; if you still see this, a resize (even a trivial one) fixes it, and a bug report with your terminal app
  (Windows Terminal, conhost, mintty, ...) would help track down whatever this doesn't yet cover.
* The default shell is `$PYTERMWM_SHELL`, else `pwsh`, `powershell`, then `cmd`. Set `shell:` in the config to override.
* Commands typed at the prompt keep backslashes: `run -- C:\tools\app.exe /flag` works as written.
* Files live in `%APPDATA%\pytermwm` (config) and `%LOCALAPPDATA%\pytermwm` (state, runtime); `XDG_*` variables override.
* Not available: load average, network counters (shown as zero), process CPU percentage.

## Project files

Put a `.pytermwm.yaml` in a repository and run `pytermwm up` from anywhere inside it: a session named after the
project is created (or reused), the desktops and windows below are built in it, and you are attached.

```yaml
name: myapp                   # session name (default: the directory name)
env: {APP_ENV: dev}           # added to every window
focus: editor                 # desktop to show afterwards
desktops:
  - name: editor
    layout: master
    windows:
      - {cmd: "nvim .", name: edit}
      - {cmd: "git status -sb", name: git}
  - name: run
    windows:
      - {cmd: "python -m pytest -f", name: tests}
      - {kind: file, path: logs/app.log, name: log}
```

`cwd` defaults to the project directory and relative `cwd` / `path` values are relative to it. Windows use the same
keys as `windows:` in the configuration. Running `up` again only adds what is missing (matching by desktop name and
window `name`), so it is safe to repeat; inside a running session use the `up` command. `pytermwm up --no-attach`
only builds the workspace.

## Recording and replay

`record` writes a window to an [asciicast v2](https://docs.asciinema.org/manual/asciicast/v2/) file (works with
asciinema players and `agg`):

```
record -t build                    # to <state dir>/recordings/<session>-<window>-<time>.cast
record -t build ~/demo.cast        # to a chosen file (refuses to overwrite one unless you add -f)
record-mark -t build "tests pass"  # add a marker
record-stop -t build
```

Only output is recorded; `record -i` also records keystrokes, which includes passwords, so use it deliberately.
`replay FILE [--speed 2] [--idle 2] [--loop]` (or a window of `kind: cast`) plays any asciicast v2 file in a window.
Keys in a replay window: Space pause, `+` / `-` speed, `r` restart, Left / Right skip five seconds, `q` close.
Pauses longer than `--idle` seconds are shortened.

## Notifications

`notify [-t TITLE] [-s ok|warn|err] MESSAGE` tells you something: status line, web UI toast, and a desktop
notification through your terminal (OSC 777) or a command of your choice. It is meant for rules, e.g. notify when a
window in the background finishes:

```yaml
rules:
  - name: job-finished
    when: {exit: true}
    do: ["notify -t finished 'window $name exited with $code'"]
```

See [configuration.md](configuration.md#notifications) for the `notify:` settings.
