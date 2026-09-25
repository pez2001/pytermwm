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


def inside_session() -> Optional[str]:
    """The session this process runs in, when it runs inside one of its windows (``None`` otherwise).

    ``PYTERMWM_WINDOW`` is only ever set in a window's environment; ``PYTERMWM_SESSION`` alone is not enough, people
    set it themselves to choose their default session."""
    if os.environ.get("PYTERMWM_WINDOW") and os.environ.get("PYTERMWM_SESSION"):
        return os.environ["PYTERMWM_SESSION"]
    return None


def nesting_refused(session: Optional[str], nested: bool = False) -> Optional[str]:
    """Why taking over this terminal for ``session`` (``None``: a new standalone one) must not happen, or ``None``.

    Attaching to the session you are in would draw the session into one of its own windows, which shows the session
    again, and so on: the output grows without end and takes the window manager down. Another session (or a
    standalone one) inside a window works, but is almost always a mistake, so it needs ``--nested``."""
    here = inside_session()
    if here is None:
        return None
    if session == here:
        return ("pytermwm: you are inside session %r (window %s); attaching to it here would show the session inside "
                "itself. Use the keys of this session, or `pytermwm run/send/ctl ...` to control it from this shell."
                % (here, os.environ.get("PYTERMWM_WINDOW")))
    if not nested:
        what = "a standalone pytermwm" if session is None else "session %r" % session
        return ("pytermwm: you are inside session %r; add --nested to really run %s in this window "
                "(or detach first and run it from outside)" % (here, what))
    return None


def attach(session: str, name: Optional[str] = None, mouse: bool = True, nested: bool = False) -> int:
    why = nesting_refused(session, nested)
    if why:
        sys.stderr.write(why + "\n")
        return 1
    try:
        sock = P.connect(session)
    except (ConnectionRefusedError, FileNotFoundError):
        sys.stderr.write("pytermwm: no session %r running (start one with: pytermwm start -s %s)\n" % (session, session))
        return 1
    cols, rows = term_size(1)
    sock.sendall(P.pack_json(P.HELLO, {"cols": cols, "rows": rows, "depth": detect_depth(), "glyphs": detect_glyphs(),
                                        "name": name or "tty%d" % os.getpid(),
                                        "inside": inside_session()}))
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
