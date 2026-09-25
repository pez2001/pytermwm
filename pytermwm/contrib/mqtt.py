"""MQTT integration with a tiny built-in MQTT 3.1.1 client (stdlib only).

    plugins:
      - name: mqtt
        host: broker.local
        port: 1883                    # 8883 with tls: true
        topics: [home/#, sensors/+/temp]
        username: me                  # optional
        password: secret
        status:                       # topic -> status line item
          home/boiler/temp: boiler
        window: true                  # open a viewer window showing all messages

Commands: mqtt-pub <topic> <payload...> [--retain] [--qos 0|1]   mqtt-sub <topic>   mqtt-unsub <topic>   mqtt-status   mqtt-window
Every received message is also emitted as the WM event ``mqtt_message`` (fields topic, payload) so automation rules
can react:  when: {event: mqtt_message, match: {topic: "^alarm/", payload: "ON"}}.
"""
from __future__ import annotations

import os
import socket
import ssl
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from pytermwm.commands import CommandError
from pytermwm.statusline import Segment

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14


def enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def enc_str(s) -> bytes:
    b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
    return struct.pack(">H", len(b)) + b


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + enc_len(len(body)) + body


def topic_matches(flt: str, topic: str) -> bool:
    f, t = flt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


class MqttError(Exception):
    pass


class MqttClient:
    def __init__(self, host: str, port: int = 1883, client_id: Optional[str] = None, username: Optional[str] = None,
                 password: Optional[str] = None, keepalive: int = 30, tls: bool = False, on_message: Optional[Callable] = None,
                 on_state: Optional[Callable] = None, reconnect: bool = True, log=None):
        self.host, self.port = host, int(port)
        self.client_id = client_id or "pytermwm-%d-%d" % (os.getpid(), int(time.time()) % 10000)
        self.username, self.password = username, password
        self.keepalive = int(keepalive)
        self.tls = tls
        self.on_message = on_message
        self.on_state = on_state
        self.reconnect = reconnect
        self.log = log
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.subs: Dict[str, int] = {}
        self._pid = 0
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error = ""
        self.rx = self.tx = 0

    # ---------------------------------------------------------------- public
    def start(self):
        self._thread = threading.Thread(target=self._run, name="ptw-mqtt", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        s = self.sock
        if s is not None:
            try:
                s.sendall(packet(DISCONNECT, 0, b""))
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False):
        if not self.connected:
            raise MqttError("not connected to %s:%d" % (self.host, self.port))
        if any(c in topic for c in "+#") or not topic:
            raise MqttError("invalid topic for publish: %r" % topic)
        body = enc_str(topic)
        flags = (qos << 1) | (1 if retain else 0)
        if qos:
            body += struct.pack(">H", self._next_pid())
        body += payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        self._send(packet(PUBLISH, flags, body))
        self.tx += 1

    def subscribe(self, topic: str, qos: int = 0):
        self.subs[topic] = qos
        if self.connected:
            self._send(packet(SUBSCRIBE, 2, struct.pack(">H", self._next_pid()) + enc_str(topic) + bytes([qos])))

    def unsubscribe(self, topic: str):
        self.subs.pop(topic, None)
        if self.connected:
            self._send(packet(UNSUBSCRIBE, 2, struct.pack(">H", self._next_pid()) + enc_str(topic)))

    # ---------------------------------------------------------------- internals
    def _next_pid(self) -> int:
        self._pid = self._pid % 65535 + 1
        return self._pid

    def _send(self, data: bytes):
        s = self.sock
        if s is None:
            raise MqttError("not connected")
        with self._wlock:
            try:
                s.sendall(data)
            except OSError as e:
                raise MqttError("send failed: %s" % e)

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise MqttError("connection closed")
            buf += chunk
        return bytes(buf)

    def _read_packet(self) -> Tuple[int, int, bytes]:
        b = self._recv_exact(1)[0]
        mult, length = 1, 0
        for _ in range(4):
            d = self._recv_exact(1)[0]
            length += (d & 0x7F) * mult
            if not d & 0x80:
                break
            mult *= 128
        else:
            raise MqttError("bad remaining length")
        if length > 16 << 20:
            raise MqttError("packet too large")
        return b >> 4, b & 0x0F, self._recv_exact(length) if length else b""

    def _connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=10)
        if self.tls:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        self.sock = raw
        flags = 0x02      # clean session
        payload = enc_str(self.client_id)
        if self.username is not None:
            flags |= 0x80
            payload += enc_str(self.username)
            if self.password is not None:
                flags |= 0x40
                payload += enc_str(self.password)
        var = enc_str("MQTT") + bytes([4, flags]) + struct.pack(">H", self.keepalive)
        raw.sendall(packet(CONNECT, 0, var + payload))
        raw.settimeout(10)
        t, _, body = self._read_packet()
        if t != CONNACK or len(body) < 2:
            raise MqttError("expected CONNACK")
        if body[1] != 0:
            raise MqttError("connection refused (code %d)" % body[1])
        raw.settimeout(max(1.0, self.keepalive / 2.0))
        self.connected = True
        for topic, qos in list(self.subs.items()):
            self._send(packet(SUBSCRIBE, 2, struct.pack(">H", self._next_pid()) + enc_str(topic) + bytes([qos])))
        self._state()

    def _state(self):
        if self.on_state:
            try:
                self.on_state(self.connected, self.last_error)
            except Exception:
                pass

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect()
                self.last_error = ""
                backoff = 1.0
                last_tx = time.time()
                while not self._stop.is_set():
                    try:
                        t, flags, body = self._read_packet()
                    except socket.timeout:
                        if time.time() - last_tx >= self.keepalive / 2.0:
                            self._send(packet(PINGREQ, 0, b""))
                            last_tx = time.time()
                        continue
                    self.rx += 1
                    if t == PUBLISH:
                        tl = struct.unpack(">H", body[:2])[0]
                        topic = body[2:2 + tl].decode("utf-8", "replace")
                        pos = 2 + tl
                        qos = (flags >> 1) & 3
                        if qos:
                            pid = body[pos:pos + 2]
                            pos += 2
                            if qos == 1:
                                self._send(packet(PUBACK, 0, pid))
                        if self.on_message:
                            self.on_message(topic, body[pos:], qos, bool(flags & 1))
                    if time.time() - last_tx >= self.keepalive / 2.0:
                        self._send(packet(PINGREQ, 0, b""))
                        last_tx = time.time()
            except (MqttError, OSError, ssl.SSLError) as e:
                if self._stop.is_set():
                    break
                self.last_error = str(e)
                if self.log:
                    self.log.info("mqtt: %s", e)
            finally:
                was = self.connected
                self.connected = False
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass
                self.sock = None
                if was or self.last_error:
                    self._state()
            if not self.reconnect:
                break
            self._stop.wait(backoff)
            backoff = min(30.0, backoff * 2)


# ---------------------------------------------------------------------------------- plugin
def setup(api):
    cfg = api.config
    wm = api.wm
    host = cfg.get("host")
    state = {"viewer": None, "routes": {}, "count": 0}       # routes: filter -> window id

    def get_viewer():
        w = state["viewer"]
        if w is not None and w in wm.windows.values():
            return w
        w = wm.create_window({"kind": "viewer", "title": "mqtt", "focus": False})
        state["viewer"] = w
        return w

    def deliver(topic, payload, qos, retain):
        text = payload.decode("utf-8", "replace")
        state["count"] += 1
        stat = cfg.get("status") or {}
        for flt, key in stat.items():
            if topic_matches(flt, topic):
                wm.set_status(str(key), text[:60], style="normal")
        wm.emit("mqtt_message", topic=topic, payload=text, qos=qos, retain=retain)
        line = "%s  %s%s" % (topic, text.replace("\n", "\\n"), "  (retained)" if retain else "")
        for flt, wid in list(state["routes"].items()):
            w = wm.windows.get(wid)
            if w is not None and topic_matches(flt, topic):
                w.feed_bytes((line + "\n").encode())
        if cfg.get("window") and not state["routes"]:
            get_viewer().feed_bytes((time.strftime("%H:%M:%S ") + line + "\n").encode())

    client: Optional[MqttClient] = None
    if host:
        def on_message(topic, payload, qos, retain):
            api.call_soon(deliver, topic, payload, qos, retain)

        def on_state(ok, err):
            api.call_soon(setattr, wm, "dirty", True)

        client = MqttClient(str(host), int(cfg.get("port", 8883 if cfg.get("tls") else 1883)), cfg.get("client_id"),
                            cfg.get("username"), cfg.get("password"), int(cfg.get("keepalive", 30)), bool(cfg.get("tls")),
                            on_message, on_state, log=api.log)
        for t in cfg.get("topics") or []:
            client.subscribe(str(t))
        for t in (cfg.get("status") or {}):
            if str(t) not in client.subs:
                client.subscribe(str(t))
        client.start()
        api.on_unload(client.stop)
    else:
        api.log.warning("mqtt plugin: no host configured (plugins: [{name: mqtt, host: ...}])")

    def need():
        if client is None:
            raise CommandError("mqtt: no broker configured (set host: in the plugin options)")
        return client

    def c_pub(wm_, args):
        retain, qos, rest, i = False, 0, [], 0
        while i < len(args):
            a = args[i]
            if a == "--retain":
                retain = True
            elif a == "--qos" and i + 1 < len(args):
                qos = int(args[i + 1])
                i += 1
            else:
                rest.append(a)
            i += 1
        if qos not in (0, 1):
            raise CommandError("only QoS 0 and 1 are supported")
        if len(rest) < 1:
            raise CommandError("usage: mqtt-pub <topic> <payload...> [--retain] [--qos 0|1]")
        try:
            need().publish(rest[0], " ".join(rest[1:]), qos, retain)
        except MqttError as e:
            raise CommandError(str(e))
        return "published to %s" % rest[0]

    def c_sub(wm_, args):
        if not args:
            raise CommandError("usage: mqtt-sub <topic-filter> [window]")
        c = need()
        c.subscribe(args[0])
        if len(args) > 1:
            state["routes"][args[0]] = wm_.resolve_window(args[1]).id
        else:
            w = wm_.create_window({"kind": "viewer", "title": "mqtt %s" % args[0]})
            state["routes"][args[0]] = w.id
        return "subscribed to %s" % args[0]

    def c_unsub(wm_, args):
        if not args:
            raise CommandError("usage: mqtt-unsub <topic-filter>")
        need().unsubscribe(args[0])
        state["routes"].pop(args[0], None)

    def c_status(wm_, args):
        c = need()
        return {"connected": c.connected, "host": c.host, "port": c.port, "subscriptions": sorted(c.subs), "received": state["count"],
                "error": c.last_error}

    def c_window(wm_, args):
        return {"id": get_viewer().id}

    api.command("mqtt-pub", c_pub, usage="mqtt-pub <topic> <payload...> [--retain] [--qos 0|1]", help="Publish an MQTT message")
    api.command("mqtt-sub", c_sub, usage="mqtt-sub <topic-filter> [window]", help="Subscribe and show messages in a window")
    api.command("mqtt-unsub", c_unsub, usage="mqtt-unsub <topic-filter>", help="Unsubscribe")
    api.command("mqtt-status", c_status, usage="mqtt-status", help="Connection state")
    api.command("mqtt-window", c_window, usage="mqtt-window", help="Open the all-messages window")

    def seg(wm_, o):
        if client is None:
            return None
        return Segment("mqtt %s" % ("●" if client.connected else "○"), "ok" if client.connected else "err", prio=2)

    api.segment("mqtt", seg)
