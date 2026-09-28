# Automation: rules and scripts

## Rules (YAML)

```yaml
rules:
  - name: fullscreen-on-error
    when: {window: build, output_matches: "ERROR|FAILED"}
    do:
      - focus $window
      - zoom on
      - "message build failed: $line"      # quote actions that contain ": "
    undo_after: 20          # restore desktop / focus / zoom / layout after 20 s
    cooldown: 5
    once: false
```

`when` has exactly one **trigger**:

| trigger | value | extra keys |
|---|---|---|
| `output_matches` | regex matched against each output line (captures become `$1..$9`) | `window` (name, id, title glob, `*`) |
| `output_changed` | true | `window` |
| `idle` | seconds without output | `window` |
| `exit` | true / `ok` / `error` / an exit code | `window` |
| `interval` | seconds | |
| `event` | event name (`window_created`, `window_closed`, `window_focus`, `desktop_switch`, `mqtt_message`, `config_applied`, `dialog_result`, ...) | `match: {field: regex}` |
| `status` | status item key | one of `equals`, `matches`, `above`, `below` (edge-triggered) |

`do` is a list of actions:

* a WM command line (`layout grid`, `status-set build fail --style err`),
* `!command` runs a shell command (in the background, output to the log),
* `{command: ...}`, `{shell: ...}`, `{python: "..."}` (python needs `allow_eval`),
* `{delay: 3}` waits (non-blocking) before the following actions.

Variables usable in actions: `$window $title $name $line $1..$9 $code $event $rule $key $value $time`, and for `event` rules every simple field of the event (`$topic`, `$payload` for `mqtt_message`, ...). They are
**shell-quoted** before substitution, so a hostile output line cannot inject commands. Runaway protection: at most 20
fires per second and nesting depth 3. Rules can be listed, disabled, enabled and fired manually with
`rule list|enable|disable|fire NAME|reload`, in the web UI's Automation tab or with the MCP tools `list_rules` /
`fire_rule`.

## Dialogs: getting a result back

`dialog confirm|input|menu ...` (see `dialog` in the command reference) pops up a UI element for whoever is looking
at the session (attached terminal or web UI) to answer — it is not a synchronous prompt, so the command itself
returns immediately with just the dialog's id (`{"dialog": 3}`), before anyone has answered. To act on the answer,
give the dialog a command to run when it closes, instead of trying to read a result back from the `dialog` call:

```
dialog confirm "Deploy?" "run deploy.sh" "message cancelled"       # confirm: yes-command, no-command
dialog input "Version tag:" "status-set version {}" v1.0.0         # input: {} becomes what was typed
dialog menu "Environment" "prod=status-set env prod" "staging=status-set env staging"
```

That command runs inside the window manager, so route the answer somewhere your own tooling can read it back:
`status-set key value` (then poll it with `pytermwm ctl state` or `capture`), `!command` to write a file a script is
waiting on, or a rule/plugin action. Every answered (or cancelled) dialog also emits a `dialog_result` event
(`{dialog, result}`) on the internal event bus, so a rule can react to it directly:

```yaml
rules:
  - name: after-deploy-confirm
    when: {event: dialog_result}
    do: ["message dialog answered: $result"]      # the typed text, or the chosen button/item label
```

or a script can subscribe directly with `api.on("dialog_result", lambda dialog, result: ...)`. Either way, `$result`
(the `result` kwarg) is only set when the dialog was actually answered; on Esc/Ctrl-C the result is `None`, which for
a rule leaves `$result` unsubstituted in the action text, so check for that if cancelling matters to you.

`dialog list` (or the control op `dialogs`) shows what is currently open; a dialog drops off that list once answered.

If you'd rather retrieve the answer directly instead of routing it through a command or event, `dialog
message|confirm|input|menu` returns the new dialog's id (`{"dialog": 3}`); poll it with `dialog poll <id>`, which
returns `{"dialog": 3, "closed": false, "result": null}` while it's still open and `{"dialog": 3, "closed": true,
"result": ...}` once answered (or cancelled, with `result: null`). Recently-closed dialogs stay pollable for a while
after they close, so a poll issued right after the answer still sees it. From the CLI, `pytermwm dialog-wait <id>
[--timeout SECONDS]` does the polling for you and blocks until the dialog closes, printing its result as JSON:

```
id=$(pytermwm ctl dialog confirm "Deploy?" | python3 -c 'import sys,json; print(json.load(sys.stdin)["dialog"])')
pytermwm dialog-wait "$id" --timeout 60
```

## Python scripts

`*.py` files in `<configdir>/scripts/` (and any paths under `scripts:` in the config) run inside the session with `wm`
(the window manager) and `api` (a PluginAPI) in scope and an optional `setup(api)`; they are reloaded when the file
changes and unloaded cleanly (registered commands, hooks and timers are removed).

```python
def setup(api):
    api.command("hello", lambda wm, args: "hello from a script", help="say hello")

    def on_error(window, line, match):
        api.message("error in window %s: %s" % (window.id, line), "err")
    api.on_output(r"ERROR|Traceback", on_error)
    api.every(60, lambda: api.status("minute", str(int(__import__("time").time() // 60 % 60))))
```

Edit them in the web UI (Scripts tab; syntax errors are shown with the line) or with the MCP tool `write_script`. See
[plugins.md](plugins.md) for the full API.
