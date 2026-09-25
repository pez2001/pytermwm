# Plugins

A plugin is one Python file (or package) that receives a `PluginAPI`. Everything it registers - commands, status
segments, window kinds, keys, event hooks, output hooks, timers, themes, threads - is removed automatically when the
plugin is unloaded, and user plugins **hot-reload** when the file changes.

```yaml
plugins:
  - docker                       # a bare name
  - name: mqtt                   # or a mapping: everything except `name` is the plugin's config
    host: broker.local
    topics: [home/#]
```

`plugin list|load|unload|reload NAME` at run time; `plugin` also works from the web UI's Plugins tab and the MCP tool
`list_plugins`. Lookup order for `NAME`: `$PYTERMWM_PLUGIN_PATH` (colon separated), `plugin_dirs:` from the config,
`<configdir>/plugins/`, `~/.config/pytermwm/plugins/`, then the bundled `pytermwm.contrib`.

## Writing a plugin

```python
"""Shows the number of running containers and adds a `whoami` command."""


def setup(api):                       # api.config holds this plugin's config mapping
    api.command("whoami", lambda wm, args: "you are %s" % __import__("getpass").getuser(), help="print the user")
    api.segment("weather", lambda wm, opts: None)          # -> Segment or None; use it in statusline: lists
    api.key("M-w", "whoami")                               # bind a key to a command line
    api.on("window_created", lambda window=None, **kw: api.message("new window %s" % window.id))
    api.on_output(r"ERROR", lambda window, line, match: api.status("err", line[:40], "err"))
    api.every(30, lambda: api.status("tick", "30s"))
    api.on_unload(lambda: None)                            # optional cleanup (registrations are removed for you)
```

Save it as `~/.config/pytermwm/plugins/hello.py`, add `hello` to `plugins:` and it is live.

API (all methods of `PluginAPI`):

| method | purpose |
|---|---|
| `command(name, fn(wm, args), usage, help, aliases, completer)` | new command, usable from every interface |
| `segment(name, fn(wm, opts))` | status line segment |
| `window_kind(name, factory)` | new `kind:` for windows (`factory(wm, id, spec, rows, cols, opts)` returns an InternalWindow) |
| `key(key, command)` | bind a key |
| `on(event, fn)` | react to a WM event (`window_created`, `window_exit`, `desktop_switch`, `config_applied`, ...) |
| `on_output(regex, fn(window, line, match), window=None)` | react to output lines |
| `every(seconds, fn)` | timer, runs on the main loop |
| `theme(name, dict)` | register a theme |
| `status(key, value, style)` / `message(text, style, ttl)` / `run(command_line)` | talk to the WM |
| `thread(target)` + `call_soon(fn)` | run blocking I/O in a thread and hand results back to the main loop safely |
| `config`, `stop_event`, `data_dir()` | cooperative shutdown flag for threads, a private state directory |

Never touch the WM from a thread directly; use `api.call_soon`.

## Bundled plugins

| plugin | what | notable commands / config |
|---|---|---|
| `docker` | container/image/stats windows, logs, exec, run (uses the `docker` CLI) | `docker-ps docker-images docker-stats docker-logs docker-exec docker-run docker-do docker-inspect`; config `interval`, `bin`; segment `docker`; keys in the window: `j/k l e s r d i a q` |
| `btop` | per-core CPU, memory, network, disks, sortable/filterable process list | `btop [cpu\|mem\|pid\|name]`, `btop-kill <pid> [signal]`; kinds `btop.*` |
| `mqtt` | built-in MQTT 3.1.1 client (stdlib only), monitor window, publishing, status items, `mqtt_message` events for rules | config `host port topics username password tls status window`; `mqtt-pub mqtt-sub mqtt-unsub mqtt-status mqtt-window` |
| `ssh` | ssh windows with hosts from `~/.ssh/config` or the config, forwards, remote commands | `ssh <name\|[user@]host> [-- cmd]`, `ssh-run`, `ssh-forward`, `ssh-list`; config `hosts use_ssh_config reconnect` |
| `effects` | animated backgrounds (matrix, plasma, starfield, fire, rain, ANSI/ASCII art files) behind the windows | `effect <name\|off\|list> [...]` (e.g. `effect matrix ascii red`, `effect ansi ~/art`); or top-level `effect:` in the config |
| `openai` | chat with any OpenAI-compatible endpoint (OpenAI, Ollama, llama.cpp, vLLM, ...) with token streaming | `ai [--from WINDOW [--last N]] question`, `ai-window`, `ai-cancel`, `ai-clear`; config `base_url model api_key_env system max_context` |

```yaml
plugins:
  - docker
  - btop
  - name: openai
    base_url: http://localhost:11434/v1     # Ollama
    model: llama3.2
  - name: ssh
    hosts:
      prod: {host: prod.example.com, user: deploy}
```

Security notes: plugin code runs with your privileges, so only load code you trust. Container names, SSH targets and
API keys are validated (an SSH target starting with `-` is rejected and `--` separates options); API keys are read from
an environment variable and never appear in logs, state or window text.

Rule example using a plugin event:

```yaml
rules:
  - name: alarm
    when: {event: mqtt_message, match: {topic: "^alarm/", payload: "ON"}}
    do: ["message alarm on $topic", "float on"]
```
