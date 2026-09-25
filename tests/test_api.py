import http.client
import json
import select
import socket
import subprocess
import sys
import threading
import time
import unittest

from tests.helpers import *
from pytermwm.api import WebServer
from pytermwm.mcp import McpServer
from pytermwm.control import handle_op


class Pumper(threading.Thread):
    """Plays the role of the server main loop for a headless WM."""

    def __init__(self, wm):
        super().__init__(daemon=True)
        self.wm = wm
        self.stop = False

    def run(self):
        wm = self.wm
        while not self.stop:
            with wm.lock:
                fds = {fd: (w, s) for fd, w, s in wm.iter_read_fds()}
            r = select.select(list(fds), [], [], 0.02)[0] if fds else []
            if not fds:
                time.sleep(0.02)
            with wm.lock:
                for fd in r:
                    w, s = fds[fd]
                    if w.id in wm.windows:
                        wm.source_readable(w, fd, s)
                wm.poll_sources()
                wm.tick(time.time())
                if wm.dirty:
                    wm.dirty = False
                    wm.frame_seq = getattr(wm, "frame_seq", 0) + 1


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile, os
        cls.tmp = tempfile.mkdtemp()
        os.environ["PYTERMWM_RUNTIME_DIR"] = cls.tmp
        os.environ["PYTERMWM_STATE_DIR"] = cls.tmp
        cls.wm = make_wm(100, 30)
        cls.wm.create_window({"cmd": "echo api-window; sleep 60", "title": "first", "name": "first"})
        cls.web = WebServer(cls.wm, "127.0.0.1", 0, "secret-token-0123456789", "t-api")
        cls.web.start()
        cls.pump = Pumper(cls.wm)
        cls.pump.start()

    @classmethod
    def tearDownClass(cls):
        cls.pump.stop = True
        cls.web.stop()
        cls.wm.shutdown()

    def req(self, method, path, body=None, token=True, headers=None, host=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=10)
        h = dict(headers or {})
        if token is True:
            h["Authorization"] = "Bearer secret-token-0123456789"
        elif token:
            h["Authorization"] = "Bearer " + token
        if host:
            h["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        if raw:
            return r, payload
        try:
            return r.status, json.loads(payload.decode() or "null")
        except ValueError:
            return r.status, payload

    def wait_until(self, fn, timeout=4):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.05)
        self.fail("condition not met")


class AuthTests(ApiCase):
    def test_no_token_rejected(self):
        self.assertEqual(self.req("GET", "/api/state", token=False)[0], 401)

    def test_wrong_token_rejected(self):
        self.assertEqual(self.req("GET", "/api/state", token="nope")[0], 401)
        self.assertEqual(self.req("POST", "/api/command", {"line": "layout grid"}, token="nope")[0], 401)

    def test_bearer_ok(self):
        st, body = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

    def test_query_token_sets_strict_cookie_then_cookie_works(self):
        r, _ = self.req("GET", "/api/state?token=secret-token-0123456789", token=False, raw=True)
        self.assertEqual(r.status, 200)
        sc = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)
        cookie = sc.split(";")[0]
        st, _ = self.req("GET", "/api/state", token=False, headers={"Cookie": cookie})
        self.assertEqual(st, 200)

    def test_cookie_post_needs_same_origin_and_json(self):
        cookie = {"Cookie": "ptw_token=secret-token-0123456789"}
        st, _ = self.req("POST", "/api/command", {"line": "message x"}, token=False,
                         headers=dict(cookie, Origin="http://evil.example"))
        self.assertEqual(st, 403)
        st, _ = self.req("POST", "/api/command", {"line": "message x"}, token=False,
                         headers=dict(cookie, Origin="http://127.0.0.1:%d" % self.web.port))
        self.assertEqual(st, 200)
        c = http.client.HTTPConnection("127.0.0.1", self.web.port)
        c.request("POST", "/api/command", '{"line":"message x"}', dict(cookie, **{"Content-Type": "text/plain"}))
        self.assertEqual(c.getresponse().status, 403)

    def test_bad_host_header_rejected(self):
        st, _ = self.req("GET", "/api/state", host="evil.example")
        self.assertEqual(st, 403)

    def test_token_in_url_is_not_needed_for_bearer_and_timing_safe_compare_used(self):
        import inspect, pytermwm.api as A
        self.assertIn("compare_digest", inspect.getsource(A))


class ApiTests(ApiCase):
    def test_state_windows_commands(self):
        st, b = self.req("GET", "/api/windows")
        self.assertEqual(b["windows"][0]["title"], "first")
        st, b = self.req("GET", "/api/commands")
        self.assertIn("new-window", [c["name"] for c in b["commands"]])
        st, b = self.req("GET", "/api/themes")
        self.assertIn("hacker", b["themes"])

    def test_command_and_error(self):
        st, b = self.req("POST", "/api/command", {"line": "layout grid"})
        self.assertTrue(b["ok"])
        self.assertEqual(self.wm.desk.layout, "grid")
        st, b = self.req("POST", "/api/command", {"line": "bogus"})
        self.assertEqual(st, 400)
        self.assertIn("unknown command", b["error"])
        self.req("POST", "/api/command", {"line": "layout tile"})

    def test_desktops_via_state_and_commands(self):
        # Regression / new feature: the web UI's Screen tab needs a way to list, switch,
        # create, rename and close desktops, and to move a window between them -- all of
        # which route through the existing `command` op plus the `state`/`windows` reads.
        st, b = self.req("POST", "/api/op", {"op": "create", "spec": {"cmd": "sleep 60", "title": "deskwin"}})
        wid = b["id"]
        try:
            st, b = self.req("GET", "/api/state")
            n0 = len(b["state"]["desktops"])
            self.assertEqual(b["state"]["desktops"][b["state"]["current_desktop"]]["name"], "main")

            st, b = self.req("POST", "/api/command", {"line": "new-desktop work"})
            self.assertTrue(b["ok"], b)
            st, b = self.req("GET", "/api/state")
            self.assertEqual(len(b["state"]["desktops"]), n0 + 1)
            cur = b["state"]["desktops"][b["state"]["current_desktop"]]
            self.assertEqual(cur["name"], "work")

            st, b = self.req("POST", "/api/command", {"line": "send-to-desktop work %d" % wid})
            self.assertTrue(b["ok"], b)
            st, b = self.req("GET", "/api/windows")
            w = next(x for x in b["windows"] if x["id"] == wid)
            self.assertEqual(w["desktop"], "work")

            st, b = self.req("POST", "/api/command", {"line": "rename-desktop staging"})
            self.assertTrue(b["ok"], b)
            st, b = self.req("GET", "/api/state")
            self.assertEqual(b["state"]["desktops"][b["state"]["current_desktop"]]["name"], "staging")

            st, b = self.req("POST", "/api/command", {"line": "close-desktop"})
            self.assertTrue(b["ok"], b)
            st, b = self.req("GET", "/api/state")
            self.assertEqual(len(b["state"]["desktops"]), n0)
            self.assertEqual(b["state"]["desktops"][b["state"]["current_desktop"]]["name"], "main")
            st, b = self.req("GET", "/api/windows")
            self.assertNotIn(wid, [x["id"] for x in b["windows"]])   # closed along with its desktop
        finally:
            st, b = self.req("GET", "/api/state")
            if b["state"]["desktops"][b["state"]["current_desktop"]]["name"] != "main":
                self.req("POST", "/api/command", {"line": "desktop main"})

    def test_create_send_capture(self):
        st, b = self.req("POST", "/api/op", {"op": "create", "spec": {"cmd": "cat", "title": "catw"}})
        wid = b["id"]
        self.req("POST", "/api/window/%d/send" % wid, {"text": "hello-cat", "enter": True})
        self.wait_until(lambda: "hello-cat" in self.req("GET", "/api/window/%d/text" % wid)[1]["text"])
        self.req("POST", "/api/window/%d/send" % wid, {"keys": ["C-d"]})
        st, b = self.req("DELETE", "/api/window/%d" % wid)
        self.assertEqual(st, 200)

    def test_frame_json_structure(self):
        st, b = self.req("GET", "/api/frame")
        self.assertEqual(b["cols"], self.wm.cols)
        self.assertEqual(len(b["lines"]), self.wm.rows)
        text = "".join(run[0] for run in b["lines"][1])
        self.assertTrue(text)

    def test_input_keys_reach_wm(self):
        n = len(self.wm.windows)
        self.req("POST", "/api/input", {"data": "\x1b\r"})     # M-Enter
        self.assertEqual(len(self.wm.windows), n + 1)
        self.req("POST", "/api/command", {"line": "close-window"})

    def test_config_validate_and_put(self):
        st, b = self.req("POST", "/api/config/validate", {"text": "layout: nope\n"})
        self.assertFalse(b["valid"])
        st, b = self.req("POST", "/api/config/validate", {"text": "layout: grid\n"})
        self.assertTrue(b["valid"])
        st, b = self.req("PUT", "/api/config", {"text": "layout: nope\n"})
        self.assertEqual(st, 400)
        st, b = self.req("GET", "/api/config")
        self.assertIn("text", b)

    def test_scripts_roundtrip_in_isolated_dir(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        old = os.getcwd()
        os.chdir(d)
        try:
            st, b = self.req("PUT", "/api/script/hello.py", {"text": "api.command('web-script', lambda wm, a: 'yes')\n"})
            self.assertEqual(st, 200, b)
            st, b = self.req("GET", "/api/script/hello.py")
            self.assertIn("web-script", b["text"])
            st, b = self.req("GET", "/api/scripts")
            self.assertIn("hello.py", b["scripts"])
            self.assertEqual(self.req("PUT", "/api/script/bad.py", {"text": "def (:"})[0], 400)
            self.assertEqual(self.req("PUT", "/api/script/..%2Fevil.py", {"text": "x=1"})[0], 400)
            self.wait_until(lambda: self.wm.execute("web-script").get("ok"))
        finally:
            os.chdir(old)
            self.wm.rules.script_paths = []
            self.wm.rules.reload_scripts()

    def test_unknown_endpoint_and_bad_json(self):
        self.assertEqual(self.req("GET", "/api/nothing")[0], 404)
        c = http.client.HTTPConnection("127.0.0.1", self.web.port)
        c.request("POST", "/api/command", "{not json", {"Authorization": "Bearer secret-token-0123456789"})
        self.assertEqual(c.getresponse().status, 400)

    def test_static_path_traversal_blocked(self):
        for p in ("/../api.py", "/%2e%2e/api.py", "/static/../../api.py"):
            st, _ = self.req("GET", p)
            self.assertEqual(st, 404, p)

    def test_rules_plugins_logs_help(self):
        self.assertEqual(self.req("GET", "/api/rules")[0], 200)
        st, b = self.req("GET", "/api/plugins")
        self.assertEqual(st, 200)
        # Regression: the plugins list must be populated even when no plugin action
        # has run yet this session (wm.plugins starts out lazily-initialized as None).
        self.assertTrue(b["plugins"], b)
        self.assertTrue(any(p["name"] == "btop" for p in b["plugins"]), b)
        # Regression: contrib helper modules with no setup(api) (e.g. ansiart.py, used internally
        # by the effects plugin) must not be offered as loadable plugins in the web UI.
        self.assertFalse(any(p["name"] == "ansiart" for p in b["plugins"]), b)
        self.assertEqual(self.req("GET", "/api/logs?n=5")[0], 200)
        st, b = self.req("GET", "/api/help/keys")
        self.assertIn("text", b)

    def test_resize_without_terminal(self):
        st, b = self.req("POST", "/api/resize", {"cols": 120, "rows": 40})
        self.assertEqual((b["cols"], b["rows"]), (120, 40))
        self.req("POST", "/api/resize", {"cols": 100, "rows": 30})


class StreamTests(ApiCase):
    def read_events(self, n, timeout=5, action=None):
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=timeout)
        c.request("GET", "/api/stream", headers={"Authorization": "Bearer secret-token-0123456789"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("Content-Type"))
        out = []
        cur = {}
        acted = False
        end = time.time() + timeout
        while len(out) < n and time.time() < end:
            line = r.fp.readline().decode()
            if not line:
                break
            line = line.rstrip("\n")
            if line.startswith("event: "):
                cur["event"] = line[7:]
            elif line.startswith("data: "):
                cur["data"] = json.loads(line[6:])
            elif line == "" and cur:
                out.append(cur)
                cur = {}
                if action and not acted:
                    acted = True
                    action()
        c.close()
        return out

    def test_initial_frame_then_update_after_change(self):
        evs = self.read_events(3, action=lambda: self.wm.execute("message streamed-msg 5"))
        frames = [e for e in evs if e["event"] == "frame"]
        self.assertGreaterEqual(len(frames), 2)
        joined = json.dumps(frames[-1]["data"])
        self.assertIn("streamed-msg", joined)

    def test_events_are_streamed(self):
        evs = self.read_events(12, timeout=6, action=lambda: self.wm.create_window({"cmd": "sleep 5", "title": "ev-win"}))
        self.assertTrue(any(e["event"] == "event" and e["data"]["event"] == "window_created" for e in evs), evs)


class McpHttpTests(ApiCase):
    def rpc(self, method, params=None, mid=1):
        st, b = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        return b

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        return r["result"]

    def test_initialize_and_tools_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "pytermwm")
        self.assertIn("tools", r["result"]["capabilities"])
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        for n in ("run_command", "new_window", "send_keys", "capture_window", "wait_for_output", "screenshot"):
            self.assertIn(n, names)
        for t in self.rpc("tools/list")["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_notification_has_no_reply(self):
        st, b = self.req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, raw=False)
        self.assertEqual(st, 202)

    def test_open_type_wait_capture(self):
        r = self.call("new_window", spec={"cmd": "cat", "title": "mcpcat"})
        self.assertFalse(r["isError"], r)
        wid = json.loads(r["content"][0]["text"])["id"]
        self.call("send_keys", window=wid, text="ping-from-mcp", enter=True)
        r = self.call("wait_for_output", window=wid, pattern="ping-from-mcp", timeout=5)
        self.assertFalse(r["isError"], r)
        r = self.call("capture_window", window=wid)
        self.assertIn("ping-from-mcp", r["content"][0]["text"])
        self.call("close_window", window=wid)

    def test_wait_timeout_is_a_tool_error(self):
        r = self.call("wait_for_output", pattern="never-appears-xyz", timeout=0.4)
        self.assertTrue(r["isError"])

    def test_run_command_error_is_tool_error_not_rpc_error(self):
        r = self.rpc("tools/call", {"name": "run_command", "arguments": {"line": "bogus-cmd"}})
        self.assertNotIn("error", r)
        self.assertTrue(r["result"]["isError"])

    def test_screenshot_and_state_and_help(self):
        self.assertTrue(self.call("screenshot")["content"][0]["text"].strip())
        self.assertIn("desktops", self.call("get_state")["content"][0]["text"])
        self.assertIn("pytermwm", self.call("get_help", topic="index")["content"][0]["text"])
        self.assertFalse(self.call("get_logs", n=5)["isError"])

    def test_unknown_tool_and_method(self):
        self.assertEqual(self.rpc("tools/call", {"name": "rm_rf", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.rpc("nope/nope")["error"]["code"], -32601)

    def test_missing_argument(self):
        r = self.call("run_command")
        self.assertTrue(r["isError"])

    def test_resources(self):
        r = self.rpc("resources/list")["result"]["resources"]
        uris = [x["uri"] for x in r]
        self.assertIn("pytermwm://state", uris)
        self.assertTrue(any(u.startswith("pytermwm://window/") for u in uris))
        wid = [u for u in uris if u.startswith("pytermwm://window/")][0]
        c = self.rpc("resources/read", {"uri": wid})["result"]["contents"][0]
        self.assertIn("text", c)
        c = self.rpc("resources/read", {"uri": "pytermwm://state"})["result"]["contents"][0]
        json.loads(c["text"])
        self.assertIn("error", self.rpc("resources/read", {"uri": "http://evil"}))

    def test_batch(self):
        st, b = self.req("POST", "/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual([x["id"] for x in b], [1, 2])

    def test_requires_auth(self):
        st, _ = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=False)
        self.assertEqual(st, 401)


class McpUnitTests(unittest.TestCase):
    def test_invalid_request(self):
        s = McpServer(lambda r: {"ok": True})
        self.assertEqual(s.handle({"id": 1})["error"]["code"], -32600)
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_command"}})["result"]["isError"], True)


if __name__ == "__main__":
    unittest.main()


class HostCheckTests(unittest.TestCase):
    """The Host header check (DNS rebinding defence) and the URL that is shown to the user."""

    def setUp(self):
        self.wm = make_wm(80, 24)

    def tearDown(self):
        self.wm.shutdown()

    def web(self, host, **cfg):
        self.wm.cfg["web"] = dict(cfg)
        return WebServer(self.wm, host, 0, "secret-token-0123456789", "t-host")

    def test_loopback_binding_only_accepts_loopback_and_listed_names(self):
        w = self.web("127.0.0.1", allowed_hosts=["pytermwm.lan", "*.example.org"])
        for ok in ("127.0.0.1", "localhost", "[::1]", "PYTERMWM.LAN", "a.example.org"):
            self.assertTrue(w.host_allowed(ok), ok)
        for bad in ("mcp.lan", "192.168.1.5", "evil.com", "example.org", "mcp"):
            self.assertFalse(w.host_allowed(bad), bad)

    def test_wildcard_binding_accepts_this_machine_but_not_foreign_names(self):
        w = self.web("0.0.0.0")
        for ok in ("mcp.lan", "192.168.1.5", "10.0.0.2", "mcp", "printer.local", "nas.home.arpa", "127.0.0.1", "[fe80::1]",
                   socket.gethostname()):
            self.assertTrue(w.host_allowed(ok), ok)
        for bad in ("evil.com", "www.example.org", "attacker.lan.evil.com", "127.0.0.1.evil.com", "localhost.evil.com"):
            self.assertFalse(w.host_allowed(bad), bad)

    def test_public_host_and_patterns_work_when_exposed_too(self):
        w = self.web("0.0.0.0", public_host="ptw.example.org", allowed_hosts=["*.corp.example.com"])
        self.assertTrue(w.host_allowed("ptw.example.org"))
        self.assertTrue(w.host_allowed("x.corp.example.com"))
        self.assertFalse(w.host_allowed("other.example.org"))

    def test_url_uses_the_machine_name_for_wildcard_binding(self):
        w = self.web("0.0.0.0")
        w.port = 8001
        host = w.url.split("//")[1].split(":")[0]
        self.assertNotIn(host, ("0.0.0.0", "127.0.0.1", ""))
        self.assertIn(host.split(".")[0].lower(), w.own_names())
        self.assertTrue(w.url.endswith(":8001/?token=secret-token-0123456789"))
        self.assertTrue(w.host_allowed(host), "the URL we print must be accepted by ourselves")
        self.assertEqual(self.web("127.0.0.1").url.split("/?")[0], "http://127.0.0.1:0")
        self.assertEqual(self.web("192.168.1.5").display_host(), "192.168.1.5")
        self.assertEqual(self.web("0.0.0.0", public_host="mcp.lan").display_host(), "mcp.lan")

    def test_over_http_a_foreign_host_is_refused_and_a_local_name_accepted(self):
        w = self.web("0.0.0.0", allowed_hosts=[])
        w.start()
        try:
            for host, expected in (("mcp.lan:%d" % w.port, 200), ("evil.example.com:%d" % w.port, 403)):
                c = http.client.HTTPConnection("127.0.0.1", w.port, timeout=10)
                c.request("GET", "/api/state", headers={"Host": host, "Authorization": "Bearer secret-token-0123456789"})
                r = c.getresponse()
                body = r.read()
                c.close()
                self.assertEqual(r.status, expected, (host, body))
                if expected != 200:
                    self.assertIn(b"bad host", body)
        finally:
            w.stop()
