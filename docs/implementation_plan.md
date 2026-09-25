# pytermwm - Implementation Plan

Goal: a Python based modern terminal window manager ("modern tmux") with
API / web / CLI / MCP interfaces and minimal dependencies (stdlib + PyYAML).

## Architecture

```
            +-------------------- pytermwm server (one per session) -------------------+
 tty client |  selector loop (single thread, RLock)                                     |
 (attach) --+->  Clients  -> KeyParser -> Keymap -> CommandRegistry -> WindowManager   |
 web (SSE)  |  Windows(pty/pipe/file/fd/internal) -> ansi.Screen (emulator + history)  |
 http API --+->  Desktops(layout engines) -> Compositor(theme, borders, statusline,    |
 mcp/cli  --+     dialogs, effects) -> frame diff -> per-client ANSI output            |
            |  Plugins / Scripting rules / Config watcher / Logging / Sessions         |
            +--------------------------------------------------------------------------+
```

Key decisions

1. **Server/client split** (like screen/tmux): the server owns ptys so sessions
   survive detaching. `--standalone` runs server + local tty client in one process.
2. **Own terminal emulator** (`ansi.py`) - windows are cell grids, so the compositor can
   draw borders, overlap floating windows, serve the same frame to tty and web.
3. **One command registry**: hotkeys, prompt, palette, CLI, HTTP, MCP, scripts and
   plugins all execute the same named commands.
4. **Stdlib only** + PyYAML. Web UI is static HTML/JS served by `http.server`.
5. **Everything is testable headless**: `VirtualClient` feeds keys and decodes the
   frames back through `ansi.Screen`.

## Package layout (`pytermwm/`)

| module | purpose |
|---|---|
| `ansi.py` | VT/ANSI parser, `Screen` (cells, colors, history, alt screen, wide chars) |
| `cp437.py` | on-the-fly CP437 -> unicode decoding |
| `colors.py` | color parsing, 256/16-color downgrade, SGR emit |
| `window.py` | `Window` + sources (process/pty, pipe, file, fd, internal), routing |
| `layout.py` | engines: tile tree, auto (master/spiral/columns/rows/monocle), grid, table, float, dock |
| `wm.py` | `WindowManager`, `Desktop`, focus, zoom, events |
| `commands.py` | command registry, parser, built-in WM commands |
| `keys.py` | key/mouse parser, keymaps, prefix mode |
| `theme.py` | theme engine, border glyph sets, builtin themes |
| `render.py` | compositor + diff to ANSI |
| `statusline.py` | segments + prompt sharing the bar |
| `prompt.py`, `completion.py` | line editor, history, autocompletion, palette |
| `dialogs.py` | modal / non-modal dialogs |
| `config.py` | YAML load/validate/apply + live reload |
| `logs.py` | integrated logging |
| `helpdata.py` | help topics |
| `server.py`, `client.py`, `protocol.py` | sessions, attach/detach, socket protocol |
| `session.py` | save / restore |
| `api.py`, `web/` | HTTP/JSON API, SSE live view, static web UI (`index.html`, `style.css`, `app.js`) |
| `mcp.py` | MCP server (stdio + HTTP) |
| `scripting.py` | rule engine + python hooks |
| `plugins.py`, `contrib/*` | plugin system; bundled plugins (docker, btop, mqtt, ssh, effects, openai) |
| `pv.py` | internal pipe viewer |
| `charts.py` | sparkline/bar/line/gauge primitives |
| `cli.py` | `pytermwm` command line tool |

## Phases

1. **M1 Core engine** - emulator, windows, layouts, desktops, compositor, keys, commands.
2. **M2 Look & feel** - themes/borders, status line, prompt, palette, completion, dialogs,
   YAML config live reload, logging, help.
3. **M3 Control plane** - sessions, CLI, HTTP API, MCP, scripting, plugin system, pv,
   viewer/status/dirwatch windows, charts, debugger.
4. **M4 Web + plugins** - web UI (live view, config editor, script/automation editor),
   docker, btop, mqtt, ssh, effects, openai plugins.
5. **M5 Docs & hardening** - documentation, test suite, smoke tests, Linux/macOS/Windows support.
6. **M6 Agents, workflow, quality** - scoped API tokens, project files (`pytermwm up`), asciicast recording/replay,
   notifications, VT-parser fuzzing, golden-frame tests, mouse selection with clipboard (tickets PTW-064..082, from docs/ideas.md).

Definition of done for a ticket: code merged, unit test (or documented manual check),
entry in docs, ticket status updated honestly in `docs/tickets/*.md`.

## Status (end of this run)

All five milestones are implemented; see `docs/milestones.md` and the ticket files. Verification: about 380 unit and
integration tests (`python -m unittest discover -s tests -t .`), `scripts/smoke.py` against the real daemon, and a
Chromium test of the web UI (`tests/test_web_ui.py`, skipped where playwright is missing). The documentation examples
are executed by `tests/test_docs.py`, so they cannot drift from the code. `tests/test_ansiart.py` additionally
cross-checks the VT100 emulator (`pytermwm/ansi.py`) against `pyte`, an independent reference implementation, when
that optional package is installed (skipped otherwise) -- this is how PTW-079 confirmed a "wrong line wrapping" bug
report against a real BBS ANSI art file was a quirk baked into that file's own escape sequences, not a pytermwm defect.

Known limits (candidates for follow-up, see `docs/ideas.md`): POSIX only (needs pty), no TLS in the built-in web
server, no bidirectional text or IME support, browser test not part of the default run.

## Platform support (PTW-061..063)

Linux, macOS and Windows 10 1809+. All OS specifics live in `pytermwm/compat.py` (dirs, shell, signals, endpoints,
threaded I/O pumps, console) and `pytermwm/winpty.py` (ConPTY via ctypes). Windows cannot `select` pipes/consoles, so
blocking handles are read by daemon threads that feed a socketpair; the session endpoint is a loopback TCP port with a
128-bit key. `PYTERMWM_THREADED_IO=1` / `PYTERMWM_TCP=1` run this model on Linux, so the whole suite covers it.
Verification: Linux (both modes) locally; real ConPTY/console via `ptw doctor` and the GitHub Actions matrix
(`.github/workflows/ci.yml`). Wrappers: `ptw`, `ptw.cmd`, `ptw.py`, `run_tests`, `run_tests.cmd`.
