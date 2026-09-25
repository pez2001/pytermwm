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
| `event` | event name (`window_created`, `window_closed`, `window_focus`, `desktop_switch`, `mqtt_message`, `config_applied`, ...) | `match: {field: regex}` |
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
