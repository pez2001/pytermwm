# Changelog

All notable changes to pytermwm are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [1.0.2] - 2026-09-26

### Added
- `pytermwm version` also lists every running session with the version its server runs, and warns when it differs
  from the installed one (a session keeps running its old code after an upgrade until it is restarted); `--check`
  exits with 1 then. Attaching to such a session warns too.

### Fixed
- A status line message wider than the screen no longer blanks the whole status line; it is cut to fit.

## [1.0.1] - 2026-09-25

### Added
- `pip install pytermwm`: pytermwm is now published on PyPI. The `pytermwm` and `ptw` commands are installed with it.
- Themes `nes` (Famicom red, gold and cream), `matrix` (green phosphor) and `dos` (gray on black, `C:\>`).
- `screenshot [FILE.svg|.ans|.txt]` saves the whole screen as an SVG picture, ANSI art or plain text.
- `record-screen [FILE.cast]` / `record-screen-stop` record the whole screen (all windows, borders and the status
  line) as asciicast v2.
- `redraw` (`C-b C-l`) repaints every attached terminal from scratch.
- `scripts/make_media.py` generates the README screenshots and demo recording from a scripted session.
- `CONTRIBUTING.md`: how to report bugs, run the tests and send pull requests.
- `fps=N` for background effects (1-60, default 20): `effect matrix fps=15`, `effect: {name: plasma, fps: 10}`.
- Setting up pytermwm's MCP server in LM Studio and other MCP clients, with a debugging checklist
  ([docs/api.md](docs/api.md#lm-studio-and-other-mcp-clients)).

### Changed
- Background effects are much cheaper: at most 20 frames per second, far less redrawn per frame, and every frame is
  sent as one synchronized update. On a 200x55 screen `matrix` went from 37% to 7% CPU and `plasma` from 262% to 11%.
- `new-window -- PROG ARG...` (and `pytermwm run -- PROG ARG...`) starts the program directly with exactly these
  arguments instead of through the shell. A single argument is still a shell command line.

### Fixed
- While a background effect runs, the screen is no longer redrawn as fast as possible, and the cursor is no longer
  hidden and shown again on every frame (it looked like very fast blinking and made windows lag).
- MCP tools report "no pytermwm session ... is running" (and how to start it) instead of an internal error.
- Running `pytermwm` (or `python ptw.py`) inside a pytermwm window no longer attaches the session to itself and
  crashes the window manager: it is refused with a hint. Attaching to another session from inside needs `--nested`.
- Windows: programs in a window wrote to pytermwm's own redirected output (the daemon's log file) instead of
  the window.
- Windows: `pytermwm run -- python ...` failed in PowerShell, the default shell.
- The status line could disappear after reattaching until the terminal was resized.
- In the command palette, `effect none` ran `effect list` instead of stopping the background effect.
- An inactive window no longer shows the bell sign `␇` when its prompt only sets the window title, or the
  activity dot `●` when its shell redraws the prompt after a resize.

## [1.0.0]

- First public version.

[1.0.2]: https://github.com/pez2001/pytermwm/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/pez2001/pytermwm/compare/894be6ad8027229a99191ddf0380e96f63f5c09a...v1.0.1
[1.0.0]: https://github.com/pez2001/pytermwm/tree/894be6ad8027229a99191ddf0380e96f63f5c09a
