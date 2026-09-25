"""Model Context Protocol server for pytermwm (JSON-RPC 2.0).

Transports:
* stdio (``pytermwm mcp``): newline-delimited JSON on stdin/stdout, talking to the session's control socket;
* HTTP (``pytermwm mcp --http PORT`` or the ``/mcp`` endpoint of the web interface).

The tools let an agent run any pytermwm command, open windows, type into them, read their contents,
wait for output and change layouts / status items / configuration.  Resources expose state, window
contents, the rendered screen, logs, the configuration and the help pages.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

Call = Callable[[dict], dict]        # request dict -> response dict (with "ok")


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
N = {"type": "number"}
WINDOW = {"type": ["string", "integer"], "description": "window id, name or title (default: focused window)"}

TOOLS: List[dict] = [
    {"name": "run_command", "description": "Run a pytermwm command line (e.g. 'layout grid', 'new-window -- htop', 'zoom'). "
     "Several commands can be separated with ';'. Use list_commands to discover commands.",
     "inputSchema": _obj({"line": S}, ["line"])},
    {"name": "list_commands", "description": "List every pytermwm command with usage and help.", "inputSchema": _obj({})},
    {"name": "get_state", "description": "Full state: desktops, layouts, windows, focus, theme.", "inputSchema": _obj({})},
    {"name": "list_windows", "description": "List windows with id, title, kind, size and status.", "inputSchema": _obj({})},
    {"name": "new_window", "description": "Open a window. spec keys: cmd (string or list), title, name, kind (term, pipe, file, text, log, "
     "viewer, chart, status, dirwatch, help), path (follow a file), desktop, floating, split ('h'/'v'), keep (keep after exit), cwd, env.",
     "inputSchema": _obj({"spec": {"type": "object"}}, ["spec"])},
    {"name": "close_window", "description": "Close a window (kills its process).", "inputSchema": _obj({"window": WINDOW}, ["window"])},
    {"name": "focus_window", "description": "Focus a window.", "inputSchema": _obj({"window": WINDOW}, ["window"])},
    {"name": "send_keys", "description": "Type into a window. Use 'text' for literal text (set enter=true to press Enter) or 'keys' for "
     "named keys (Enter, C-c, Up, Tab, M-x, F5, ...).",
     "inputSchema": _obj({"window": WINDOW, "text": S, "enter": B, "keys": {"type": "array", "items": S}})},
    {"name": "capture_window", "description": "Read the text currently shown by a window (set history=true for scrollback, lines=N for the last N lines).",
     "inputSchema": _obj({"window": WINDOW, "history": B, "lines": I})},
    {"name": "wait_for_output", "description": "Wait until a regex appears in a window's text (polls); returns the matching text or times out.",
     "inputSchema": _obj({"window": WINDOW, "pattern": S, "timeout": N}, ["pattern"])},
    {"name": "screenshot", "description": "The whole rendered screen (all windows, borders, status line) as plain text.", "inputSchema": _obj({})},
    {"name": "set_status", "description": "Set a status line item (shown in the status line).",
     "inputSchema": _obj({"key": S, "value": S, "style": {"type": "string", "enum": ["normal", "ok", "warn", "err", "dim", "accent"]}}, ["key", "value"])},
    {"name": "get_logs", "description": "Recent log lines of the session.", "inputSchema": _obj({"n": I, "level": S, "pattern": S})},
    {"name": "get_config", "description": "The YAML configuration text and the active settings.", "inputSchema": _obj({})},
    {"name": "set_config", "description": "Validate and apply a new YAML configuration (saved to the config file when one is in use).",
     "inputSchema": _obj({"text": S}, ["text"])},
    {"name": "list_rules", "description": "Automation rules and scripts with their status.", "inputSchema": _obj({})},
    {"name": "fire_rule", "description": "Trigger an automation rule manually.", "inputSchema": _obj({"name": S}, ["name"])},
    {"name": "list_plugins", "description": "Loaded and available plugins.", "inputSchema": _obj({})},
    {"name": "write_script", "description": "Save and load a python script (in the scripts directory next to the configuration).",
     "inputSchema": _obj({"name": S, "text": S}, ["name", "text"])},
    {"name": "get_selection", "description": "The text the user currently has selected (mouse or copy mode) and the paste buffer (what was last copied).",
     "inputSchema": _obj({})},
    {"name": "get_help", "description": "A help topic (index, keys, commands, layouts, windows, prompt, themes, config, sessions, automation).",
     "inputSchema": _obj({"topic": S})},
]


class McpServer:
    def __init__(self, call: Call, name: str = "pytermwm"):
        self.call = call
        self.name = name
        self.initialized = False

    # ------------------------------------------------------------ JSON-RPC plumbing
    def handle(self, msg: Any) -> Optional[Any]:
        """Handle one JSON-RPC message (or batch).  Returns the response (or None for notifications)."""
        if isinstance(msg, list):
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method = msg.get("method")
        mid = msg.get("id")
        is_note = "id" not in msg
        if method is None:              # a response from the client: ignore
            return None
        try:
            fn = getattr(self, "m_" + re.sub(r"[^a-z_]", "_", method.lower()), None)
            if fn is None:
                if is_note:
                    return None
                return self._error(mid, -32601, "method not found: %s" % method)
            res = fn(msg.get("params") or {})
            if is_note:
                return None
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        except _RpcError as e:
            return self._error(mid, e.code, str(e))
        except Exception as e:                     # never kill the transport
            return self._error(mid, -32603, "internal error: %s" % e)

    @staticmethod
    def _error(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    # ------------------------------------------------------------ lifecycle
    def m_initialize(self, p):
        want = p.get("protocolVersion")
        ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        self.initialized = True
        return {"protocolVersion": ver,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": __version__},
                "instructions": "pytermwm is a terminal window manager. Open windows with new_window, type with send_keys, "
                                "read with capture_window / wait_for_output, and use run_command for everything else."}

    def m_notifications_initialized(self, p):
        return None

    def m_ping(self, p):
        return {}

    # ------------------------------------------------------------ tools
    def m_tools_list(self, p):
        return {"tools": TOOLS}

    def m_prompts_list(self, p):
        return {"prompts": []}

    def _req(self, req: dict) -> dict:
        res = self.call(req)
        if not res.get("ok"):
            raise _ToolError(res.get("error", "failed"))
        return res

    def m_tools_call(self, p):
        name = p.get("name")
        args = p.get("arguments") or {}
        fn = getattr(self, "t_" + str(name), None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            raise _RpcError(-32602, "unknown tool: %s" % name)
        try:
            out = fn(args)
            text = out if isinstance(out, str) else json.dumps(out, indent=2, default=str)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except _ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except KeyError as e:
            return {"content": [{"type": "text", "text": "missing argument: %s" % e}], "isError": True}

    def t_run_command(self, a):
        r = self._req({"op": "command", "line": a["line"], "source": "mcp"})
        res = r.get("result")
        return "ok" if res is None else res

    def t_list_commands(self, a):
        return [{"name": c["name"], "usage": c["usage"], "help": c["help"]} for c in self._req({"op": "commands"})["commands"]]

    def t_get_state(self, a):
        return self._req({"op": "state"})["state"]

    def t_list_windows(self, a):
        return self._req({"op": "windows"})["windows"]

    def t_new_window(self, a):
        r = self._req({"op": "create", "spec": a["spec"]})
        return {"id": r["id"], "title": r["title"]}

    def t_close_window(self, a):
        self._req({"op": "close", "window": a["window"]})
        return "closed"

    def t_focus_window(self, a):
        self._req({"op": "command", "line": "focus %s" % _q(a["window"]), "source": "mcp"})
        return "focused"

    def t_send_keys(self, a):
        req = {"op": "send", "window": a.get("window")}
        if a.get("keys"):
            req["keys"] = a["keys"]
        elif "text" in a:
            req["text"] = a["text"]
            req["enter"] = bool(a.get("enter"))
        else:
            raise _ToolError("send_keys needs 'text' or 'keys'")
        return "sent %d bytes" % self._req(req)["sent"]

    def t_capture_window(self, a):
        return self._req({"op": "capture", "window": a.get("window"), "history": bool(a.get("history")),
                          "lines": a.get("lines")})["text"]

    def t_wait_for_output(self, a):
        try:
            rx = re.compile(a["pattern"], re.M)
        except re.error as e:
            raise _ToolError("bad regex: %s" % e)
        deadline = time.time() + min(float(a.get("timeout", 15)), 120.0)
        text = ""
        while True:
            text = self._req({"op": "capture", "window": a.get("window"), "history": True})["text"]
            m = rx.search(text)
            if m:
                line = text[max(0, text.rfind("\n", 0, m.start()) + 1):]
                return {"matched": True, "match": m.group(0), "context": "\n".join(line.split("\n")[:5])}
            if time.time() >= deadline:
                raise _ToolError("timed out waiting for %r; last text:\n%s" % (a["pattern"], "\n".join(text.split("\n")[-15:])))
            time.sleep(0.15)

    def t_screenshot(self, a):
        return self._req({"op": "frame"})["text"]

    def t_set_status(self, a):
        self._req({"op": "status_set", "key": a["key"], "value": a["value"], "style": a.get("style", "normal")})
        return "ok"

    def t_get_logs(self, a):
        from .logs import format_record
        recs = self._req({"op": "logs", "n": a.get("n", 50), "level": a.get("level"), "pattern": a.get("pattern")})["logs"]
        return "\n".join(x if isinstance(x, str) else format_record(x, color=False) for x in recs)

    def t_get_config(self, a):
        r = self._req({"op": "config_get"})
        return r["text"]

    def t_set_config(self, a):
        return self._req({"op": "config_put", "text": a["text"]})

    def t_list_rules(self, a):
        return self._req({"op": "rules"})["rules"]

    def t_fire_rule(self, a):
        self._req({"op": "command", "line": "rule fire %s" % _q(a["name"]), "source": "mcp"})
        return "fired"

    def t_list_plugins(self, a):
        return self._req({"op": "plugins"})["plugins"]

    def t_write_script(self, a):
        return self._req({"op": "script_put", "name": a["name"], "text": a["text"]})

    def t_get_selection(self, a):
        r = self._req({"op": "selection"})
        return {k: r[k] for k in ("selection", "window", "paste_buffer", "copy_mode")}

    def t_get_help(self, a):
        r = self._req({"op": "help", "topic": a.get("topic", "index")})
        return "%s\n\n%s" % (r["title"], r["text"])

    # ------------------------------------------------------------ resources
    def m_resources_list(self, p):
        res = [
            {"uri": "pytermwm://state", "name": "state", "description": "Session state (JSON)", "mimeType": "application/json"},
            {"uri": "pytermwm://frame", "name": "screen", "description": "The rendered screen as text", "mimeType": "text/plain"},
            {"uri": "pytermwm://logs", "name": "logs", "description": "Recent session log", "mimeType": "text/plain"},
            {"uri": "pytermwm://config", "name": "config", "description": "YAML configuration", "mimeType": "application/yaml"},
        ]
        try:
            for w in self._req({"op": "windows"})["windows"]:
                res.append({"uri": "pytermwm://window/%s" % w["id"], "name": "window %s: %s" % (w["id"], w.get("title", "")),
                            "description": "Text of window %s" % w["id"], "mimeType": "text/plain"})
        except _ToolError:
            pass
        return {"resources": res}

    def m_resources_templates_list(self, p):
        return {"resourceTemplates": [
            {"uriTemplate": "pytermwm://window/{id}", "name": "window", "description": "Text of a window (with scrollback)", "mimeType": "text/plain"},
            {"uriTemplate": "pytermwm://help/{topic}", "name": "help", "description": "Help topic", "mimeType": "text/plain"}]}

    def m_resources_read(self, p):
        uri = str(p.get("uri", ""))
        m = re.match(r"^pytermwm://([a-z]+)(?:/(.*))?$", uri)
        if not m:
            raise _RpcError(-32602, "bad uri: %s" % uri)
        kind, rest = m.group(1), m.group(2)
        mime = "text/plain"
        try:
            if kind == "state":
                text, mime = json.dumps(self._req({"op": "state"})["state"], indent=2, default=str), "application/json"
            elif kind == "frame":
                text = self._req({"op": "frame"})["text"]
            elif kind == "logs":
                text = self.t_get_logs({"n": 200})
            elif kind == "config":
                text, mime = self.t_get_config({}), "application/yaml"
            elif kind == "window" and rest:
                text = self._req({"op": "capture", "window": rest, "history": True})["text"]
            elif kind == "help":
                text = self.t_get_help({"topic": rest or "index"})
            else:
                raise _RpcError(-32602, "unknown resource: %s" % uri)
        except _ToolError as e:
            raise _RpcError(-32002, str(e))
        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


class _RpcError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class _ToolError(Exception):
    pass


def _q(v) -> str:
    import shlex
    return shlex.quote(str(v))


# ---------------------------------------------------------------------------- transports
def socket_call(session: str) -> Call:
    from .protocol import ControlClient

    def call(req: dict) -> dict:
        try:
            c = ControlClient(session)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            # the usual first-time problem with an MCP client (LM Studio, Claude Desktop, ...): the MCP server runs, the
            # pytermwm session it talks to does not. Say so plainly instead of "[Errno 2] No such file or directory".
            return {"ok": False, "error": "no pytermwm session %r is running (%s). Start one first: `pytermwm -s %s start` "
                                          "(or attach to it in a terminal); the MCP server does not start it"
                                          % (session, e.__class__.__name__, session)}
        try:
            return c.request(req, 60.0)
        finally:
            c.close()
    return call


def run_stdio(session: str, http_port: Optional[str] = None) -> int:
    call = socket_call(session)
    server = McpServer(call)
    if http_port:
        return run_http(server, int(http_port))
    stdin, stdout = sys.stdin, sys.stdout
    for stream in (stdin, stdout):                       # JSON-RPC over stdio: UTF-8 and bare "\n" on every platform
        try:
            stream.reconfigure(encoding="utf-8", newline="\n" if stream is stdout else None)
        except (AttributeError, ValueError):
            pass
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = McpServer._error(None, -32700, "parse error")
        else:
            resp = server.handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0


def run_http(server: McpServer, port: int, host: str = "127.0.0.1") -> int:
    import hmac
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = secrets.token_urlsafe(16)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_response(401)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                msg = json.loads(self.rfile.read(min(n, 4 << 20)).decode())
            except ValueError:
                msg = None
            resp = server.handle(msg) if msg is not None else McpServer._error(None, -32700, "parse error")
            if resp is None:
                self.send_response(202)
                self.end_headers()
                return
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((host, port), H)
    sys.stderr.write("pytermwm MCP over HTTP: http://%s:%d/  (Authorization: Bearer %s)\n" % (host, httpd.server_address[1], token))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
