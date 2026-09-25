# Milestones

| id | name | tickets | exit criteria |
|----|------|---------|---------------|
| M1 | Core engine | PTW-001 .. PTW-020 | Standalone WM runs bash windows in tiled/grid/float layouts, multiple desktops, hotkeys, full ANSI, history, scrollbars; unit tests green |
| M2 | Look & feel + UI | PTW-021 .. PTW-036 | Themes (default, hacker, bbs, mc, c64, amiga), status line, prompt, palette, completion, dialogs, YAML live reload, logging, help |
| M3 | Control plane | PTW-037 .. PTW-049 | Detach/attach/save/restore, CLI, HTTP API, MCP, scripting rules, plugin loader, pv, viewer/status/dirwatch/debug windows, charts |
| M4 | Web UI + plugins | PTW-050 .. PTW-058 | Web live view + editors, docker/btop/mqtt/ssh/effects/openai plugins |
| M5 | Docs & hardening | PTW-059 .. PTW-063 | Complete docs, full test suite, portable smoke test, Linux/macOS/Windows support, CI |
| M6 | Agents, workflow, quality | PTW-064 .. PTW-082 | Scoped tokens, project files (`up`), recording/replay, notifications, VT fuzzing, golden frames, mouse selection and clipboard |

Ticket files: `docs/tickets/M1_core_engine.md`, `M2_look_and_ui.md`,
`M3_control_plane.md`, `M4_web_and_plugins.md`, `M5_docs_and_hardening.md`, `M6_agents_and_workflow.md`.
Statuses: `todo` / `wip` / `done` / `partial` (with note).

Status: **M1-M6 done** (all 82 tickets). Evidence: unit/integration tests, `scripts/smoke.py`, Chromium UI test.
