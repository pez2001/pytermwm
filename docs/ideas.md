# Code ideas (collected while working)

Implemented since (see docs/tickets/M6_agents_and_workflow.md): per-token permissions (PTW-064), `pytermwm up` project
files (PTW-065), session recording as asciicast + replay (PTW-066), notification bridge (PTW-067), VT parser fuzzing
(PTW-068), golden-frame tests (PTW-069), PuTTY-style mouse selection and clipboard (PTW-070), Windows support without pywinpty via ctypes ConPTY (PTW-061..063).

Unsorted ideas that came up during implementation. Not committed to; ordered roughly by value/effort within each group.

## Terminal core
- **Grapheme clusters and emoji width.** `ansi.Screen` uses per-codepoint widths; ZWJ sequences, flags and variation
  selectors should be measured per grapheme (a small table-driven implementation, no dependency).
- **Sixel / kitty graphics passthrough** for a focused window, composited only when the window is unobscured.
- **OSC 52 clipboard and OSC 8 hyperlinks** routed through the compositor so they work across windows and in the web UI
  (click to open, `copy-mode` writes to the browser clipboard).
- **Selection and copy mode** with mouse drag and vi keys, shared by the tty client and the web UI.
- **Scrollback search** (`/` in scroll mode exists as a prompt; add incremental highlighting and `n`/`N`).
- **Synchronized output (DEC 2026)** towards the client to remove tearing on big redraws.

## Layout and UX
- **Layout scripting**: let a script return rects (`def layout(rects, n, params)`) so users can define new engines
  without touching the core.
- **Named workspaces from a project file** (`pytermwm up` reads `.pytermwm.yaml` in a repo and creates the desktop:
  editor, tests, logs).
- **Window groups and tabs** (stack several windows in one tile with a tab strip).
- **Persistent per-window scrollback** in the saved session, not only the layout and command.
- **Prompt history sharing** across sessions (currently one file per state dir, last writer wins).
- **Touch-friendly web UI**: on-screen modifier keys and a soft keyboard row (Esc, Ctrl, Alt, arrows).

## Automation and agents
- **Rule debugger**: a "dry run against the last N lines of window X" button in the web UI's Automation tab.
- **Rule conditions on more than one trigger** (`all:` / `any:`) plus a `state:` store so rules can count or debounce.
- **MCP sampling / elicitation**: let a rule ask the connected agent for a decision ("build failed, want me to open the log?").
- **Per-token permissions**: read-only, "may type only in windows tagged agent", so a token given to an agent can be
  scoped. The `tag` window key already exists as a hook.
- **Session recording** (asciicast v2 per window) and replay in the web UI.
- **OpenTelemetry-style event export** of WM events to a file or MQTT topic.

## Web
- **Serve frames as binary + zlib** and only changed rows (today JSON diff per row) for slow links.
- **TLS and a real login page** (or `--unix-socket` for reverse proxies) so the port can be published safely.
- **Web terminal per window** (not just the composed screen) so one browser tab can show a single window at full size.
- **Config editor niceties**: schema-driven completion from `KNOWN_KEYS`/`WINDOW_KEYS` and inline hover docs.
- **Playwright run in CI** with `PTW_CHROMIUM`, plus screenshot golden files for the themes.

## Plugins
- **Plugin manifest** (`plugin.yaml`: name, version, config schema, permissions) so the Plugins tab can render a form
  and warn before a plugin gains network or subprocess access.
- **Kubernetes plugin** modelled on the docker one (`kubectl get pods`, logs, exec).
- **Git plugin**: status segment, `git-log` window, staged-diff viewer.
- **System bell / notification bridge**: a rule action `notify` using OSC 777 or `notify-send` when a window in the
  background finishes.
- **Theme gallery**: `theme preview` renders all built-in themes side by side in one float window.

## Quality
- **Fuzz the VT parser** with random byte streams and compare against a reference for crash-freedom (no reference needed
  for "never raises and screen invariants hold").
- **Golden-frame tests** for each layout and theme (text frames are stable and diffable).
- **Type hints checked with mypy in CI**; the modules are mostly annotated already.
- Windows support via `pywinpty` behind the same `PtySource` interface (kept out of the core on purpose).
