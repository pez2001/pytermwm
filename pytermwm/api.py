"""HTTP/JSON API + server-sent events + web UI + MCP endpoint (stdlib only).

Security model: the server binds to 127.0.0.1 by default and every request needs the session token
(``Authorization: Bearer <token>``, ``?token=`` on the first visit which sets a SameSite=Strict cookie).
The ``Host`` header is checked (DNS rebinding) and browsers' cross-origin POSTs are rejected.

Endpoints (all JSON unless noted)::

    GET  /                          web UI              GET  /api/state         session state
    GET  /api/windows               windows             GET  /api/frame         rendered screen (styled runs)
    GET  /api/window/<id>/text      window text         GET  /api/commands      command reference
    GET  /api/config                config text+active  POST /api/config/validate {text}
    PUT  /api/config {text}         validate+save+apply GET  /api/rules /plugins /themes /logs /scripts
    GET  /api/script/<name>         script text         PUT  /api/script/<name> {text}
    POST /api/command {line}        run a command       POST /api/op {op, ...}  any control operation
    POST /api/window/<id>/send      {text|keys,enter}   POST /api/input {data}  raw terminal input for the WM
    POST /api/resize {cols, rows}   resize a session with no attached terminal
    GET  /api/stream                SSE: `frame`, `event`  POST /mcp            MCP over HTTP (JSON-RPC)
"""
from __future__ import annotations

import fnmatch
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .control import handle_op, frame_json
from .mcp import McpServer

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 4 << 20


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):      # clients vanish all the time (closed tabs)
        pass


class WebServer:
    def __init__(self, wm, host: str, port: int, token: Optional[str], session: str):
        self.wm = wm
        self.host = host
        self.session = session
        self.token = token or self._load_token(session)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.requested_port = port
        self.port = port
        self._frame_lock = threading.Lock()
        self._frame_cache = (-1, "")
        self.mcp = McpServer(lambda req: handle_op(wm, req))
        self._scoped_mcp: dict = {}
        self.stopping = False

    def _load_token(self, session: str) -> str:
        from .protocol import token_path
        path = token_path(session)
        try:
            with open(path, encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) >= 16:
                return t
        except OSError:
            pass
        t = secrets.token_urlsafe(18)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(t + "\n")
        except OSError:
            pass
        return t

    def mcp_for(self, scope: Optional[str]) -> McpServer:
        if scope is None:
            return self.mcp
        if scope not in self._scoped_mcp:
            wm = self.wm
            self._scoped_mcp[scope] = McpServer(lambda req, _s=scope: handle_op(wm, req, _s))
        return self._scoped_mcp[scope]

    LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")
    WILDCARD = ("0.0.0.0", "", "::", "[::]")
    # names that only exist on private networks: a web page on the internet can never make a browser send these
    PRIVATE_SUFFIXES = (".lan", ".local", ".localdomain", ".home.arpa", ".internal", ".intranet", ".home", ".corp")

    def _web_cfg(self) -> dict:
        w = self.wm.cfg.get("web")
        return w if isinstance(w, dict) else {}

    def exposed(self) -> bool:
        """Listening on something other than loopback."""
        return self.host.lower() not in self.LOOPBACK

    def own_names(self) -> set:
        """Names this machine calls itself (hostname, fully qualified name and their short forms)."""
        if getattr(self, "_own_names", None) is None:
            names = set()
            for fn in (socket.gethostname, socket.getfqdn):
                try:
                    n = fn().lower().rstrip(".")
                except OSError:
                    continue
                if n:
                    names.add(n)
                    names.add(n.split(".")[0])
            self._own_names = names
        return self._own_names

    def host_allowed(self, host: str) -> bool:
        """Is this Host header acceptable?  This is the DNS-rebinding defence: a page from another site reaches us
        under *its* name, so only names that cannot belong to another site are let in."""
        h = host.lower().strip().rstrip(".")
        cfg = self._web_cfg()
        if h in self.LOOPBACK or h == self.host.lower():
            return True
        for pat in list(cfg.get("allowed_hosts") or []) + ([cfg["public_host"]] if cfg.get("public_host") else []):
            if fnmatch.fnmatchcase(h, str(pat).lower()):
                return True
        if not self.exposed():
            return False
        try:                                        # a literal IP address is not a name anybody else can point elsewhere
            ipaddress.ip_address(h.strip("[]"))
            return True
        except ValueError:
            pass
        return h in self.own_names() or "." not in h or h.endswith(self.PRIVATE_SUFFIXES)

    def allowed_hosts(self):
        """Explicit allow list (kept for callers; ``host_allowed`` is the real check)."""
        hs = set(self.LOOPBACK) | {self.host}
        hs.update(str(h) for h in (self._web_cfg().get("allowed_hosts") or []))
        return hs

    def display_host(self) -> str:
        """The name to put in URLs: ``web.public_host``, else the host we bind to, else (for 0.0.0.0) this machine's name."""
        pub = self._web_cfg().get("public_host")
        if pub:
            return str(pub)
        if self.host not in self.WILDCARD:
            return "[%s]" % self.host if ":" in self.host and not self.host.startswith("[") else self.host
        try:
            fq = socket.getfqdn()
            if fq and "." in fq and not fq.startswith("localhost"):
                return fq
            return socket.gethostname() or "127.0.0.1"
        except OSError:
            return "127.0.0.1"

    @property
    def url(self) -> str:
        return "http://%s:%d/?token=%s" % (self.display_host(), self.port, self.token)

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        server = self

        class Handler(_Handler):
            web = server

        self.httpd = _HTTPServer((self.host, int(self.requested_port)), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ptw-web", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping = True
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None

    # ---------------------------------------------------------------- shared frame for SSE clients
    def frame_json(self):
        wm = self.wm
        seq = getattr(wm, "frame_seq", 0)
        with self._frame_lock:
            if self._frame_cache[0] == seq and self._frame_cache[1]:
                return seq, self._frame_cache[1]
            with wm.lock:
                seq = getattr(wm, "frame_seq", 0)
                data = json.dumps(frame_json(wm), separators=(",", ":"))
            self._frame_cache = (seq, data)
            return seq, data


class _Handler(BaseHTTPRequestHandler):
    web: WebServer = None       # type: ignore
    protocol_version = "HTTP/1.1"
    server_version = "pytermwm"

    def log_message(self, fmt, *args):
        try:
            self.web.wm.log.debug("web: " + fmt, *args)
        except Exception:
            pass

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body: bytes, ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        hdrs = dict(headers or {})
        if getattr(self, "_set_cookie", False) and "Set-Cookie" not in hdrs and code < 400:
            hdrs["Set-Cookie"] = "ptw_token=%s; Path=/; HttpOnly; SameSite=Strict" % self._presented
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, default=str).encode(), headers=headers)

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw.decode())

    def _authorized(self, query) -> Optional[str]:
        """Return None if authorized, else the reason.  Sets self._set_cookie when the token came in the URL."""
        web = self.web
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
            else (self.headers.get("Host") or "").split("]")[0] + "]"
        if not web.host_allowed(host):
            return "bad host"
        tok = ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            tok = auth[7:].strip()
        via_cookie = False
        if not tok:
            q = query.get("token")
            if q:
                tok = q[0]
                self._set_cookie = True
        if not tok:
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ptw_token":
                    tok = v
                    via_cookie = True
        self._scope = None
        self._presented = tok
        if not tok:
            return "unauthorized"
        if not hmac.compare_digest(tok.encode("utf-8", "replace"), web.token.encode("utf-8")):
            from . import perms
            entry = perms.find_token(web.wm.cfg, tok)
            if entry is None:
                return "unauthorized"
            self._scope = entry["scope"]
        if self.command not in ("GET", "HEAD") and via_cookie:
            # cookie-authenticated writes must come from our own page
            origin = self.headers.get("Origin")
            if origin:
                oh = urllib.parse.urlparse(origin).netloc
                if oh != self.headers.get("Host"):
                    return "cross-origin request rejected"
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return "content-type must be application/json"
        return None

    def _handle(self):
        self._set_cookie = False
        self._scope = None
        self._presented = ""
        u = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(u.query)
        why = self._authorized(query)
        if why:
            return self._err(401 if why == "unauthorized" else 403, why)
        cookie = {"Set-Cookie": "ptw_token=%s; Path=/; HttpOnly; SameSite=Strict" % self._presented} if self._set_cookie else None
        path = urllib.parse.unquote(u.path)
        try:
            if path == "/mcp":
                return self._mcp()
            if path.startswith("/api/"):
                return self._api(path[5:].strip("/"), query)
            if self.command in ("GET", "HEAD"):
                return self._static(path, cookie)
            self._err(404, "not found")
        except (ValueError, json.JSONDecodeError) as e:
            self._err(400, "bad request: %s" % e)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.web.wm.log.error("web request failed: %s", e, exc_info=True)
            except Exception:
                pass
            self._err(500, "internal error: %s" % e)

    do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = _handle

    # ---------------------------------------------------------------- static / ui
    def _static(self, path, cookie):
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(WEB_DIR, rel)
        if not os.path.abspath(full).startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            return self._err(404, "not found")
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        hdrs = dict(cookie or {})
        if full.endswith(".html"):
            hdrs["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                                               "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self._send(200, data, ctype, hdrs)

    # ---------------------------------------------------------------- MCP
    def _mcp(self):
        if self.command != "POST":
            return self._err(405, "POST JSON-RPC here")
        msg = self._body()
        resp = self.web.mcp_for(self._scope).handle(msg)
        if resp is None:
            return self._send(202, b"")
        self._json(resp)

    # ---------------------------------------------------------------- API
    def _op(self, req: dict, code_on_error=400):
        res = handle_op(self.web.wm, req, self._scope)
        self._json(res, 200 if res.get("ok") else (403 if res.get("denied") else code_on_error))

    def _api(self, route: str, query):
        m = self.command
        wm = self.web.wm
        parts = route.split("/")
        head = parts[0]
        if head == "stream":
            return self._stream(query)
        if head == "command" and m == "POST":
            b = self._body()
            return self._op({"op": "command", "line": b.get("line", b.get("command", "")), "source": "web"})
        if head == "op" and m == "POST":
            b = self._body()
            if b.get("op") in ("quit", "detach"):
                pass                                     # allowed: the token holder controls the session
            return self._op(b)
        simple_get = {"state": "state", "windows": "windows", "frame": "frame_json", "commands": "commands", "rules": "rules",
                      "plugins": "plugins", "themes": "themes", "dialogs": "dialogs"}
        if m == "GET" and head in simple_get and len(parts) == 1:
            return self._op({"op": simple_get[head]})
        if head == "logs" and m == "GET":
            return self._op({"op": "logs", "n": int(query.get("n", ["100"])[0]), "level": (query.get("level") or [None])[0],
                             "pattern": (query.get("pattern") or [None])[0]})
        if head == "events" and m == "GET":
            return self._op({"op": "events", "since": int(query.get("since", ["0"])[0])})
        if head == "help" and m == "GET":
            return self._op({"op": "help", "topic": parts[1] if len(parts) > 1 else "index"})
        if head == "window" and len(parts) >= 2:
            wid = parts[1]
            sub = parts[2] if len(parts) > 2 else ""
            if sub == "text" and m == "GET":
                return self._op({"op": "capture", "window": wid, "history": query.get("history", ["0"])[0] in ("1", "true"),
                                 "lines": int(query["lines"][0]) if "lines" in query else None})
            if sub == "send" and m == "POST":
                b = self._body()
                b.update({"op": "send", "window": wid})
                return self._op(b)
            if sub == "" and m == "DELETE":
                return self._op({"op": "close", "window": wid})
        if head == "input" and m == "POST":
            b = self._body()
            return self._op({"op": "keys", "data": b.get("data", "")})
        if head == "resize" and m == "POST":
            b = self._body()
            return self._op({"op": "resize", "cols": int(b["cols"]), "rows": int(b["rows"])})
        if head == "config":
            if m == "GET" and len(parts) == 1:
                return self._op({"op": "config_get"})
            if m == "POST" and parts[-1] == "validate":
                return self._op({"op": "config_validate", "text": self._body().get("text", "")})
            if m in ("PUT", "POST") and len(parts) == 1:
                return self._op({"op": "config_put", "text": self._body().get("text", "")})
        if head in ("scripts", "script"):
            if m == "GET":
                return self._op({"op": "script_get", "name": parts[1] if len(parts) > 1 else None})
            if m in ("PUT", "POST") and len(parts) > 1:
                return self._op({"op": "script_put", "name": parts[1], "text": self._body().get("text", "")})
        self._err(404, "no such endpoint: %s %s" % (m, route))

    # ---------------------------------------------------------------- SSE
    def _stream(self, query):
        if self.command != "GET":
            return self._err(405, "GET only")
        wm = self.web.wm
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seq = -1
        ev_seq = int(query.get("since", [str(wm.event_seq)])[0])
        last_ping = time.time()

        def send(kind, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, data)).encode())
            self.wfile.flush()
        try:
            while not self.web.stopping:
                s, data = self.web.frame_json()
                if s != seq:
                    seq = s
                    send("frame", data)
                    last_ping = time.time()
                evs = [e for e in wm.event_ring if e["seq"] > ev_seq]
                if evs:
                    ev_seq = evs[-1]["seq"]
                    for e in evs:
                        send("event", json.dumps(e, default=str))
                    last_ping = time.time()
                if time.time() - last_ping > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
