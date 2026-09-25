# Control API

Everything the WM can do is reachable through the same operations, whatever the transport:

| transport | how |
|---|---|
| CLI | `pytermwm ctl "layout grid"`, `pytermwm ls / send / capture / state / logs / wait / frame` |
| unix socket | length-prefixed JSON (`pytermwm/protocol.py`), path from `PYTERMWM_SOCK` inside windows |
| HTTP + SSE | `pytermwm --web PORT` or `web: {enabled: true}`; `pytermwm web` prints the URL |
| MCP | `pytermwm mcp` (stdio), `pytermwm mcp --http PORT`, or `POST /mcp` on the web server |

Operations (`control.handle_op`): `command state windows capture send create close feed frame frame_json keys key
status_set progress logs events commands config_get config_validate config_put rules plugins themes help detach quit
dialogs script_get script_put resize ping selection`. Every response is `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.
Commands run through `wm.execute(line)` and return `{"ok", "result" | "error"}`.

## HTTP

```
GET  /                         web UI                         GET  /api/state, /api/windows, /api/frame
GET  /api/window/<id>/text?history=1&lines=200                GET  /api/commands, /api/themes, /api/plugins, /api/rules
GET  /api/config               {text, path, default, active}  POST /api/config/validate {text}
PUT  /api/config {text}        validate, save, apply live     GET  /api/logs?n=100&level=warning&pattern=regex
GET  /api/scripts              names                          GET|PUT /api/script/<name> {text}
POST /api/command {line}       run any command                POST /api/op {op, ...}  any operation
POST /api/window/<id>/send     {text|keys, enter}             DELETE /api/window/<id>
POST /api/input {data}         raw terminal input for the WM  POST /api/resize {cols, rows} (no terminal attached)
GET  /api/stream               server-sent events: `frame` (changed rows) and `event`
```

Examples:

```
TOKEN=$(pytermwm web | sed 's/.*token=//')
curl -H "Authorization: Bearer $TOKEN" localhost:8765/api/state
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"line": "new-window -- htop"}' localhost:8765/api/command
curl -N -H "Authorization: Bearer $TOKEN" localhost:8765/api/stream
```

`frame` events carry `lines`: one list of runs per row, each run `[text, fg, bg, flags, cells]` (colors are hex
without `#` or null; flags bit values BOLD 1, DIM 2, ITALIC 4, UNDERLINE 8, BLINK 16, REVERSE 32, HIDDEN 64,
STRIKE 128; `cells` is the display width so wide characters line up), plus `cols`, `rows`, `cursor`, `cursor_shape`
and `title`.

## MCP

```json
{ "mcpServers": { "pytermwm": { "command": "python", "args": ["-m", "pytermwm", "-s", "default", "mcp"] } } }
```

or over HTTP: `{"type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer <token>"}}`.
Protocol versions 2025-06-18, 2025-03-26 and 2024-11-05. Tools: see [reference/mcp.md](reference/mcp.md). Resources:
`pytermwm://state`, `pytermwm://frame`, `pytermwm://logs`, `pytermwm://config`, `pytermwm://window/{id}`,
`pytermwm://help/{topic}`. A typical agent loop: `new_window` -> `send_keys` (`enter: true`) -> `wait_for_output` ->
`capture_window`.

### LM Studio (and other MCP clients)

The MCP server talks to a running session; it does not start one. Start it first (`pytermwm -s default start`, or just
attach in a terminal), then add pytermwm under **Program → Install → Edit mcp.json** in LM Studio (0.3.17 or newer):

```json
{ "mcpServers": { "pytermwm": { "command": "pytermwm", "args": ["-s", "default", "mcp"] } } }
```

LM Studio does not start the server from your shell, so `pytermwm` (or `python`) may not be on its `PATH`: use the full
path (`where pytermwm` / `which pytermwm`; from a checkout: `"command": "C:\\Python312\\python.exe", "args":
["C:\\src\\pytermwm\\ptw.py", "-s", "default", "mcp"]`). Over HTTP instead: run `pytermwm mcp --http 8766`, which
prints a token, and use `{ "url": "http://127.0.0.1:8766/", "headers": { "Authorization": "Bearer <token>" } }`; the web
server's `/mcp` works the same with the web token (a scoped `agent` token is the safer choice). Use a model that supports
tool calling, and enable the pytermwm tools in the chat.

When it does not work:

* **Test the server without LM Studio:** `echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | pytermwm -s default mcp`
  must print one JSON line listing the tools. Only JSON-RPC goes to stdout; everything else goes to stderr.
* **"no pytermwm session 'default' is running":** start the session, or point `-s` at the one you use (`pytermwm sessions`).
  LM Studio must run as the same user, since sessions are per user.
* **Tools listed but nothing happens:** watch what the model sends in the session log (`pytermwm logs`, or `C-b l`), and
  try the same line yourself with `pytermwm ctl "layout grid"`.
* **LM Studio shows the server as failed:** its log (the MCP server entry in the Program tab, or Developer → logs) shows
  the command it ran and its stderr; a wrong path or Python version shows up there.

`selection` (MCP `get_selection`) returns the current selection and paste buffer; it needs a full-scope token because copied text may be sensitive.

## Security model

* The web server binds to `127.0.0.1`; every request needs the session token (random, compared in constant
  time). The token lives in a 0600 file in the runtime directory.
* `?token=` is accepted once on a page load and converted to an HttpOnly, SameSite=Strict cookie; the UI then removes
  it from the address bar.
* The `Host` header is checked (DNS rebinding): bound to loopback only loopback names and `web.allowed_hosts` (patterns like
  `*.lan` work) are accepted. When you bind to another address (`--web 0.0.0.0:8001`) the machine's own name, literal IP
  addresses, single-label names and private-network names (`.lan .local .home.arpa ...`) are accepted as well, since no
  other website can make a browser use those; anything else, e.g. `evil.com`, is answered with `bad host`. Cookie-authenticated
  writes need `Content-Type: application/json` and a same-origin `Origin` (CSRF). A strict CSP forbids inline and eval'd
  script; static files are served from the package directory only, with path-traversal protection.
* `eval`, the python console and `{python: ...}` rule actions are disabled unless `allow_eval: true`; HTTP, MCP and
  scripts additionally need `allow_remote_debug: true`.
* Rule variables are shell-quoted; `docker`/`ssh` plugin inputs are validated.
* Whoever holds the token can type into your terminals, so treat it like an SSH key. Do not expose the port beyond
  localhost without a TLS-terminating reverse proxy and `allowed_hosts`.

### Scoped tokens

Besides the session token you can hand out weaker ones in the configuration (`web.tokens`, see
[configuration.md](configuration.md#web)). They work everywhere the session token does (HTTP, SSE, `/mcp`) and are
checked on every request against the live config, so removing an entry revokes it immediately.

| scope | allowed |
|---|---|
| `read` | `state`, `windows`, `capture`, `frame`, `events`, `commands`, `rules`, `plugins`, `themes`, `dialogs`, `help`, `logs`, `ping`, the SSE stream. Never the config, scripts, input or commands. |
| `agent` | everything in `read`, plus `create` (the window is forced to `tag: agent`), and `send` / `close` on windows tagged `agent`. No commands, config, scripts, `feed`, detach or quit. |

Denied requests get HTTP 403 (`{"ok": false, "denied": true, "error": ...}`), MCP tools report the error. A scoped
token that arrives in a URL is turned into a cookie for *that* token, never for the session token. Give an AI agent an
`agent` token and tag the windows it may drive (`tag: agent` in the config or `new-window` spec).

Be clear about what `agent` means: it can open windows and type into them, so it can run any program as you. The scope
limits what it can *touch* (your other windows, the config, the session itself), not what its own windows can do. Use
`read` for anything that should only watch. Neither scope needs `allow_eval`; `eval` stays off for them regardless.
