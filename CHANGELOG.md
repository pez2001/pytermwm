# Changelog

All notable changes to pytermwm are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

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

### Changed
- `new-window -- PROG ARG...` (and `pytermwm run -- PROG ARG...`) starts the program directly with exactly these
  arguments instead of through the shell. A single argument is still a shell command line.

### Fixed
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

[1.0.1]: https://github.com/pez2001/pytermwm/compare/894be6ad8027229a99191ddf0380e96f63f5c09a...v1.0.1
[1.0.0]: https://github.com/pez2001/pytermwm/tree/894be6ad8027229a99191ddf0380e96f63f5c09a
