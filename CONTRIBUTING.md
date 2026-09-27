# Contributing to pytermwm

Thanks for helping! Bug reports, fixes, themes, plugins and documentation are all welcome.

## Reporting a bug

Open an [issue](https://github.com/pez2001/pytermwm/issues) with:

- what you did, what you expected and what happened instead (a screenshot helps: `screenshot ~/bug.svg` saves one);
- the output of `pytermwm doctor` (OS, Python version, terminal, ConPTY/pty check);
- your terminal program (Windows Terminal, PuTTY, the Linux console, ...) and `TERM`;
- your config, if it matters (`pytermwm check-config` validates it).

The session log often has the answer: `pytermwm logs`, or `C-b l` inside pytermwm.

## Setting up

pytermwm is pure Python 3.9+ and needs only PyYAML. Nothing has to be installed to work on it:

```
git clone https://github.com/pez2001/pytermwm
cd pytermwm
pip install -r requirements.txt
./ptw --standalone                 # run your checkout (ptw.cmd or python ptw.py on Windows)
```

Or install it in editable mode (`pip install -e .`) to get the `pytermwm` and `ptw` commands for your checkout.

## Tests

Run the tests before you open a pull request; CI runs the same on Linux, macOS and Windows:

```
python run_tests.py                        # everything that can run here (Windows: the portable subset)
python run_tests.py -k palette -v          # only tests whose id contains "palette"
python run_tests.py --threaded-io --tcp    # the Windows I/O model, on Linux/macOS
python scripts/smoke.py                    # end to end: real daemon, CLI, HTTP, MCP
```

- **Bug fixes come with a test** that fails without the fix. Most tests drive a headless window manager
  (`tests/helpers.py`: `make_wm`, `type_keys`, `screen_of_frame`), so no real terminal is needed.
- **Never skip or disable a failing test** to get green; fix the cause. A test that fails only sometimes is a race:
  make it wait for the condition it checks.
- **Golden files** (`tests/golden/`) hold rendered frames. After a change that is meant to alter the output, regenerate
  them with `PTW_UPDATE_GOLDEN=1 python run_tests.py -k golden` and check the diff before you commit it.
- Windows: `run_tests.py --portable` is what CI requires; tests that need a POSIX shell are informational there.

## Generated files

Some files are generated from the code; regenerate them instead of editing them by hand:

| file | regenerate with |
|---|---|
| `docs/reference/commands.md`, `docs/reference/mcp.md` | `python scripts/gen_reference.py` |
| `docs/media/*` (README screenshots and `demo.cast`) | `python scripts/make_media.py` |
| `tests/golden/*` | `PTW_UPDATE_GOLDEN=1 python run_tests.py -k golden` |

## Code style

- Standard library only; a new runtime dependency needs a very good reason (PyYAML is the only one).
- Match the code around your change: its naming, comment density and idioms.
- Everything a user can do goes through the command registry (`@command` in `pytermwm/builtin_commands.py`), so it
  works the same from keys, the prompt, the CLI, HTTP and MCP. Give new commands a `usage` and `help` text.
- Anything that starts processes or touches files must work on Linux, macOS **and** Windows (see `pytermwm/compat.py`);
  never build shell command lines by joining strings, use `compat.quote_shell_arg` or pass an argv.
- Themes live in `pytermwm/theme.py` (`BUILTIN`); plugins are a single file with `setup(api)`, see
  [docs/plugins.md](docs/plugins.md).
- Don't commit build output or `__pycache__/` (`.gitignore` covers them).

## Pull requests

- One topic per pull request, with a description of what changed and why.
- Add a line for user-visible changes to the `Unreleased` section at the top of [CHANGELOG.md](CHANGELOG.md).
- Update the docs (`README.md`, `docs/`) when behaviour or options change.
- CI must be green before a merge.

## Releases

Maintainers release by setting `__version__` in `pytermwm/__init__.py` and moving the `Unreleased` changelog entries
under the new version; the release workflow then publishes the tag `vX.Y.Z` to PyPI. See "Releasing" in the
[README](README.md#releasing).

## License

pytermwm is licensed under the [GNU LGPL 2.1 or later](LICENSE). By contributing you agree that your contribution is
licensed under the same terms.
