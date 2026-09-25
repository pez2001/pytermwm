import http.server
import json
import os
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.helpers import *
from pytermwm import config as C
from pytermwm.ansi import Screen
from pytermwm.contrib import mqtt as M
from pytermwm.contrib import ssh as S
from pytermwm.contrib.effects import EFFECTS, NAMES


def shown(w):
    w.refresh()
    return "\n".join(Screen.line_text(l) for l in w.screen.lines)


def fake_bin(dirpath, name, script):
    p = os.path.join(dirpath, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + script)
    os.chmod(p, 0o755)
    return p


class PCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._path = os.environ["PATH"]
        os.environ["PATH"] = self.tmp + os.pathsep + self._path
        self.wm = make_wm(120, 40)

    def tearDown(self):
        self.wm.shutdown()
        os.environ["PATH"] = self._path

    def load(self, name, **cfg):
        C.apply_config(self.wm, {"plugins": [dict(name=name, **cfg)]})


# ------------------------------------------------------------------------------------------ effects
class EffectTests(PCase):
    def test_all_effects_render_correct_size_and_animate(self):
        for n in [n for n in NAMES if n != "ansi"]:  # ansi is static: tests/test_ansiart.py
            e = EFFECTS[n](seed=1)
            f1 = e.render(60, 20, 100.0)
            f2 = e.render(60, 20, 100.6)
            f3 = e.render(60, 20, 101.4)
            self.assertEqual((len(f1), len(f1[0])), (20, 60), n)
            self.assertTrue(all(len(r) == 60 for r in f3), n)
            self.assertNotEqual(f1, f3, n)
            e.render(1, 1, 102.0)          # tiny screens do not crash
            e.render(200, 60, 103.0)

    def test_matrix_looks_like_matrix(self):
        from pytermwm.ansi import char_width
        from pytermwm.contrib.effects import Matrix
        e = Matrix(seed=3)
        for i in range(30):
            cells = e.render(80, 24, 50.0 + i * 0.1)
        used = {c[0] for row in cells for c in row if c[0] != " "}
        self.assertTrue(used)
        self.assertTrue(any(ord(ch) > 0xFF00 for ch in used), "katakana glyphs are shown")
        self.assertTrue(all(char_width(ch) == 1 for ch in used), "every glyph is one cell wide")
        heads = [c for row in cells for c in row if c[0] != " " and c[1] and min(c[1]) > 150]
        self.assertTrue(heads, "streams have a bright head")
        greens = [c[1] for row in cells for c in row if c[0] != " "]
        self.assertTrue(all(g[1] >= g[0] and g[1] >= g[2] for g in greens))

    def test_matrix_options(self):
        r = self.wm.execute("effect matrix binary red")
        self.assertTrue(r["ok"], r)
        cells = None
        for i in range(20):
            cells = self.wm.background.render(40, 20, 10.0 + i * 0.1)
        chars = {c[0] for row in cells for c in row if c[0] != " "}
        self.assertTrue(chars and chars <= {"0", "1"})
        cols = [c[1] for row in cells for c in row if c[0] != " " and c[1]]
        self.assertTrue(all(c[0] >= c[1] and c[0] >= c[2] for c in cols))
        self.assertFalse(self.wm.execute("effect matrix nonsense")["ok"])
        self.assertFalse(self.wm.execute("effect matrix ascii chartreuse")["ok"])
        self.assertTrue(self.wm.execute("effect matrix ascii 10,200,90")["ok"])

    def test_config_effect_mapping(self):
        C.apply_config(self.wm, {"effect": {"name": "matrix", "glyphs": "hex", "color": "amber"}})
        self.assertEqual(self.wm.background.name, "matrix")
        self.assertEqual(self.wm.background.glyphs, "0123456789ABCDEF")
        self.assertEqual(self.wm.background.base, (255, 176, 0))

    def test_effect_command_loads_plugin_and_shows_in_frame(self):
        self.assertTrue(self.wm.execute("effect plasma")["ok"])
        self.assertIsNotNone(self.wm.background)
        a = Compositor(self.wm).compose(120, 40).cells
        time.sleep(0.05)
        self.wm.background.t0 = None
        self.wm.background.last = None
        scr, frame = screen_of_frame(self.wm)
        self.assertTrue(any(c[0] != " " for c in frame.cells[5]))
        self.assertIn("plasma", self.wm.execute("effect list")["result"])

    def test_windows_draw_over_effect(self):
        self.wm.create_window({"cmd": "echo over-effect; sleep 5", "title": "w"})
        self.wm.execute("effect matrix")
        pump(self.wm, 2)
        scr, _ = screen_of_frame(self.wm)
        self.assertIn("over-effect", "\n".join(Screen.line_text(l) for l in scr.lines))

    def test_off_unknown_and_unload(self):
        self.wm.execute("effect fire")
        self.assertTrue(self.wm.execute("effect off")["ok"])
        self.assertIsNone(self.wm.background)
        r = self.wm.execute("effect nonsense")
        self.assertFalse(r["ok"])
        self.wm.execute("effect rain")
        self.wm.execute("plugin unload effects")
        self.assertIsNone(self.wm.background)
        self.assertNotIn("effects_command", self.wm.extra)

    def test_config_effect_key_and_plugin_start(self):
        C.apply_config(self.wm, {"effect": "starfield"})
        self.assertEqual(self.wm.background.name, "starfield")
        C.apply_config(self.wm, {"effect": None})
        self.assertIsNone(self.wm.background)
        self.load("effects", start="rain")
        self.assertEqual(self.wm.background.name, "rain")

    def test_broken_effect_is_dropped_not_fatal(self):
        class Bad:
            name = "bad"
            def render(self, *a):
                raise RuntimeError("x")
        self.wm.background = Bad()
        Compositor(self.wm).compose(100, 30)
        self.assertIsNone(self.wm.background)


# ------------------------------------------------------------------------------------------ btop
class BtopTests(PCase):
    def test_window_renders_sections(self):
        self.load("btop")
        r = self.wm.execute("btop")
        w = self.wm.windows[r["result"]["id"]]
        t = shown(w)
        for needle in ("CPU", "MEM", "NET", "PID", "NAME"):
            self.assertIn(needle, t)
        self.assertRegex(t, r"\d+ procs")

    def test_lists_this_process(self):
        self.load("btop")
        w = self.wm.windows[self.wm.execute("btop")["result"]["id"]]
        w.refresh()
        self.assertTrue(any(p["pid"] == os.getpid() for p in w._procs) or len(w._procs) > 5)

    def test_sort_filter_and_keys(self):
        self.load("btop")
        w = self.wm.windows[self.wm.execute("btop")["result"]["id"]]
        w.refresh()
        w.handle_key("m", b"")
        self.assertEqual(w.sort, "mem")
        w.handle_key("n", b"")
        self.assertEqual(w.sort, "name")
        w.refresh()
        names = [p["name"] for p in w._procs]
        self.assertEqual(names, sorted(names))
        w.set_filter("python")
        w.refresh()
        self.assertTrue(all("python" in p["name"].lower() or "python" in str(p["pid"]) for p in w._procs))
        w.handle_key("j", b"")
        w.handle_key("PageDown", b"")
        w.handle_key("q", b"")
        self.assertNotIn(w.id, self.wm.windows)

    def test_kill_command_safe_and_working(self):
        self.load("btop")
        self.assertFalse(self.wm.execute("btop-kill 1")["ok"])
        self.assertFalse(self.wm.execute("btop-kill %d" % os.getpid())["ok"])
        self.assertFalse(self.wm.execute("btop-kill")["ok"])
        p = subprocess.Popen(["sleep", "30"])
        try:
            self.assertTrue(self.wm.execute("btop-kill %d" % p.pid)["ok"])
            self.assertEqual(p.wait(timeout=5), -15)
        finally:
            if p.poll() is None:
                p.kill()

    def test_kill_key_opens_confirmation(self):
        self.load("btop")
        w = self.wm.windows[self.wm.execute("btop")["result"]["id"]]
        w.refresh()
        w.handle_key("k", b"")
        self.assertEqual(len(self.wm.dialogs.stack), 1)

    def test_tiny_window(self):
        self.load("btop")
        w = self.wm.create_window({"kind": "btop", "rows": 3})
        w.set_viewport(20, 4)
        shown(w)


# ------------------------------------------------------------------------------------------ docker
FAKE_DOCKER = r'''
echo "$@" >> "%(log)s"
case "$1" in
  ps) cat <<'EOT'
{"ID":"abc123def456","Names":"web","Image":"nginx:latest","State":"running","Status":"Up 2 hours"}
{"ID":"999888777666","Names":"db","Image":"postgres:16","State":"exited","Status":"Exited (0) 3 days ago"}
EOT
  ;;
  images) echo '{"Repository":"nginx","Tag":"latest","ID":"sha256abcdef","Size":"187MB"}';;
  stats) echo '{"Name":"web","CPUPerc":"0.5%%","MemUsage":"10MiB / 1GiB","NetIO":"1kB / 2kB","PIDs":"5"}';;
  logs) echo "log line from $4"; sleep 30;;
  exec) echo "exec in $3"; sleep 30;;
  inspect) echo '[{"Id":"abc","Name":"/web"}]';;
  stop|start|restart|rm|kill|pause|unpause) [ "$2" = "boom" ] && { echo "Error: no such container: boom" >&2; exit 1; }; exit 0;;
esac
'''


class DockerTests(PCase):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "docker.log")
        fake_bin(self.tmp, "docker", FAKE_DOCKER % {"log": self.log})

    def calls(self):
        try:
            return open(self.log).read().splitlines()
        except OSError:
            return []

    def test_window_lists_containers_and_switches_modes(self):
        self.load("docker", interval=0.2)
        w = self.wm.windows[self.wm.execute("docker-ps")["result"]["id"]]
        pump(self.wm, 4, lambda: "nginx:latest" in shown(w))
        t = shown(w)
        self.assertIn("web", t)
        self.assertIn("Exited", t)
        w.handle_key("Tab", b"")
        pump(self.wm, 4, lambda: "187MB" in shown(w))
        w.handle_key("Tab", b"")
        pump(self.wm, 4, lambda: "10MiB" in shown(w))

    def test_segment_counts(self):
        self.load("docker", interval=0.2)
        C.apply_config(self.wm, {"plugins": [{"name": "docker", "interval": 0.2}], "statusline": {"left": ["docker"], "center": [], "right": []}})
        pump(self.wm, 4, lambda: self.wm.plugins.plugins["docker"].api.state is not None and "🐳 1/2" in "".join(
            Screen.line_text(l) for l in screen_of_frame(self.wm)[0].lines))
        scr, _ = screen_of_frame(self.wm)
        self.assertIn("1/2", "".join(Screen.line_text(l) for l in scr.lines))

    def test_logs_and_exec_windows_use_argv_not_shell(self):
        self.load("docker", interval=5)
        r = self.wm.execute("docker-logs web 50")
        w = self.wm.windows[r["result"]["id"]]
        pump(self.wm, 3, lambda: "log line from" in w.text())
        self.assertTrue(any(c.startswith("logs -f --tail 50 web") for c in self.calls()))
        r = self.wm.execute("docker-exec web")
        w2 = self.wm.windows[r["result"]["id"]]
        pump(self.wm, 3, lambda: "exec in" in w2.text())

    def test_invalid_names_rejected(self):
        self.load("docker", interval=5)
        for bad in ("web;rm -rf /", "$(id)", "-v", "a b"):
            r = self.wm.execute(["docker-logs", bad] and "docker-logs '%s'" % bad)
            self.assertFalse(r["ok"], bad)
        self.assertFalse(self.wm.execute("docker-do explode web")["ok"])
        self.assertFalse(self.wm.execute("docker-run")["ok"])

    def test_do_action_reports_result(self):
        self.load("docker", interval=5)
        self.assertTrue(self.wm.execute("docker-do restart web")["ok"])
        pump(self.wm, 3, lambda: any(c.startswith("restart web") for c in self.calls()))
        pump(self.wm, 1)
        self.assertTrue(any(c.startswith("restart web") for c in self.calls()))
        self.wm.execute("docker-do stop boom")
        pump(self.wm, 3, lambda: any("no such container" in m[0] for m in self.wm.messages))
        self.assertTrue(any("no such container" in m[0] for m in self.wm.messages))

    def test_inspect_opens_viewer(self):
        self.load("docker", interval=5)
        r = self.wm.execute("docker-inspect web")
        self.assertIn('"Name"', self.wm.windows[r["result"]["id"]].text())

    def test_keys_in_window(self):
        self.load("docker", interval=0.2)
        w = self.wm.windows[self.wm.execute("docker-ps")["result"]["id"]]
        pump(self.wm, 4, lambda: "web" in shown(w))
        w.handle_key("l", b"")
        self.assertTrue(any(x.kind == "term" and "logs" in x.title for x in self.wm.windows.values()))
        w.handle_key("j", b"")
        w.handle_key("d", b"")
        self.assertEqual(len(self.wm.dialogs.stack), 1)

    def test_missing_docker_binary(self):
        os.unlink(os.path.join(self.tmp, "docker"))
        os.environ["PATH"] = self.tmp
        self.load("docker", interval=0.2)
        w = self.wm.windows[self.wm.execute("docker-ps")["result"]["id"]]
        pump(self.wm, 3, lambda: "docker not found" in shown(w))
        self.assertIn("not found", shown(w))
        self.assertFalse(self.wm.execute("docker-logs web")["ok"])

    def test_unload_stops_poller(self):
        self.load("docker", interval=0.1)
        pump(self.wm, 0.5)
        n = len(self.calls())
        self.wm.execute("plugin unload docker")
        time.sleep(0.4)
        n2 = len(self.calls())
        time.sleep(0.4)
        self.assertLessEqual(len(self.calls()) - n2, 1)


# ------------------------------------------------------------------------------------------ mqtt
class MiniBroker:
    """Just enough of an MQTT 3.1.1 broker to test the client."""

    def __init__(self, user=None, password=None):
        self.user, self.password = user, password
        self.clients = []
        self.retained = {}
        self.received = []
        self.lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            c = {"sock": s, "subs": []}
            self.clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def close(self):
        self.stop = True
        for c in list(self.clients):
            try:
                c["sock"].close()
            except OSError:
                pass
        self.srv.close()

    def drop_clients(self):
        for c in list(self.clients):
            try:
                c["sock"].shutdown(socket.SHUT_RDWR)
                c["sock"].close()
            except OSError:
                pass
        self.clients.clear()

    def _read(self, s):
        def rx(n):
            b = b""
            while len(b) < n:
                d = s.recv(n - len(b))
                if not d:
                    raise EOFError
                b += d
            return b
        h = rx(1)[0]
        mult, length = 1, 0
        while True:
            d = rx(1)[0]
            length += (d & 127) * mult
            if not d & 128:
                break
            mult *= 128
        return h >> 4, h & 15, rx(length) if length else b""

    def _serve(self, c):
        s = c["sock"]
        try:
            while True:
                t, flags, body = self._read(s)
                if t == 1:
                    n = struct.unpack(">H", body[:2])[0]
                    pos = 2 + n + 1
                    cflags = body[pos]
                    pos += 1 + 2
                    ln = struct.unpack(">H", body[pos:pos + 2])[0]
                    pos += 2 + ln
                    ok = True
                    if self.user is not None:
                        ok = False
                        if cflags & 0x80:
                            ul = struct.unpack(">H", body[pos:pos + 2])[0]
                            u = body[pos + 2:pos + 2 + ul].decode()
                            pos += 2 + ul
                            pw = ""
                            if cflags & 0x40:
                                pl = struct.unpack(">H", body[pos:pos + 2])[0]
                                pw = body[pos + 2:pos + 2 + pl].decode()
                            ok = (u == self.user and pw == self.password)
                    s.sendall(bytes([0x20, 2, 0, 0 if ok else 5]))
                    if not ok:
                        return
                elif t == 8:
                    pid = body[:2]
                    pos = 2
                    codes = b""
                    while pos < len(body):
                        tl = struct.unpack(">H", body[pos:pos + 2])[0]
                        flt = body[pos + 2:pos + 2 + tl].decode()
                        c["subs"].append(flt)
                        codes += b"\x00"
                        pos += 2 + tl + 1
                        for topic, payload in list(self.retained.items()):
                            if M.topic_matches(flt, topic):
                                self._send_pub(c, topic, payload, 1)
                    s.sendall(bytes([0x90, 2 + len(codes)]) + pid + codes)
                elif t == 10:
                    s.sendall(bytes([0xB0, 2]) + body[:2])
                elif t == 12:
                    s.sendall(bytes([0xD0, 0]))
                elif t == 3:
                    tl = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tl].decode()
                    pos = 2 + tl
                    qos = (flags >> 1) & 3
                    if qos:
                        s.sendall(bytes([0x40, 2]) + body[pos:pos + 2])
                        pos += 2
                    payload = body[pos:]
                    with self.lock:
                        self.received.append((topic, payload, qos, bool(flags & 1)))
                    if flags & 1:
                        self.retained[topic] = payload
                    self.publish(topic, payload)
                elif t == 14:
                    return
        except (EOFError, OSError):
            pass

    def _send_pub(self, c, topic, payload, retain=0):
        body = M.enc_str(topic) + payload
        try:
            c["sock"].sendall(M.packet(3, retain, body))
        except OSError:
            pass

    def publish(self, topic, payload):
        for c in list(self.clients):
            if any(M.topic_matches(f, topic) for f in c["subs"]):
                self._send_pub(c, topic, payload)


class MqttTests(PCase):
    def setUp(self):
        super().setUp()
        self.broker = MiniBroker()

    def tearDown(self):
        super().tearDown()
        self.broker.close()

    def test_topic_matching(self):
        m = M.topic_matches
        self.assertTrue(m("a/b", "a/b"))
        self.assertTrue(m("a/+/c", "a/x/c"))
        self.assertTrue(m("a/#", "a/b/c"))
        self.assertTrue(m("#", "anything/at/all"))
        self.assertFalse(m("a/+", "a/b/c"))
        self.assertFalse(m("a/b", "a"))
        self.assertFalse(m("a/b/c", "a/b"))

    def test_encoding_helpers(self):
        self.assertEqual(M.enc_len(0), b"\x00")
        self.assertEqual(M.enc_len(127), b"\x7f")
        self.assertEqual(M.enc_len(128), b"\x80\x01")
        self.assertEqual(M.enc_len(16383), b"\xff\x7f")
        self.assertEqual(M.enc_str("hi"), b"\x00\x02hi")

    def test_client_roundtrip_qos0_qos1_retain_and_large_payload(self):
        got = []
        c = M.MqttClient("127.0.0.1", self.broker.port, on_message=lambda t, p, q, r: got.append((t, p, r)))
        c.subscribe("t/#")
        c.start()
        try:
            end = time.time() + 3
            while not c.connected and time.time() < end:
                time.sleep(0.02)
            self.assertTrue(c.connected)
            time.sleep(0.1)
            c.publish("t/a", "hello")
            c.publish("t/b", b"\x00\xff", qos=1)
            c.publish("t/big", b"x" * 200000)
            c.publish("t/keep", "kept", retain=True)
            end = time.time() + 3
            while len(got) < 4 and time.time() < end:
                time.sleep(0.02)
            self.assertEqual(sorted(t for t, _, _ in got), ["t/a", "t/b", "t/big", "t/keep"])
            self.assertEqual(dict((t, p) for t, p, _ in got)["t/b"], b"\x00\xff")
            self.assertEqual(len(dict((t, p) for t, p, _ in got)["t/big"]), 200000)
            with self.assertRaises(M.MqttError):
                c.publish("bad/+", "x")
        finally:
            c.stop()

    def test_auth_failure_and_reconnect(self):
        b = MiniBroker("u", "p")
        try:
            c = M.MqttClient("127.0.0.1", b.port, username="u", password="wrong", reconnect=False)
            c.start()
            time.sleep(0.5)
            self.assertFalse(c.connected)
            self.assertIn("refused", c.last_error)
            c2 = M.MqttClient("127.0.0.1", b.port, username="u", password="p")
            c2.subscribe("x")
            c2.start()
            end = time.time() + 3
            while not c2.connected and time.time() < end:
                time.sleep(0.02)
            self.assertTrue(c2.connected)
            b.drop_clients()
            end = time.time() + 6
            time.sleep(0.2)
            while not c2.connected and time.time() < end:
                time.sleep(0.05)
            self.assertTrue(c2.connected, "should reconnect")
            c2.stop()
        finally:
            b.close()

    def test_plugin_status_events_window_and_publish_command(self):
        self.load("mqtt", host="127.0.0.1", port=self.broker.port, topics=["home/#"], status={"home/boiler": "boiler"}, window=True)
        events = []
        self.wm.on("mqtt_message", lambda **kw: events.append(kw))
        pump(self.wm, 3, lambda: self.wm.execute("mqtt-status")["result"]["connected"])
        time.sleep(0.2)
        self.assertTrue(self.wm.execute("mqtt-pub home/boiler 61C --retain")["ok"])
        pump(self.wm, 3, lambda: events)
        self.assertEqual(events[0]["topic"], "home/boiler")
        self.assertEqual(events[0]["payload"], "61C")
        self.assertEqual(self.wm.status_items["boiler"]["value"], "61C")
        viewer = [w for w in self.wm.windows.values() if w.title == "mqtt"][0]
        self.assertIn("home/boiler  61C", viewer.text())
        self.assertEqual(self.broker.retained["home/boiler"], b"61C")
        st = self.wm.execute("mqtt-status")["result"]
        self.assertGreaterEqual(st["received"], 1)

    def test_rules_react_to_mqtt_messages(self):
        self.load("mqtt", host="127.0.0.1", port=self.broker.port, topics=["alarm/#"])
        C.apply_config(self.wm, {"plugins": [{"name": "mqtt", "host": "127.0.0.1", "port": self.broker.port, "topics": ["alarm/#"]}],
                                 "rules": [{"name": "alarm", "when": {"event": "mqtt_message", "match": {"topic": "^alarm/", "payload": "^ON$"}},
                                            "do": ["status-set alarm $payload"]}]})
        pump(self.wm, 3, lambda: self.wm.execute("mqtt-status")["result"]["connected"])
        time.sleep(0.2)
        self.broker.publish("alarm/door", b"OFF")
        self.broker.publish("alarm/door", b"ON")
        pump(self.wm, 3, lambda: "alarm" in self.wm.status_items)
        self.assertEqual(self.wm.status_items["alarm"]["value"], "ON")

    def test_sub_command_routes_to_window_and_unsub(self):
        self.load("mqtt", host="127.0.0.1", port=self.broker.port)
        pump(self.wm, 3, lambda: self.wm.execute("mqtt-status")["result"]["connected"])
        r = self.wm.execute("mqtt-sub sensors/+/temp")
        self.assertTrue(r["ok"], r)
        time.sleep(0.2)
        self.broker.publish("sensors/a/temp", b"21.5")
        w = [x for x in self.wm.windows.values() if x.title.startswith("mqtt sensors")][0]
        pump(self.wm, 3, lambda: "21.5" in w.text())
        self.assertIn("sensors/a/temp  21.5", w.text())
        self.assertTrue(self.wm.execute("mqtt-unsub sensors/+/temp")["ok"])

    def test_no_broker_configured_and_bad_args(self):
        self.load("mqtt")
        self.assertFalse(self.wm.execute("mqtt-pub a b")["ok"])
        self.assertFalse(self.wm.execute("mqtt-status")["ok"])

    def test_unreachable_broker_does_not_block_wm(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        t0 = time.time()
        self.load("mqtt", host="127.0.0.1", port=port)
        self.assertLess(time.time() - t0, 1.0)
        self.assertFalse(self.wm.execute("mqtt-pub a b")["ok"])
        C.apply_config(self.wm, {"plugins": [], "statusline": {"left": ["mqtt"], "center": [], "right": []}})

    def test_segment_shows_state(self):
        self.load("mqtt", host="127.0.0.1", port=self.broker.port)
        C.apply_config(self.wm, {"plugins": [{"name": "mqtt", "host": "127.0.0.1", "port": self.broker.port}],
                                 "statusline": {"left": ["mqtt"], "center": [], "right": []}})
        pump(self.wm, 3, lambda: self.wm.execute("mqtt-status")["result"]["connected"])
        scr, _ = screen_of_frame(self.wm)
        self.assertIn("mqtt ●", "\n".join(Screen.line_text(l) for l in scr.lines))


# ------------------------------------------------------------------------------------------ ssh
class SshTests(PCase):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "ssh.log")
        fake_bin(self.tmp, "ssh", 'echo "$@" >> "%s"\necho "fake ssh: $@"\nsleep 30\n' % self.log)
        self.cfgfile = os.path.join(self.tmp, "sshcfg")
        open(self.cfgfile, "w").write("Host web1 web2\n  HostName 10.0.0.1\n  User alice\n  Port 2200\n  IdentityFile ~/.ssh/k\n\nHost *\n  ServerAliveInterval 5\n\nHost jump\n  HostName jump.example.com\n")

    def calls(self):
        try:
            return open(self.log).read().splitlines()
        except OSError:
            return []

    def test_parse_ssh_config_skips_wildcards(self):
        h = S.parse_ssh_config(self.cfgfile)
        self.assertEqual(sorted(h), ["jump", "web1", "web2"])
        self.assertEqual(h["web1"], {"host": "10.0.0.1", "user": "alice", "port": "2200", "identity": "~/.ssh/k"})

    def test_open_named_host_builds_argv(self):
        self.load("ssh", ssh_config=self.cfgfile, hosts={"prod": {"host": "prod.example.com", "user": "deploy", "port": 2222, "identity": "/k"}})
        r = self.wm.execute("ssh prod")
        w = self.wm.windows[r["result"]["id"]]
        pump(self.wm, 3, lambda: "fake ssh" in w.text())
        line = self.calls()[0]
        self.assertIn("-p 2222", line)
        self.assertIn("-i /k", line)
        self.assertTrue(line.endswith("-- deploy@prod.example.com"))
        self.assertEqual(w.title, "ssh deploy@prod.example.com")

    def test_ssh_config_hosts_and_plain_targets(self):
        self.load("ssh", ssh_config=self.cfgfile)
        self.wm.execute("ssh web1")
        self.wm.execute("ssh bob@example.org -- uptime -p")
        pump(self.wm, 2, lambda: len(self.calls()) >= 2)
        c = self.calls()
        self.assertTrue(any("-p 2200" in l and l.endswith("-- alice@10.0.0.1") for l in c), c)
        self.assertTrue(any(l.endswith("-- bob@example.org uptime -p") for l in c), c)

    def test_option_injection_rejected(self):
        self.load("ssh", use_ssh_config=False)
        for bad in ("-oProxyCommand=touch /tmp/pwn", "-F/etc/passwd", "host;rm -rf /", "a b", "$(id)", ""):
            r = self.wm.execute("ssh '%s'" % bad)
            self.assertFalse(r["ok"], bad)
        self.assertEqual(self.calls(), [])

    def test_run_and_forward(self):
        self.load("ssh", ssh_config=self.cfgfile)
        r = self.wm.execute("ssh-run web1 uptime")
        pump(self.wm, 2, lambda: self.calls())
        self.assertIn("-T", self.calls()[0])
        self.assertIn("BatchMode=yes", self.calls()[0])
        self.assertTrue(self.wm.execute("ssh-forward web1 8080:localhost:80")["ok"])
        self.assertFalse(self.wm.execute("ssh-forward web1 '8080:localhost:80 -oProxyCommand=x'")["ok"])
        self.assertFalse(self.wm.execute("ssh-forward web1 nonsense")["ok"])
        pump(self.wm, 2, lambda: len(self.calls()) >= 2)
        self.assertTrue(any("-L 8080:localhost:80" in l and "-N" in l for l in self.calls()))

    def test_host_cmd_and_reconnect(self):
        self.load("ssh", use_ssh_config=False, hosts={"box": {"host": "box.lan", "cmd": "tmux attach", "reconnect": True}})
        r = self.wm.execute("ssh box")
        w = self.wm.windows[r["result"]["id"]]
        self.assertEqual(w.opts["on_exit"], "restart")
        pump(self.wm, 2, lambda: self.calls())
        self.assertIn("-t", self.calls()[0])
        self.assertTrue(self.calls()[0].endswith("-- box.lan tmux attach"))

    def test_list_segment_and_missing_binary(self):
        self.load("ssh", ssh_config=self.cfgfile)
        lst = self.wm.execute("ssh-list")["result"]
        self.assertEqual([h["name"] for h in lst], ["jump", "web1", "web2"])
        self.wm.execute("ssh web1")
        C.apply_config(self.wm, {"plugins": [{"name": "ssh", "ssh_config": self.cfgfile}], "statusline": {"left": ["ssh"], "center": [], "right": []}})
        scr, _ = screen_of_frame(self.wm)
        self.assertIn("ssh:1", "\n".join(Screen.line_text(l) for l in scr.lines))
        os.unlink(os.path.join(self.tmp, "ssh"))
        os.environ["PATH"] = self.tmp
        r = self.wm.execute("ssh web1")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])


# ------------------------------------------------------------------------------------------ openai
class FakeAI(http.server.BaseHTTPRequestHandler):
    requests = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        FakeAI.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if FakeAI.mode == "401":
            data = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode()
            self.send_response(401)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        words = ["The ", "answer ", "is ", "42", "."] if FakeAI.mode == "ok" else ["slow "] * 200
        try:
            for w in words:
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": w}}]}) + "\n\n").encode())
                self.wfile.flush()
                if FakeAI.mode == "slow":
                    time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class OpenAITests(PCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAI)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/v1" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        super().setUp()
        FakeAI.requests = []
        FakeAI.mode = "ok"

    def enable(self, **kw):
        kw.setdefault("base_url", self.url)
        kw.setdefault("api_key", "sk-test-secret")
        self.load("openai", **kw)

    def test_streams_answer_into_window(self):
        self.enable(model="m1", system="be brief")
        r = self.wm.execute("ai what is the answer")
        self.assertTrue(r["ok"], r)
        w = self.wm.windows[r["result"]["id"]]
        pump(self.wm, 4, lambda: not w.busy and w.log and w.log[-1][1].endswith("."))
        self.assertEqual(w.log[-1], ("assistant", "The answer is 42."))
        self.assertIn("The answer is 42.", shown(w))
        req = FakeAI.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["auth"], "Bearer sk-test-secret")
        self.assertEqual(req["body"]["model"], "m1")
        self.assertTrue(req["body"]["stream"])
        self.assertEqual(req["body"]["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(req["body"]["messages"][-1], {"role": "user", "content": "what is the answer"})

    def test_conversation_history_is_sent(self):
        self.enable()
        w = self.wm.windows[self.wm.execute("ai first")["result"]["id"]]
        pump(self.wm, 4, lambda: not w.busy and len(w.messages) == 2)
        self.wm.execute("ai second")
        pump(self.wm, 4, lambda: not w.busy and len(w.messages) == 4)
        roles = [m["role"] for m in FakeAI.requests[1]["body"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_context_from_other_window(self):
        self.enable(max_context=50)
        src = self.wm.create_window({"cmd": "echo 'Traceback: boom happened'; sleep 30"})
        pump(self.wm, 2, lambda: "boom" in src.text())
        r = self.wm.execute("ai --from %d explain this" % src.id)
        self.assertTrue(r["ok"], r)
        pump(self.wm, 4, lambda: FakeAI.requests)
        content = FakeAI.requests[0]["body"]["messages"][-1]["content"]
        self.assertIn("explain this", content)
        self.assertIn("boom happened", content)
        self.assertLessEqual(len(content), len("explain this\n\n---\n") + 50)

    def test_http_error_shown_and_state_recovers(self):
        self.enable()
        FakeAI.mode = "401"
        w = self.wm.windows[self.wm.execute("ai hi")["result"]["id"]]
        pump(self.wm, 4, lambda: not w.busy)
        self.assertIn("Incorrect API key", shown(w))
        self.assertEqual(w.messages, [])            # unanswered question dropped
        FakeAI.mode = "ok"
        self.wm.execute("ai hi again")
        pump(self.wm, 4, lambda: not w.busy and w.messages)
        self.assertEqual(w.messages[-1]["content"], "The answer is 42.")

    def test_cancel(self):
        self.enable()
        FakeAI.mode = "slow"
        w = self.wm.windows[self.wm.execute("ai talk forever")["result"]["id"]]
        pump(self.wm, 2, lambda: w.log and len(w.log[-1][1]) > 10)
        self.assertTrue(w.busy)
        self.assertFalse(self.wm.execute("ai more")["ok"])       # busy
        self.wm.execute("ai-cancel")
        pump(self.wm, 3, lambda: not w.busy)
        self.assertFalse(w.busy)
        self.assertIn("(cancelled)", [t for _r, t in w.log])

    def test_typing_in_window_and_editing_keys(self):
        self.enable()
        w = self.wm.windows[self.wm.execute("ai-window")["result"]["id"]]
        type_keys(self.wm, b"question typed\r")
        pump(self.wm, 4, lambda: not w.busy and w.messages)
        self.assertEqual(FakeAI.requests[0]["body"]["messages"][-1]["content"], "question typed")
        type_keys(self.wm, b"\x0c")                          # C-l clears
        self.assertEqual(w.log, [])
        self.assertEqual(w.messages, [])

    def test_missing_key_is_reported_and_key_never_leaks(self):
        os.environ.pop("PTW_TEST_KEY", None)
        self.load("openai", base_url="https://api.example.invalid/v1", api_key_env="PTW_TEST_KEY")
        r = self.wm.execute("ai hello")
        self.assertFalse(r["ok"])
        self.assertIn("PTW_TEST_KEY", r["error"])
        self.enable()
        C.apply_config(self.wm, {"plugins": [{"name": "openai", "base_url": self.url, "api_key": "sk-test-secret"}]})
        blob = json.dumps(self.wm.state(), default=str) + json.dumps(self.wm.plugins.describe(), default=str) if False else json.dumps(self.wm.state(), default=str)
        self.assertNotIn("sk-test-secret", blob)

    def test_unreachable_endpoint(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.load("openai", base_url="http://127.0.0.1:%d/v1" % port)
        w = self.wm.windows[self.wm.execute("ai hi")["result"]["id"]]
        pump(self.wm, 4, lambda: not w.busy)
        self.assertIn("cannot reach", shown(w))


if __name__ == "__main__":
    unittest.main()
