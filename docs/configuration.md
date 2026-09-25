# Configuration

The file is YAML. It is looked up in this order: `-c FILE`, `$PYTERMWM_CONFIG`, `./pytermwm.yaml`,
`<xdg config>/pytermwm/config.yaml` (`~/.config/pytermwm/config.yaml`). `pytermwm init-config` writes a commented
starter; `pytermwm check-config [FILE]` validates without applying (also available in the web UI as you type).

**Live reload:** the running session watches the file. A valid change is applied at once (windows reconciled by
`name`, keys, theme, status line, rules, plugins). An invalid change is rejected with a message in the status line and
log and the previous configuration stays active. `reload` forces a reload; `include:` merges other files first and the
including file wins.

## Top-level keys

| key | default | meaning |
|---|---|---|
| `theme` | `default` | a built-in (`default light modern hacker bbs mc c64 amiga nes matrix dos`), a name from `themes:`, or an inline mapping |
| `themes` | | mapping `name -> theme` (colors, border sets, decorations); see `pytermwm/theme.py` `DEFAULTS` for every option |
| `layout` | `tile` | initial layout of desktops: `tile master spiral columns rows grid centered monocle table float` |
| `desktops` | `[main]` | list of names, or of mappings `{name, layout, ...params}` |
| `history` | 2000 | scrollback lines per window |
| `gap` | 0 | cells between tiled windows |
| `master_ratio`, `master_count` | 0.55, 1 | parameters of the master layouts |
| `new_window_position` | `end` | where new windows enter the layout |
| `on_last_close` | `exit` | `exit` / `respawn` / `keep` when the last window closes |
| `prompt_takeover` | 2.0 | seconds the status line shows command feedback |
| `mouse` | true | mouse support |
| `shell` | `$SHELL` | default shell for new windows |
| `cwd` | | default working directory |
| `window_defaults` | | default window options, e.g. `{border: true, scrollbar: auto, overflow: wrap}` |
| `confirm_quit` | false | ask before quitting |
| `statusline` | | `{position: top\|bottom\|off, left: [...], center: [...], right: [...]}` segments |
| `keys` | | `prefix`, `direct`, `prefix_table`, `modes` (see below) |
| `windows` | | windows kept in sync with the file |
| `rules`, `scripts` | | automation, see [automation.md](automation.md) |
| `plugins` | | list of names or `{name: ..., options}` mappings, see [plugins.md](plugins.md) |
| `plugin_dirs` | | extra directories searched for plugins |
| `web` | disabled | `{enabled, host, port, token, allowed_hosts, tokens}` |
| `notify` | | `{osc: true, command: "..."}`, see [Notifications](#notifications) |
| `selection` | | mouse selection and clipboard, see [Selection](#selection) |
| `effect` | | background effect: `matrix plasma starfield fire rain ansi` or empty for none; or a mapping: `{name: matrix, glyphs: katakana\|ascii\|binary\|hex, color: green\|red\|blue\|cyan\|amber\|white\|purple\|R,G,B}`, `{name: ansi, path: ~/art, hold: 15, ...}` (see [ANSI art background](#ansi-art-background)) |
| `charts` | | `{glyphs: unicode\|blocks\|ascii}` for the CPU sparkline and other bars/gauges; see [Charts and the status line](user_guide.md#charts-and-the-status-line) |
| `log` | | `{level: debug\|info\|warning\|error}` |
| `autosave` | 30 | seconds between automatic session snapshots (0 disables) |
| `allow_eval` | false | enables `eval` and the debugger's python console |
| `allow_remote_debug` | false | additionally lets HTTP/MCP/scripts use `eval` |
| `include` | | list of files merged below this one |

Unknown keys are reported as warnings, wrong types as errors.

## Status line segments

`desktops layout mode title pv status cpu mem disk time` plus segments from plugins (`docker`, ...). `status` shows the
items set with `status-set`. Themes decide the look of the bar (plain, pills or brackets).

## Keys

```yaml
keys:
  prefix: C-b
  direct:               # no prefix needed
    M-Enter: new-window
    M-h: focus left
  prefix_table:
    c: new-window
  modes:                # named modes, entered with `mode NAME`
    resize: {h: resize left, l: resize right, Esc: mode normal}
```

Key names: letters, `Enter Tab Space Esc Backspace Delete Insert Home End PageUp PageDown Up Down Left Right F1..F12`,
modifiers `C-` `M-` (alt) `S-`, e.g. `C-M-x`. A value is any command line.

## Windows

```yaml
windows:
  - name: build            # the identity used for reconciliation on reload
    cmd: make watch        # string or list; omit for the default shell
    desktop: dev
    keep: true             # do not close it when it disappears from the file
    title: build
    cwd: ~/src
    env: {CI: "1"}
    floating: false        # rect: [x, y, w, h] for floating windows
    dock: "left:30"       # edge[:size]; a [edge, size] list also works
    vsize: 200x60          # virtual size
    border: true           # scrollbar: auto|on|off, overflow: wrap|clip|ellipsis, cp437, history, shadow, on_exit
  - name: cpu
    kind: chart            # internal kinds: text help log status viewer dirwatch chart debug + plugin kinds
    source: cpu
    desktop: logs
```

Changing `cmd` of an existing named window restarts it only when `restart_on_change: true`; other options are applied
live. Windows created by hand are never touched by a reload.

## Web

```yaml
web:
  enabled: true
  host: 127.0.0.1      # keep it on localhost unless you know why not
  port: 8765
  token: change-me     # default: random, stored in the runtime dir; `pytermwm web` prints the URL
  allowed_hosts: [pytermwm.lan, "*.example.org"]   # extra Host header values accepted (patterns allowed)
  public_host: pytermwm.lan       # name shown in the URL that `pytermwm web` prints (default: this machine's name; also accepted)
  tokens:                         # extra tokens with less power (live: edits apply immediately)
    - {name: dashboard, token: "at-least-16-random-characters", scope: read}
    - {name: agent, token: "another-16+-character-secret", scope: agent}
```

`scope: read` may look (state, windows, window text, frames, events, logs, help) but not touch or see the config.
`scope: agent` may in addition create windows (always tagged `agent`) and type into or close windows whose `tag` is
`agent`. See [the security model](api.md#security-model). Generate a token with
`python -c "import secrets; print(secrets.token_urlsafe(24))"`.

## Notifications

`notify MESSAGE` (also usable as a rule action) shows the message in the status line, publishes a `notify` event (the
web UI shows it as a toast and, if the browser allows notifications, as a desktop notification) and sends OSC 777 to
attached terminals that support it (foot, WezTerm, VTE terminals, Ghostty ...).

```yaml
notify:
  osc: true                                  # forward to the terminal (default)
  command: "notify-send {title} {body}"      # optional: also run a program; values are shell-quoted for you
```

On macOS use `osascript -e 'display notification {body} with title {title}'`, on Windows a PowerShell toast
snippet. Example rule: `{when: {exit: error}, do: ["notify -s err -t 'build' 'window $name failed ($code)'"]}`.

## Selection

```yaml
selection:
  copy_on_release: true         # copy when the mouse button is released (PuTTY style); false: use copy-selection / y
  modifier: alt                 # alt | shift | ctrl | none: held to select in windows whose program uses the mouse
  paste_buttons: [middle, right]
  osc52: true                   # also send copies to the terminal clipboard (OSC 52, not supported by PuTTY)
  command: "wl-copy"            # optional: receives every copy on stdin (runs where the session runs)
  word_chars: "_-.~/%+=:@#?&"   # punctuation that counts as part of a word for double click
```

See the user guide's *Selection and clipboard* for behaviour. Copied text is never published in events and the `selection` operation and `get_selection` tool are unavailable to `read` and `agent` tokens.

## ANSI art background

`effect ansi FILE-OR-DIRECTORY [key=value ...]` shows ANSI or ASCII art behind and between the windows (`.ans .ansi .asc
.ascii .ice .nfo .diz .txt .art`; a directory is searched recursively). Files are read as UTF-8 or, if that fails, as DOS code
page 437, so classic BBS art with block characters, cursor movement, bright colours and a SAUCE record displays as intended. Art
is shown on black; a SAUCE iCE flag (or `ice: true`) turns blinking into a bright background.

```yaml
effect:
  name: ansi
  path: ~/art            # file or directory; without it a small built-in sample is shown
  hold: 15               # seconds per file (longer if scrolling takes longer)
  scroll: 4              # cells per second when the art is bigger than the screen (sideways: twice as fast)
  pause: 2               # seconds to rest at the start and end of a scroll
  order: name            # name | random
  align: center          # center | top: where art smaller than the screen sits
  dim: false             # darken it
  encoding: auto         # auto | cp437 | utf-8 | latin-1
```

Art smaller than the screen stays put. Art that is taller or wider scrolls down (and across) and, when it is the only file,
back up again; with several files each is shown in turn (scrolling once) and the directory is read again after the last one.
On the command line the same options are `key=value` words: `effect ansi ~/art hold=30 order=random dim=1`.

If a file renders with content in the wrong place (a common symptom: everything below some point in the picture looks
shifted), set `PYTERMWM_ANSIART_DEBUG=1` and check the logs (the `log` window kind, or the web UI's Logs tab -- see
[user_guide.md](user_guide.md#windows)). It reports the file's SAUCE record, every source line that ran past the
render width and had to autowrap onto extra row(s) below it -- the near-universal cause of that kind of shift, since
each such line pushes everything that follows it down by however many extra rows it needed -- and any escape sequence
pytermwm's terminal emulator did not recognise and ignored. This does not change what is rendered, only what gets
logged.

## Environment variables

`PYTERMWM_CONFIG`, `PYTERMWM_SESSION`, `PYTERMWM_PLUGIN_PATH`, `PYTERMWM_RUNTIME_DIR` (sockets, tokens),
`PYTERMWM_STATE_DIR` (saved sessions, recordings, prompt history) and `PYTERMWM_SHELL` (default shell) work on every
platform. Processes started by pytermwm see `PYTERMWM_SOCK`, `PYTERMWM_SESSION` and `PYTERMWM_WINDOW` so scripts inside
a window can call `pytermwm ctl ...`.

`PYTERMWM_CONFIG`, `PYTERMWM_RUNTIME_DIR`, `PYTERMWM_STATE_DIR` and `PYTERMWM_SHELL` override locations and the default
shell on every platform. `PYTERMWM_TCP=1` makes the session endpoint a loopback TCP port (always so on Windows) and
`PYTERMWM_THREADED_IO=1` uses reader threads instead of `select` on pipes (always so on Windows); both are for testing
the Windows code paths on Linux. Default config location: `~/.config/pytermwm/config.yaml` on POSIX,
`%APPDATA%\pytermwm\config.yaml` on Windows.
