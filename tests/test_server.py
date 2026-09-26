"""End-to-end tests: a real daemon, the CLI as control client, and a pty-attached client."""
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from pytermwm.ansi import Screen


def wins(st):
    return list(st["windows"].values())


class DaemonCase(unittest.TestCase):
    session = "t-e2e"
    config_text = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ptw-")
        cls.env = dict(os.environ, PYTERMWM_RUNTIME_DIR=os.path.join(cls.tmp, "run"),
                       PYTERMWM_STATE_DIR=os.path.join(cls.tmp, "state"), HOME=cls.tmp,
                       PYTHONPATH=ROOT, SHELL="/bin/sh", TERM="xterm-256color")
        os.makedirs(cls.env["PYTERMWM_RUNTIME_DIR"], exist_ok=True)
        args = []
        if cls.config_text is not None:
            cls.cfg = os.path.join(cls.tmp, "cfg.yml")
            with open(cls.cfg, "w") as f:
                f.write(cls.config_text)
            args = ["-c", cls.cfg]
        r = cls.cli(*(["-s", cls.session] + args + ["start"]), check=False)
        assert r.returncode == 0, r.stderr
        cls.wait_for_socket()

    @classmethod
    def tearDownClass(cls):
        cls.cli("-s", cls.session, "kill", check=False)
        time.sleep(0.2)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def wait_for_socket(cls, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            r = cls.cli("-s", cls.session, "ls", check=False)
            if r.returncode == 0:
                return
            time.sleep(0.1)
        raise AssertionError("server did not come up")

    @classmethod
    def cli(cls, *args, check=True, timeout=20, input=None):
        r = subprocess.run([sys.executable, "-m", "pytermwm"] + list(args), env=cls.env, capture_output=True,
                           text=True, timeout=timeout, cwd=ROOT, input=input)
        if check and r.returncode != 0:
            raise AssertionError("cli %s failed: %s %s" % (args, r.stdout, r.stderr))
        return r

    def s(self, *args, **kw):
        return self.cli("-s", self.session, *args, **kw)

    def wait_text(self, win, needle, timeout=6.0):
        end = time.time() + timeout
        out = ""
        while time.time() < end:
            out = self.s("capture", "-t", str(win), check=False).stdout
            if needle in out:
                return out
            time.sleep(0.1)
        self.fail("%r not seen in window %s; got %r" % (needle, win, out))

    def new_window(self, *cmd, extra=()):
        r = self.s("ctl", "new-window", *extra, "--", *cmd)
        return int(json.loads(r.stdout)["id"]) if r.stdout.strip().startswith("{") else self._last_id()

    def _last_id(self):
        st = json.loads(self.s("state").stdout)
        return max(w["id"] for w in wins(st))


class ControlTests(DaemonCase):
    def test_sessions_lists_and_state_is_json(self):
        self.assertIn(self.session, self.cli("sessions").stdout)
        st = json.loads(self.s("state").stdout)
        self.assertEqual(st["session"], self.session)
        self.assertIn("desktops", st)

    def test_run_capture_send_roundtrip(self):
        r = self.s("run", "--", "sh", "-c", "echo e2e-hello; cat")
        wid = self._last_id()
        self.wait_text(wid, "e2e-hello")
        self.s("send", "-t", str(wid), "typed-input", "Enter")
        self.wait_text(wid, "typed-input")

    def test_wait_command_matches_regex(self):
        self.s("run", "--", "sh", "-c", "sleep 0.3; echo READY-42; sleep 20")
        wid = self._last_id()
        r = self.s("wait", "-t", str(wid), "READY-[0-9]+", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_ctl_unknown_command_reports_error(self):
        r = self.s("ctl", "no-such-command", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown command", r.stderr + r.stdout)

    def test_status_item_and_frame(self):
        self.s("status", "deploy", "green")
        frame = self.s("frame").stdout
        self.assertIn("green", frame)

    def test_logs_available(self):
        self.s("ctl", "message", "hello-log")
        self.assertTrue(self.s("logs").stdout.strip() != "")

    def test_pipe_stdin_into_viewer(self):
        self.cli("-s", self.session, "pipe", "-t", "piped", input="line-a\nline-b\n")
        st = json.loads(self.s("state").stdout)
        w = [w for w in wins(st) if w.get("title") == "piped" or w.get("name") == "piped"]
        self.assertTrue(w, [x.get("title") for x in wins(st)])
        self.wait_text(w[0]["id"], "line-b")

    def test_layout_and_desktop_via_ctl(self):
        self.s("ctl", "layout", "grid")
        self.s("ctl", "new-desktop", "e2e")
        st = json.loads(self.s("state").stdout)
        self.assertIn("e2e", [d["name"] for d in st["desktops"]])
        self.s("ctl", "desktop", "1")

    def test_raw_socket_protocol(self):
        from pytermwm.protocol import ControlClient
        old = dict(os.environ)
        os.environ.update({k: self.env[k] for k in ("PYTERMWM_RUNTIME_DIR", "PYTERMWM_STATE_DIR")})
        try:
            c = ControlClient(self.session)
            self.assertTrue(c.request({"op": "ping"})["ok"])
            r = c.request({"op": "command", "line": "nope-nope"})
            self.assertFalse(r["ok"])
            r = c.request({"op": "windows"})
            self.assertTrue(r["ok"])
            c.close()
        finally:
            os.environ.clear()
            os.environ.update(old)


class Attached:
    """A real client process attached to the session over a pty; decodes what it draws."""

    def __init__(self, env, session, cols=100, rows=30):
        self.cols, self.rows = cols, rows
        self.screen = Screen(rows, cols, 0)
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(ROOT)
            os.execvpe(sys.executable, [sys.executable, "-m", "pytermwm", "-s", session, "attach"], env)
        self.pid, self.fd = pid, fd
        import fcntl, struct, termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(pid, signal.SIGWINCH)
        self.exited = None

    def pump(self, timeout=0.5, until=None):
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    self.exited = True
                    return False
                self.screen.feed(data.decode("utf-8", "replace"))
            if until and until():
                return True
        return until() if until else True

    def text(self):
        return "\n".join(Screen.line_text(l) for l in self.screen.lines)

    def send(self, data: bytes):
        os.write(self.fd, data)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class AttachTests(DaemonCase):
    session = "t-attach"

    def test_attach_renders_types_and_detaches(self):
        c = Attached(self.env, self.session)
        try:
            self.assertTrue(c.pump(6, lambda: "main" in c.text()), c.text())
            self.s("run", "--", "sh", "-c", "echo attach-visible; sleep 30")
            self.assertTrue(c.pump(5, lambda: "attach-visible" in c.text()), c.text())
            c.send(b"\x1b\r")           # M-Enter: new window
            self.assertTrue(c.pump(5, lambda: len(wins(json.loads(self.s("state").stdout))) >= 3))
            c.send(b"echo typed-in-attach\r")
            self.assertTrue(c.pump(5, lambda: "typed-in-attach" in c.text().replace("echo typed-in-attach", "")), c.text())
            c.send(b"\x02d")            # prefix d = detach
            c.pump(3, lambda: c.exited)
            self.assertTrue(c.exited)
            # session (and windows) survive
            self.assertGreaterEqual(len(wins(json.loads(self.s("state").stdout))), 3)
            # reattach shows the same content
            c2 = Attached(self.env, self.session)
            try:
                self.assertTrue(c2.pump(5, lambda: "attach-visible" in c2.text()), c2.text())
            finally:
                c2.close()
        finally:
            c.close()

    def test_ptw_inside_its_own_session_is_refused_and_the_session_survives(self):
        # the reported crash: `python ptw.py` in a shell inside the session attached to that same session
        ptw = os.path.join(ROOT, "ptw.py")
        self.s("run", "--name", "nest", "--", sys.executable, "-c",
               "import subprocess, sys; r = subprocess.call([sys.executable, %r]); print('nest-rc=%%d' %% r); "
               "import time; time.sleep(30)" % ptw)
        out = self.wait_text("nest", "nest-rc=1", timeout=15)
        self.assertIn("inside session", out.replace("\n", ""))           # the message wraps in a narrow window
        self.assertEqual(self.s("ls").returncode, 0)                    # the window manager is still alive
        self.assertIn("nest-rc=1", self.s("capture", "-t", "nest").stdout)

    def test_server_refuses_a_client_from_its_own_window(self):
        # an older client (no CLI guard) that says it runs inside this session gets EXIT, not the screen
        from pytermwm import protocol as P
        old = dict(os.environ)
        os.environ.update({k: self.env[k] for k in ("PYTERMWM_RUNTIME_DIR", "PYTERMWM_STATE_DIR")})
        try:
            sock = P.connect(self.session)
            sock.sendall(P.pack_json(P.HELLO, {"cols": 80, "rows": 24, "inside": self.session}))
            mb, got, end = P.MessageBuffer(), [], time.time() + 5
            sock.settimeout(1)
            while time.time() < end and not any(k == P.EXIT for k, _ in got):
                try:
                    data = sock.recv(65536)
                except OSError:
                    continue
                if not data:
                    break
                got.extend(mb.feed(data))
            sock.close()
        finally:
            os.environ.clear()
            os.environ.update(old)
        kinds = [k for k, _ in got]
        self.assertIn(P.EXIT, kinds)
        self.assertNotIn(P.OUTPUT, kinds)
        reason = json.loads([p for k, p in got if k == P.EXIT][0].decode())["reason"]
        self.assertIn("inside session", reason)
        self.assertEqual(self.s("ls").returncode, 0)

    def test_two_clients_share_session_and_resize(self):
        a = Attached(self.env, self.session, 100, 30)
        b = Attached(self.env, self.session, 70, 20)
        try:
            self.assertTrue(a.pump(6, lambda: "main" in a.text()))
            self.assertTrue(b.pump(6, lambda: "main" in b.text()))
            self.s("status", "shared", "both")
            self.assertTrue(a.pump(4, lambda: "both" in a.text()), a.text())
            self.assertTrue(b.pump(4, lambda: "both" in b.text()), b.text())
        finally:
            a.close()
            b.close()


class ChartGlyphAttachTests(DaemonCase):
    """A real daemon, attached over a pty exactly like the bug report (Linux console / PuTTY): the CPU status
    segment must never draw the fine 1/8-cell Unicode blocks that font is missing (see docs/configuration.md
    `charts:`), whether that is because the attaching terminal hinted so (TERM=linux) or because the config says so."""
    session = "t-chart-glyphs"

    RISKY = "▁▂▃▄▅▆▇▏▎▍▌▋▊▉"

    @staticmethod
    def cpu_glyphs(text):
        import re
        m = re.search(r"CPU\s*\d+%\s*(\S*)", text)
        return m.group(1) if m else ""

    def test_term_linux_hint_avoids_the_missing_glyphs(self):
        env = dict(self.env, TERM="linux")
        c = Attached(env, self.session)
        try:
            self.assertTrue(c.pump(6, lambda: "CPU" in c.text()), c.text())
            for _ in range(10):                                      # sample across a few status refreshes
                c.pump(0.6)
                g = self.cpu_glyphs(c.text())
                self.assertFalse(any(ch in self.RISKY for ch in g), (g, c.text()))
        finally:
            c.close()


class ChartGlyphsConfigTests(DaemonCase):
    session = "t-chart-glyphs-cfg"
    config_text = "charts: {glyphs: ascii}\n"
    RISKY = "▁▂▃▄▅▆▇▏▎▍▌▋▊▉░▒▓"

    def test_config_override_forces_ascii_even_on_a_normal_terminal(self):
        c = Attached(self.env, self.session)                          # self.env has TERM=xterm-256color
        try:
            self.assertTrue(c.pump(6, lambda: "CPU" in c.text()), c.text())
            m = None
            import re
            for _ in range(10):
                c.pump(0.6)
                m = re.search(r"CPU\s*\d+%\s*(\S*)", c.text())
                if m and m.group(1):
                    break
            self.assertTrue(m and m.group(1), c.text())
            g = m.group(1)
            self.assertFalse(any(ch in self.RISKY for ch in g), (g, c.text()))
            self.assertTrue(all(ord(ch) < 128 for ch in g), g)
        finally:
            c.close()


class SessionTests(DaemonCase):
    session = "t-sess"

    def test_save_and_restore(self):
        self.s("run", "--", "sh", "-c", "echo before-save; sleep 60")
        self.s("ctl", "layout", "rows")
        self.s("ctl", "rename", "persisted title")
        path = os.path.join(self.tmp, "saved.json")
        self.s("save", path)
        data = json.load(open(path))
        self.assertIn("desktops", data)
        self.s("kill")
        time.sleep(0.3)
        self.cli("-s", "t-sess2", "restore", path)
        self.__class__.session_restored = "t-sess2"
        try:
            end = time.time() + 6
            st = None
            while time.time() < end:
                r = self.cli("-s", "t-sess2", "state", check=False)
                if r.returncode == 0:
                    st = json.loads(r.stdout)
                    break
                time.sleep(0.2)
            self.assertIsNotNone(st)
            titles = [w["title"] for w in wins(st)]
            self.assertIn("persisted title", titles)
            self.assertEqual(st["desktops"][0]["layout"], "rows")
        finally:
            self.cli("-s", "t-sess2", "kill", check=False)


class ConfigTests(DaemonCase):
    session = "t-cfg"
    config_text = "layout: rows\nwindows:\n  - {name: one, cmd: 'echo cfg-one; sleep 60', keep: true}\nstatusline:\n  right: [time]\n"

    def test_config_windows_and_live_reload(self):
        st = json.loads(self.s("state").stdout)
        self.assertEqual([w["name"] for w in wins(st)], ["one"])
        self.assertEqual(st["desktops"][0]["layout"], "rows")
        with open(self.cfg, "w") as f:
            f.write(self.config_text.replace("statusline:", "  - {name: two, cmd: 'echo cfg-two; sleep 60', keep: true}\nstatusline:"))
        os.utime(self.cfg, (time.time() + 3, time.time() + 3))
        end = time.time() + 6
        names = []
        while time.time() < end:
            names = [w["name"] for w in wins(json.loads(self.s("state").stdout))]
            if "two" in names:
                break
            time.sleep(0.2)
        self.assertIn("two", names)

    def test_check_config(self):
        good = os.path.join(self.tmp, "good.yml")
        bad = os.path.join(self.tmp, "bad.yml")
        open(good, "w").write("layout: grid\n")
        open(bad, "w").write("layout: nonsense\n")
        self.assertEqual(self.cli("check-config", good).returncode, 0)
        r = self.cli("check-config", bad, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("layout", r.stdout + r.stderr)


class WebAndMcpTests(DaemonCase):
    session = "t-web"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # restart with the web interface on a free port
        cls.cli("-s", cls.session, "kill", check=False)
        time.sleep(0.3)
        r = cls.cli("-s", cls.session, "--web=0", "start", check=False)
        assert r.returncode == 0, r.stderr
        cls.wait_for_socket()

    def test_web_url_and_api_over_http(self):
        import urllib.request
        url = self.s("web").stdout.strip()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        base, _, token = url.partition("/?token=")
        req = urllib.request.Request(base + "/api/state", headers={"Authorization": "Bearer " + token})
        data = json.load(urllib.request.urlopen(req, timeout=5))
        self.assertEqual(data["state"]["session"], self.session)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/api/state", timeout=5)
        self.assertEqual(cm.exception.code, 401)

    def test_mcp_stdio(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "new_window", "arguments": {"spec": {"cmd": ["sh", "-c", "echo mcp-stdio-ok; sleep 30"]}}}},
        ]
        r = self.cli("-s", self.session, "mcp", input="\n".join(json.dumps(m) for m in msgs) + "\n")
        lines = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual([m["id"] for m in lines], [1, 2])
        wid = json.loads(lines[1]["result"]["content"][0]["text"])["id"]
        self.wait_text(wid, "mcp-stdio-ok")

    def test_version_reports_the_running_server(self):
        # a session keeps running the code it was started with; `version` shows what each session's server runs
        from pytermwm import __version__
        r = self.cli("-s", self.session, "version")
        self.assertEqual(r.stdout.splitlines()[0], "pytermwm %s" % __version__)       # first line as before
        self.assertIn("session %s" % self.session, r.stdout)
        self.assertIn("server %s (pid" % __version__, r.stdout)
        self.assertNotIn("warning", r.stderr)
        self.assertEqual(self.cli("version", "--check").returncode, 0)

    def test_hello_from_another_version_shows_a_warning(self):
        from pytermwm import protocol as P
        old = dict(os.environ)
        os.environ.update({k: self.env[k] for k in ("PYTERMWM_RUNTIME_DIR", "PYTERMWM_STATE_DIR")})
        try:
            sock = P.connect(self.session)
            sock.sendall(P.pack_json(P.HELLO, {"cols": 100, "rows": 30, "version": "0.0.1"}))
            end = time.time() + 5
            frame = ""
            while time.time() < end and "restart it to update" not in frame.replace("\n", ""):
                time.sleep(0.2)
                frame = self.cli("-s", self.session, "frame").stdout
            sock.close()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertIn("the client is 0.0.1", frame.replace("\n", ""))

    def test_mcp_survives_garbage_and_missing_session(self):
        r = self.cli("-s", self.session, "mcp", input="not json\n" + json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}) + "\n")
        out = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual(out[0]["error"]["code"], -32700)
        self.assertEqual(out[1]["id"], 5)

    def test_mcp_without_a_running_session_says_so(self):
        # what an MCP client (LM Studio, ...) shows when the session it points at was never started
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "run_command", "arguments": {"line": "layout grid"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "pytermwm://state"}}]
        r = self.cli("-s", "no-such-session", "mcp", input="".join(json.dumps(m) + "\n" for m in msgs))
        out = {m["id"]: m for m in (json.loads(l) for l in r.stdout.splitlines())}
        self.assertIn("protocolVersion", out[1]["result"])                 # the handshake still works
        self.assertTrue(out[2]["result"]["isError"])
        self.assertIn("no pytermwm session 'no-such-session' is running", out[2]["result"]["content"][0]["text"])
        self.assertIn("pytermwm -s no-such-session start", out[3]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
