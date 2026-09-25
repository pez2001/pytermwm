"""The session server: owns the window manager, ptys and clients (attach / detach like screen)."""
from __future__ import annotations

import itertools
import json
import os
import selectors
import signal
import socket
import sys
import time
import traceback
from typing import Dict, List, Optional

from . import compat
from . import protocol as P
from .commands import CommandError
from .config import ConfigError, attach_config
from .keys import KeyParser
from .logs import setup_logging
from .render import Compositor, Frame, FrameWriter, crop_frame
from .charts import GLYPH_SETS
from .terminal import RawTerminal, StdinReader, console_palette, detect_depth, detect_glyphs, term_size
from .wm import WindowManager

_ids = itertools.count(1)
BACKLOG_LIMIT = 512 * 1024


class Client:
    def __init__(self, sock: Optional[socket.socket] = None, local: bool = False):
        self.id = next(_ids)
        self.sock = sock
        self.local = local
        self.mb = P.MessageBuffer()
        self.outbuf = bytearray()
        self.hello = False
        self.cols, self.rows, self.depth = 80, 24, 8
        self.glyphs = "unicode"
        self.parser = KeyParser()
        self.writer: Optional[FrameWriter] = None
        self.esc_deadline = 0.0
        self.closed = False
        self.stale = False
        self.last_input = 0.0
        self.close_after_flush = False
        self.name = "local" if local else "client%d" % self.id

    def backlog(self) -> int:
        return len(self.outbuf)

    def send_raw(self, data: bytes):
        if self.local:
            try:
                view = memoryview(data)
                while view:
                    n = os.write(1, view)
                    view = view[n:]
            except BlockingIOError:
                pass
            except OSError:
                self.closed = True
            return
        self.outbuf += data

    def send_output(self, data: bytes):
        if self.local:
            self.send_raw(data)
        else:
            self.send_raw(P.pack(P.OUTPUT, data))

    def send_json(self, kind: bytes, obj):
        if not self.local:
            self.send_raw(P.pack_json(kind, obj))


class Server:
    def __init__(self, session: str = "default", config_path: Optional[str] = None, cols: int = 80, rows: int = 24,
                 standalone: bool = False, restore_path: Optional[str] = None, web: Optional[dict] = None,
                 debug: bool = False, sock_path: Optional[str] = None, log_stderr: bool = False):
        self.session = session
        self.config_path = config_path
        self.standalone = standalone
        self.restore_path = restore_path
        self.web_override = web
        self.debug = debug
        self.sock_path = sock_path or P.socket_path(session)
        self.log_stderr = log_stderr
        self.cols, self.rows = cols, rows
        self.sel = selectors.DefaultSelector()
        self.clients: List[Client] = []
        self.primary: Optional[Client] = None
        self.listen: Optional[socket.socket] = None
        self.win_fds: Dict[int, tuple] = {}
        self.win_wfds: Dict[int, object] = {}
        self.wm: Optional[WindowManager] = None
        self.comp: Optional[Compositor] = None
        self.last_frame: Optional[Frame] = None
        self.web = None
        self.web_cfg = None
        self.stop = False
        self.waker = compat.Waker()
        self.stdin_reader: Optional[StdinReader] = None
        self.listener: Optional[compat.Listener] = None
        self.last_render = 0.0
        self.last_autosave = time.time()
        self.last_snapshot_hash = None
        self.active_client: Optional[Client] = None
        self.local: Optional[Client] = None
        self.term: Optional[RawTerminal] = None
        self.resize_pending = False
        self.mouse_state = True
        self.log = None
        self._reload_flag = False
        self.sel.register(self.waker.r, selectors.EVENT_READ, ("wake", None))

    # ------------------------------------------------------------------ setup
    def setup(self):
        log_file = os.path.join(P.state_dir(), "logs", "%s.log" % self.session)
        logger = setup_logging("pytermwm.%s" % self.session, "debug" if self.debug else "info", log_file, stderr=self.log_stderr)
        self.log = logger
        wm = WindowManager(self.cols, self.rows, session_name=self.session, logger=logger)
        self.wm = wm
        wm.wake = self._wake
        wm.prompt.enable_persistence(os.path.join(P.state_dir(), "prompt_history.json"))
        self.comp = Compositor(wm)
        wm.sock_path = self.sock_path
        wm.server = self
        from .protocol import list_sessions
        wm.extra["sessions_provider"] = list_sessions
        if self.restore_path:
            from .session import load_session_file, restore
            restore(wm, load_session_file(self.restore_path))
        attach_config(wm, self.config_path, initial=True)
        if not wm.windows:
            wm.create_window({})
        wm.on("config_applied", lambda **kw: self._config_applied())
        if not self.standalone:
            self._listen()
        self._start_web()
        for sig, handler in ((signal.SIGTERM, self._on_term), (signal.SIGINT, self._on_term), (compat.SIGHUP, self._on_hup),
                             (compat.SIGWINCH, self._on_winch)):
            compat.install_signal(sig, handler)
        if compat.IS_WINDOWS:
            compat.install_signal(getattr(signal, "SIGBREAK", None), self._on_term)
        logger.info("session %s ready (socket %s)", self.session, self.sock_path if not self.standalone else "-")

    def _listen(self):
        path = self.sock_path
        if os.path.exists(path) and compat.endpoint_alive(path):
            raise RuntimeError("session %r is already running" % self.session)
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        self.listener = compat.Listener(path)
        self.listen = self.listener.sock
        self.sel.register(self.listen, selectors.EVENT_READ, ("listen", None))

    def _on_term(self, *a):
        self.stop = True
        self._wake()

    def _on_hup(self, *a):
        self._reload_flag = True
        self._wake()

    def _on_winch(self, *a):
        self.resize_pending = True
        self._wake()

    def _wake(self):  # also handed to the wm as ``wm.wake``
        self.waker.wake()

    # ------------------------------------------------------------------ web
    def _web_config(self) -> Optional[dict]:
        cfg = dict((self.wm.cfg.get("web") or {}))
        if self.web_override:
            cfg.update(self.web_override)
        if not cfg.get("enabled"):
            return None
        cfg.setdefault("host", "127.0.0.1")
        cfg.setdefault("port", 8765)
        return cfg

    def _start_web(self):
        cfg = self._web_config()
        if cfg == self.web_cfg:
            return
        if self.web:
            try:
                self.web.stop()
            except Exception:
                pass
            self.web = None
        self.web_cfg = cfg
        if cfg is None:
            return
        try:
            from .api import WebServer
            token = cfg.get("token")
            self.web = WebServer(self.wm, cfg["host"], int(cfg["port"]), token, self.session)
            self.web.start()
            self.log.info("web interface on http://%s:%s/?token=%s", cfg["host"], self.web.port, self.web.token)
        except Exception as e:
            self.log.error("cannot start web interface: %s", e)
            self.wm.message("web interface failed: %s" % e, "err", 6.0)

    def _config_applied(self):
        self._start_web()
        self._sync_mouse()

    def _sync_mouse(self):
        """Tell the clients whether to report the mouse (off in copy view so the terminal can select text itself)."""
        mouse = self.wm.mouse_effective()
        if mouse != self.mouse_state:
            self.mouse_state = mouse
            for c in self.clients:
                if c.local and self.term:
                    self.term.set_mouse(mouse)
                else:
                    c.send_json(P.MESSAGE, {"mouse": mouse})

    # ------------------------------------------------------------------ local tty (standalone)
    def attach_local(self):
        c = Client(local=True)
        c.hello = True
        c.cols, c.rows = term_size(1)
        c.depth = detect_depth()
        c.glyphs = detect_glyphs()
        c.writer = FrameWriter(c.depth)
        self.local = c
        self.clients.append(c)
        self.primary = c
        self.wm.chart_glyphs_hint = c.glyphs
        self.stdin_reader = self.term.stdin_reader() if self.term else StdinReader()
        self.sel.register(self.stdin_reader.fileno(), selectors.EVENT_READ, ("stdin", c))
        self.wm.resize(c.cols, c.rows)

    # ------------------------------------------------------------------ main loop
    def run(self):
        wm = self.wm
        try:
            while not self.stop and not wm.quit_requested:
                self._sync_fds()
                timeout = 0.02 if wm.dirty else 0.1
                try:
                    events = self.sel.select(timeout)
                except InterruptedError:
                    events = []
                except OSError:
                    events = []
                with wm.lock:
                    for key, mask in events:
                        self._dispatch(key, mask)
                    now = time.time()
                    self._esc_timeouts(now)
                    if self.local and compat.SIGWINCH is None:      # no SIGWINCH on Windows: poll the console size
                        cur = term_size(1)
                        if cur != (self.local.cols, self.local.rows):
                            self.resize_pending = True
                    if self.resize_pending and self.local:
                        self.resize_pending = False
                        self.local.cols, self.local.rows = term_size(1)
                        wm.resize(self.local.cols, self.local.rows)
                        self.local.writer.invalidate()
                        wm.dirty = True
                    if getattr(self, "_reload_flag", False):
                        self._reload_flag = False
                        wm.run_command_line("reload", source="signal")
                    wm.poll_sources()
                    wm.tick(now)
                    self._flush_clients()
                    if wm.dirty and now - self.last_render >= 0.015:
                        self.last_render = now
                        wm.dirty = False
                        wm.frame_seq = getattr(wm, "frame_seq", 0) + 1
                        self.render()
                    self._autosave(now)
                    self._sync_client_writes()
                    self._handle_detach()
        finally:
            self.shutdown()

    def _sync_fds(self):
        wm = self.wm
        want = {fd: (w, stream) for fd, w, stream in wm.iter_read_fds()}
        for fd in list(self.win_fds):
            if fd not in want or self.win_fds[fd] != want[fd]:
                try:
                    self.sel.unregister(fd)
                except (KeyError, ValueError, OSError):
                    pass
                del self.win_fds[fd]
        for fd, val in want.items():
            if fd not in self.win_fds:
                try:
                    self.sel.register(fd, selectors.EVENT_READ, ("win", val))
                    self.win_fds[fd] = val
                except (ValueError, OSError, KeyError):
                    pass
        wwant = {fd: w for fd, w in wm.iter_write_fds()}
        for fd in list(self.win_wfds):
            if fd not in wwant:
                try:
                    self.sel.unregister(fd)
                except (KeyError, ValueError, OSError):
                    pass
                del self.win_wfds[fd]
        for fd, w in wwant.items():
            if fd not in self.win_wfds and fd not in self.win_fds:
                try:
                    self.sel.register(fd, selectors.EVENT_WRITE, ("winw", w))
                    self.win_wfds[fd] = w
                except (ValueError, OSError, KeyError):
                    pass
            elif fd in self.win_fds and fd not in self.win_wfds:
                # fd already registered for reading (pty master): modify to read|write
                try:
                    self.sel.modify(fd, selectors.EVENT_READ | selectors.EVENT_WRITE, ("win", self.win_fds[fd]))
                    self.win_wfds[fd] = w
                except (ValueError, OSError, KeyError):
                    pass

    def _dispatch(self, key, mask):
        kind, obj = key.data
        try:
            if kind == "listen":
                self._accept()
            elif kind == "client":
                if mask & selectors.EVENT_READ:
                    self._client_readable(obj)
                if mask & selectors.EVENT_WRITE:
                    self._client_writable(obj)
            elif kind == "stdin":
                self._stdin_readable(obj)
            elif kind == "win":
                w, stream = obj
                if mask & selectors.EVENT_READ:
                    self.wm.source_readable(w, key.fd, stream)
                if mask & selectors.EVENT_WRITE and w.source is not None:
                    w.source.flush()
            elif kind == "winw":
                if obj.source is not None:
                    obj.source.flush()
            elif kind == "wake":
                pass
        except Exception:
            self.log.error("event dispatch failed:\n%s", traceback.format_exc())
        self.waker.drain()

    # ------------------------------------------------------------------ clients
    def _accept(self):
        conn = self.listener.accept()
        if conn is None:
            return
        c = Client(conn)
        self.clients.append(c)
        self.sel.register(conn, selectors.EVENT_READ, ("client", c))

    def _client_readable(self, c: Client):
        try:
            data = c.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(c)
            return
        try:
            msgs = c.mb.feed(data)
        except ValueError:
            self._drop(c)
            return
        for kind, payload in msgs:
            self._on_message(c, kind, payload)
            if c.closed:
                break

    def _client_writable(self, c: Client):
        if not c.outbuf:
            return
        try:
            n = c.sock.send(c.outbuf)
            del c.outbuf[:n]
        except BlockingIOError:
            return
        except OSError:
            self._drop(c)
            return
        if not c.outbuf and c.close_after_flush:
            self._drop(c)

    def _flush_clients(self):
        for c in list(self.clients):
            if c.sock is not None and c.outbuf and not c.closed:
                self._client_writable(c)

    def _sync_client_writes(self):
        for c in self.clients:
            if c.sock is None or c.closed:
                continue
            ev = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.outbuf else 0)
            try:
                self.sel.modify(c.sock, ev, ("client", c))
            except (KeyError, ValueError, OSError):
                pass
            if c.backlog() < BACKLOG_LIMIT // 4 and c.stale:
                self.wm.dirty = True

    def _drop(self, c: Client):
        if c.closed:
            return
        c.closed = True
        if c in self.clients:
            self.clients.remove(c)
        if c.sock is not None:
            try:
                self.sel.unregister(c.sock)
            except (KeyError, ValueError, OSError):
                pass
            try:
                c.sock.close()
            except OSError:
                pass
        if self.primary is c:
            cand = [x for x in self.clients if x.hello]
            self.primary = max(cand, key=lambda x: x.last_input) if cand else None
            if self.primary:
                self.wm.resize(self.primary.cols, self.primary.rows)
        if c.hello:
            self.wm.emit("client_detached", client=c.name)
            self.log.info("client %s detached", c.name)

    def _on_message(self, c: Client, kind: bytes, payload: bytes):
        wm = self.wm
        if kind == P.HELLO:
            info = json.loads(payload.decode() or "{}")
            c.cols, c.rows = int(info.get("cols", 80)), int(info.get("rows", 24))
            c.depth = int(info.get("depth", 8))
            g = info.get("glyphs")
            c.glyphs = g if g in GLYPH_SETS else "unicode"
            c.name = info.get("name") or c.name
            c.writer = FrameWriter(c.depth)
            c.hello = True
            c.last_input = time.time()
            self.primary = c
            wm.chart_glyphs_hint = c.glyphs
            wm.resize(c.cols, c.rows)
            wm.dirty = True
            c.send_json(P.MESSAGE, {"mouse": wm.mouse_effective(), "session": self.session})
            self.log.info("client %s attached (%dx%d, depth %d)", c.name, c.cols, c.rows, c.depth)
            wm.emit("client_attached", client=c.name)
        elif kind == P.INPUT:
            c.last_input = time.time()
            if c.hello and self.primary is not c:
                self.primary = c
                wm.resize(c.cols, c.rows)
            self._handle_input(c, payload)
        elif kind == P.RESIZE:
            info = json.loads(payload.decode())
            c.cols, c.rows = int(info["cols"]), int(info["rows"])
            if c.writer:
                c.writer.invalidate()
            if self.primary is c or self.primary is None:
                wm.resize(c.cols, c.rows)
            wm.dirty = True
        elif kind == P.DETACH:
            self._detach(c, "detached")
        elif kind == P.CONTROL:
            req = json.loads(payload.decode())
            from .control import handle_op
            self.active_client = c
            try:
                res = handle_op(wm, req)
            finally:
                self.active_client = None
            if "req_id" in req:            # correlation id (never clobbers result fields such as a window "id")
                res["req_id"] = req["req_id"]
            c.send_json(P.RESPONSE, res)

    def _handle_input(self, c: Client, data: bytes):
        wm = self.wm
        self.active_client = c
        try:
            evs = c.parser.feed(data)
            if c.parser.pending():
                c.esc_deadline = time.time() + 0.05
            wm.process_events(evs, c)
        finally:
            self.active_client = None

    def _esc_timeouts(self, now: float):
        for c in self.clients:
            if c.esc_deadline and now >= c.esc_deadline:
                c.esc_deadline = 0.0
                if c.parser.pending():
                    self.active_client = c
                    try:
                        self.wm.process_events(c.parser.flush(), c)
                    finally:
                        self.active_client = None

    def _stdin_readable(self, c: Client):
        data = self.stdin_reader.read()
        if data is None:
            self.stop = True
            return
        if not data:
            return
        c.last_input = time.time()
        self._handle_input(c, data)

    def _handle_detach(self):
        wm = self.wm
        if not wm.detach_requested:
            return
        wm.detach_requested = False
        if self.standalone:
            wm.message("detach is not available in standalone mode (use quit)", "warn")
            wm.detach_all = False
            return
        if wm.detach_all:
            wm.detach_all = False
            for c in list(self.clients):
                if c.hello:
                    self._detach(c, "detached")
        else:
            targets = [self.active_client] if self.active_client else [c for c in self.clients if c.hello]
            for c in targets:
                if c is not None:
                    self._detach(c, "detached")

    def _detach(self, c: Client, reason: str):
        if c.local:
            self.stop = True
            return
        c.send_json(P.EXIT, {"reason": reason, "session": self.session})
        c.close_after_flush = True
        c.hello = False
        if self.primary is c:
            cand = [x for x in self.clients if x.hello]
            self.primary = max(cand, key=lambda x: x.last_input) if cand else None

    # ------------------------------------------------------------------ rendering
    def render(self):
        wm = self.wm
        self._sync_mouse()
        tty = [c for c in self.clients if c.hello and c.writer]
        if not tty:
            wm.pending_terminal_output.clear()          # nobody to show a notification to
            return
        frame = self.comp.compose(wm.cols, wm.rows)
        self.last_frame = frame
        extra = "".join(wm.pending_terminal_output).encode("utf-8", "replace")
        wm.pending_terminal_output.clear()
        for c in tty:
            if extra:
                c.send_output(extra)
            if c.backlog() > BACKLOG_LIMIT:
                c.stale = True
                continue
            f = frame if (c.cols, c.rows) == (wm.cols, wm.rows) else crop_frame(frame, c.cols, c.rows, wm.status_row())
            if c.stale:
                c.writer.invalidate()
                c.stale = False
            data = c.writer.write(f)
            if data:
                c.send_output(data.encode("utf-8", "replace"))

    # ------------------------------------------------------------------ autosave
    def _autosave(self, now: float):
        every = self.wm.cfg.get("autosave", 30)
        if not every or now - self.last_autosave < float(every):
            return
        self.last_autosave = now
        try:
            from .session import snapshot, save_session
            h = hash(json.dumps(snapshot(self.wm, False), sort_keys=True, default=str)[:200000])
            if h != self.last_snapshot_hash:
                save_session(self.wm)
                self.last_snapshot_hash = h
        except Exception as e:
            self.log.warning("autosave failed: %s", e)

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        wm = self.wm
        if wm is None:
            return
        try:
            if self.wm.cfg.get("autosave", 30):
                from .session import save_session
                save_session(wm)
        except Exception:
            pass
        for c in list(self.clients):
            if c.sock is not None:
                try:
                    c.sock.send(P.pack_json(P.EXIT, {"reason": "exited", "session": self.session}))
                except OSError:
                    pass
                try:
                    c.sock.close()
                except OSError:
                    pass
        if self.web:
            try:
                self.web.stop()
            except Exception:
                pass
        try:
            wm.shutdown()
        except Exception:
            pass
        if self.stdin_reader is not None:
            self.stdin_reader.close()
        if self.listener is not None:
            self.listener.close()                      # closes the socket and removes the endpoint file
            try:
                os.unlink(P.token_path(self.session))
            except OSError:
                pass
        self.log.info("session %s ended", self.session)


def run_standalone(session: str = "standalone", config_path: Optional[str] = None, debug: bool = False,
                   web: Optional[dict] = None, restore_path: Optional[str] = None) -> int:
    """Single process mode: the server also is the (only) terminal client."""
    cols, rows = term_size(1)
    srv = Server(session, config_path, cols, rows, standalone=True, debug=debug, web=web, restore_path=restore_path)
    try:
        srv.setup()
    except (ConfigError, CommandError) as e:
        sys.stderr.write("pytermwm: configuration error: %s\n" % e)
        return 2
    with RawTerminal(mouse=bool(srv.wm.cfg.get("mouse", True)), palette=console_palette()) as term:
        srv.term = term
        srv.attach_local()
        srv.run()
    return 0
