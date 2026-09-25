"""The attach client: a thin terminal front end for a running session."""
from __future__ import annotations

import json
import os
import select
import sys
from typing import Optional

from . import compat
from . import protocol as P
from .terminal import RawTerminal, console_palette, detect_depth, detect_glyphs, term_size, write_all

RESIZE_POLL = 0.25          # seconds; used where there is no SIGWINCH (Windows)


def attach(session: str, name: Optional[str] = None, mouse: bool = True) -> int:
    try:
        sock = P.connect(session)
    except (ConnectionRefusedError, FileNotFoundError):
        sys.stderr.write("pytermwm: no session %r running (start one with: pytermwm start -s %s)\n" % (session, session))
        return 1
    cols, rows = term_size(1)
    sock.sendall(P.pack_json(P.HELLO, {"cols": cols, "rows": rows, "depth": detect_depth(), "glyphs": detect_glyphs(),
                                        "name": name or "tty%d" % os.getpid()}))
    mb = P.MessageBuffer()
    waker = compat.Waker()
    poll_resize = compat.SIGWINCH is None
    old = None
    if not poll_resize:
        import signal
        old = signal.signal(compat.SIGWINCH, lambda *a: waker.wake())
    reason = "lost connection"
    size = (cols, rows)
    try:
        with RawTerminal(mouse=mouse, palette=console_palette()) as term:
            stdin = term.stdin_reader()
            sock.setblocking(True)
            # Entering raw mode (Windows: switching console modes, and reconciling the screen
            # buffer to the window -- see _WinConsole.sync_buffer_to_window) is itself sometimes
            # what settles a console's idea of its own size; re-check right away instead of
            # waiting for the first poll/SIGWINCH so a HELLO sent with a stale size self-corrects
            # immediately rather than showing a garbled first frame.
            c2, r2 = term_size(1)
            if (c2, r2) != size:
                size = (c2, r2)
                sock.sendall(P.pack_json(P.RESIZE, {"cols": c2, "rows": r2}))
            done = False
            while not done:
                try:
                    r, _, _ = select.select([stdin.fileno(), sock, waker.r], [], [], RESIZE_POLL if poll_resize else None)
                except InterruptedError:
                    continue
                if waker.r in r:
                    waker.drain()
                if waker.r in r or poll_resize:
                    c, rr = term_size(1)
                    if (c, rr) != size:
                        size = (c, rr)
                        sock.sendall(P.pack_json(P.RESIZE, {"cols": c, "rows": rr}))
                if stdin.fileno() in r:
                    data = stdin.read()
                    if data is None:
                        reason = "input closed"
                        break
                    if data:
                        sock.sendall(P.pack(P.INPUT, data))
                if sock in r:
                    try:
                        data = sock.recv(1 << 20)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    for kind, payload in mb.feed(data):
                        if kind == P.OUTPUT:
                            write_all(1, payload)
                        elif kind == P.EXIT:
                            info = json.loads(payload.decode() or "{}")
                            reason = info.get("reason", "exited")
                            done = True
                        elif kind == P.MESSAGE:
                            try:
                                info = json.loads(payload.decode())
                                if "mouse" in info and mouse:
                                    term.set_mouse(bool(info["mouse"]))
                            except ValueError:
                                pass
            stdin.close()
    finally:
        if old is not None:
            import signal
            signal.signal(compat.SIGWINCH, old)
        waker.close()
        try:
            sock.close()
        except OSError:
            pass
    sys.stdout.write("[%s (session %s)]\n" % (reason, session))
    return 0
