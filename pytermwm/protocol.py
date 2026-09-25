"""Wire protocol between server and clients (attach / control) + runtime paths."""
from __future__ import annotations

import json
import os
import socket
import struct
from typing import List, Optional, Tuple

from . import compat

MAX_MSG = 64 * 1024 * 1024
HDR = struct.Struct(">cI")

# client -> server
HELLO, INPUT, RESIZE, DETACH, CONTROL = b"H", b"I", b"R", b"D", b"C"
# server -> client
OUTPUT, EXIT, MESSAGE, RESPONSE = b"O", b"X", b"M", b"r"


def pack(kind: bytes, payload: bytes = b"") -> bytes:
    return HDR.pack(kind, len(payload)) + payload


def pack_json(kind: bytes, obj) -> bytes:
    return pack(kind, json.dumps(obj, default=str).encode())


class MessageBuffer:
    """Incremental decoder of protocol messages."""

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> List[Tuple[bytes, bytes]]:
        self.buf += data
        out = []
        while len(self.buf) >= HDR.size:
            kind, n = HDR.unpack_from(self.buf)
            if n > MAX_MSG:
                raise ValueError("message too large")
            if len(self.buf) < HDR.size + n:
                break
            out.append((kind, self.buf[HDR.size:HDR.size + n]))
            self.buf = self.buf[HDR.size + n:]
        return out


def runtime_dir() -> str:
    base = os.environ.get("PYTERMWM_RUNTIME_DIR") or compat.runtime_home()
    os.makedirs(base, mode=0o700, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def state_dir() -> str:
    base = os.environ.get("PYTERMWM_STATE_DIR")
    if not base:
        base = os.path.join(compat.state_home(), "pytermwm")
    os.makedirs(base, exist_ok=True)
    return base


def socket_path(session: str) -> str:
    """Endpoint file of a session: a unix socket (POSIX) or a ``.port`` file describing a loopback listener (Windows)."""
    return os.path.join(runtime_dir(), session + compat.endpoint_ext())


def token_path(session: str) -> str:
    return os.path.join(runtime_dir(), "%s.token" % session)


def connect(session: str, timeout: float = 3.0) -> socket.socket:
    path = os.environ.get("PYTERMWM_SOCK") if session == "__env__" else socket_path(session)
    s = compat.connect_endpoint(path, timeout)
    s.settimeout(None)
    return s


def list_sessions(cleanup: bool = True) -> List[str]:
    """Names of live sessions (removes stale sockets)."""
    out = []
    d = runtime_dir()
    for f in sorted(os.listdir(d)):
        ext = ".port" if f.endswith(".port") else ".sock" if f.endswith(".sock") else None
        if ext is None:
            continue
        name = f[:-len(ext)]
        if compat.endpoint_alive(os.path.join(d, f)):
            out.append(name)
        elif cleanup:
            try:
                os.unlink(os.path.join(d, f))
            except OSError:
                pass
    return out


class ControlClient:
    """Synchronous request/response client for the control channel."""

    def __init__(self, session: Optional[str] = None, path: Optional[str] = None):
        p = path or (os.environ.get("PYTERMWM_SOCK") if not session and os.environ.get("PYTERMWM_SOCK") else socket_path(session or "default"))
        self.sock = compat.connect_endpoint(p, 10.0)
        self.sock.settimeout(None)
        self.mb = MessageBuffer()

    def request(self, req: dict, timeout: float = 30.0) -> dict:
        self.sock.settimeout(timeout)
        self.sock.sendall(pack_json(CONTROL, req))
        while True:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("server closed the connection")
            for kind, payload in self.mb.feed(data):
                if kind == RESPONSE:
                    return json.loads(payload.decode())

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
