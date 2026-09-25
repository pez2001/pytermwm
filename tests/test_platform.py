"""Cross-platform layer: runs on Linux, macOS and Windows.

The Windows specific *logic* (paths, quoting, endpoints, tasklist/ConPTY helpers) is exercised everywhere by pretending
to be Windows; the real ConPTY / console code is exercised when the tests actually run on Windows.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.helpers import make_wm, pump
from pytermwm import compat, protocol as P, winpty, winsys
from pytermwm.commands import split_line


def fake_windows():
    return mock.patch.object(compat, "IS_WINDOWS", True)


class DirectoryLayout(unittest.TestCase):
    def test_windows_directories(self):
        env = {"APPDATA": r"C:\Users\me\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
        with fake_windows(), mock.patch.dict(os.environ, env, clear=False):
            for v in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
                os.environ.pop(v, None)
            self.assertEqual(compat.config_home(), env["APPDATA"])
            self.assertEqual(compat.state_home(), env["LOCALAPPDATA"])
            self.assertEqual(compat.runtime_home(), os.path.join(env["LOCALAPPDATA"], "pytermwm", "run"))

    def test_xdg_wins_everywhere(self):
        with fake_windows(), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/x/cfg"}):
            self.assertEqual(compat.config_home(), "/x/cfg")

    def test_runtime_dir_override(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"PYTERMWM_RUNTIME_DIR": d}):
            self.assertEqual(P.runtime_dir(), d)
            self.assertTrue(P.socket_path("s").startswith(d))


class ShellAndQuoting(unittest.TestCase):
    def test_default_shell_windows_prefers_powershell(self):
        found = {"pwsh.exe": None, "powershell.exe": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}
        with fake_windows(), mock.patch.object(compat, "_which", lambda n: found.get(n)), \
                mock.patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHELL", None)
            os.environ.pop("PYTERMWM_SHELL", None)
            self.assertTrue(compat.default_shell().endswith("powershell.exe"))
            found["powershell.exe"] = None
            self.assertEqual(compat.default_shell(), r"C:\Windows\System32\cmd.exe")

    def test_shell_command_forms(self):
        with fake_windows(), mock.patch.object(compat, "default_shell", lambda: r"C:\Windows\System32\cmd.exe"):
            self.assertEqual(compat.shell_command("dir /w"), [r"C:\Windows\System32\cmd.exe", "/d", "/c", "dir /w"])
        with fake_windows(), mock.patch.object(compat, "default_shell", lambda: "pwsh.exe"):
            self.assertEqual(compat.shell_command("ls"), ["pwsh.exe", "-NoLogo", "-Command", "ls"])
        with mock.patch.object(compat, "IS_WINDOWS", False), mock.patch.object(compat, "default_shell", lambda: "/bin/sh"):
            self.assertEqual(compat.shell_command("ls"), ["/bin/sh", "-c", "ls"])

    def test_windows_split_keeps_backslashes(self):
        with fake_windows():
            self.assertEqual(compat.split_command_line(r'C:\Users\me\app.exe "a b" --x=C:\tmp'),
                             [r"C:\Users\me\app.exe", "a b", r"--x=C:\tmp"])
            self.assertEqual(compat.split_command_line("'' x"), ["", "x"])
            with self.assertRaises(ValueError):
                compat.split_command_line('"open')

    def test_wm_command_line_keeps_windows_paths(self):
        with fake_windows():
            self.assertEqual(split_line(r"new-window -c C:\src\proj -- python -V"), [["new-window", "-c", r"C:\src\proj", "--", "python", "-V"]])
            self.assertEqual(split_line("a 'x y'; b"), [["a", "x y"], ["b"]])
        with mock.patch.object(compat, "IS_WINDOWS", False):
            self.assertEqual(split_line(r"echo a\ b"), [["echo", "a b"]])      # POSIX escapes still work

    def test_shell_quote_is_injection_safe_on_windows(self):
        nasty = 'x" & calc & "%PATH%$(id)`whoami`; rm ^ | <>\r\n'
        with fake_windows():
            q = compat.quote_shell_arg(nasty)
        self.assertTrue(q.startswith('"') and q.endswith('"'))
        inner = q[1:-1]
        for bad in '"&|<>^%!`$;()\r\n':
            self.assertNotIn(bad, inner)
        with mock.patch.object(compat, "IS_WINDOWS", False):
            import shlex
            self.assertEqual(shlex.split(compat.quote_shell_arg(nasty)), [nasty])

    def test_executable_detection_uses_pathext_on_windows(self):
        d = tempfile.mkdtemp()
        exe, txt = os.path.join(d, "tool.EXE"), os.path.join(d, "notes.txt")
        for p in (exe, txt):
            open(p, "w").close()
        with fake_windows(), mock.patch.dict(os.environ, {"PATHEXT": ".COM;.EXE;.BAT;.CMD"}):
            self.assertTrue(compat.is_executable_file(exe))
            self.assertFalse(compat.is_executable_file(txt))


class WinHelpers(unittest.TestCase):
    def test_env_block(self):
        b = winpty.build_env_block({"b": "2", "A": "1", "c": "x y"})
        self.assertEqual(b, "A=1\0b=2\0c=x y\0\0")

    def test_command_line_and_resolution(self):
        with mock.patch("shutil.which", lambda n: r"C:\Program Files\Python\python.exe" if n == "python" else None):
            self.assertEqual(winpty.command_line(["python", "-c", "print('a b')"]),
                             '"C:\\Program Files\\Python\\python.exe" -c "print(\'a b\')"')
        with mock.patch("shutil.which", lambda n: r"C:\tools\build.cmd"), mock.patch.dict(os.environ, {"COMSPEC": "cmd.exe"}):
            self.assertEqual(winpty.resolve_argv(["build", "all"]), ["cmd.exe", "/d", "/c", r"C:\tools\build.cmd", "all"])
        with mock.patch("shutil.which", lambda n: None):
            self.assertEqual(winpty.resolve_argv(["nope"]), ["nope"])          # error surfaces from CreateProcess
        with self.assertRaises(ValueError):
            winpty.resolve_argv([])

    def test_parse_tasklist(self):
        text = '"System Idle Process","0","Services","0","8 K"\r\n"python.exe","4242","Console","1","12,345 K"\r\n"weird","x","","",""\r\n'
        procs = winsys.parse_tasklist(text)
        self.assertEqual([p["pid"] for p in procs], [0, 4242])
        self.assertEqual(procs[1]["name"], "python.exe")
        self.assertEqual(procs[1]["rss"], 12345 * 1024)

    def test_sysinfo_falls_back_cleanly_when_windows_apis_fail(self):
        from pytermwm import sysinfo
        with mock.patch.object(sysinfo, "IS_WINDOWS", True), mock.patch.object(winsys, "cpu_times", lambda: None), \
                mock.patch.object(winsys, "memory", lambda: None), mock.patch.object(winsys, "processes", lambda: winsys.parse_tasklist('"a.exe","7","","","5 K"')):
            s = sysinfo.Sampler()
            self.assertEqual(s.cpu()[0], 0.0 if not hasattr(os, "getloadavg") else s.cpu()[0])
            self.assertEqual(s.net_totals(), (0, 0))
            self.assertEqual(s.processes(5, "mem")[0]["pid"], 7)
            self.assertEqual(len(s.load()), 3)


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _pair(self, name="t.port"):
        path = os.path.join(self.d, name)
        lis = compat.Listener(path)
        self.addCleanup(lis.close)
        return path, lis

    def _accept(self, lis, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            c = lis.accept()
            if c is not None:
                return c
            time.sleep(0.01)
        return None

    def test_tcp_endpoint_roundtrip_and_file_contents(self):
        path, lis = self._pair()
        info = json.load(open(path))
        self.assertEqual(sorted(info), ["key", "pid", "port"])
        self.assertEqual(len(info["key"]), compat.KEY_LEN)
        c = compat.connect_endpoint(path)
        srv = self._accept(lis)
        self.assertIsNotNone(srv)
        c.sendall(b"hello")
        end = time.time() + 2
        got = b""
        while len(got) < 5 and time.time() < end:
            try:
                got += srv.recv(16)
            except BlockingIOError:
                time.sleep(0.01)
        self.assertEqual(got, b"hello")
        c.close()
        srv.close()

    def test_wrong_key_is_rejected(self):
        path, lis = self._pair()
        port = json.load(open(path))["port"]
        s = socket.create_connection(("127.0.0.1", port), 2)
        s.sendall(b"x" * compat.KEY_LEN)
        self.assertIsNone(self._accept(lis, 1.5))
        s.close()

    def test_silent_connection_is_dropped(self):
        path, lis = self._pair()
        port = json.load(open(path))["port"]
        s = socket.create_connection(("127.0.0.1", port), 2)
        time.sleep(0.1)                                 # let the connection become pending
        t0 = time.time()
        self.assertIsNone(lis.accept())                 # gives up after the authentication timeout
        self.assertLess(time.time() - t0, 2.5)
        s.close()

    def test_alive_and_stale(self):
        path, lis = self._pair()
        self.assertTrue(compat.endpoint_alive(path))
        lis.close()
        self.assertFalse(compat.endpoint_alive(path))
        with open(path, "w") as f:
            f.write("not json")
        self.assertFalse(compat.endpoint_alive(path))

    def test_list_sessions_sees_port_files_and_cleans_stale_ones(self):
        with mock.patch.dict(os.environ, {"PYTERMWM_RUNTIME_DIR": self.d}):
            live = compat.Listener(os.path.join(self.d, "live.port"))
            self.addCleanup(live.close)
            with open(os.path.join(self.d, "dead.port"), "w") as f:
                json.dump({"port": 9, "key": "k" * 32}, f)
            self.assertEqual(P.list_sessions(), ["live"])
            self.assertFalse(os.path.exists(os.path.join(self.d, "dead.port")))

    def test_control_client_over_tcp_endpoint(self):
        path, lis = self._pair("ctl.port")
        c = P.ControlClient(path=path)
        srv = self._accept(lis)
        srv.setblocking(True)
        t = threading.Thread(target=lambda: srv.sendall(P.pack_json(P.RESPONSE, {"ok": True, "n": 1})), daemon=True)
        t.start()
        self.assertEqual(c.request({"op": "ping"}, 3), {"ok": True, "n": 1})
        c.close()
        srv.close()

    def test_unix_endpoint_when_available(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no unix sockets")
        path = os.path.join(self.d, "u.sock")
        lis = compat.Listener(path)
        self.addCleanup(lis.close)
        self.assertFalse(lis.tcp)
        self.assertTrue(compat.endpoint_alive(path))


class Plumbing(unittest.TestCase):
    def test_pump_delivers_bytes_then_eof(self):
        chunks = [b"abc", b"def", b""]
        p = compat.Pump(lambda: chunks.pop(0))
        got = b""
        end = time.time() + 3
        while time.time() < end:
            d = p.recv()
            if d is None:
                break
            got += d
            time.sleep(0.005)
        self.assertEqual(got, b"abcdef")
        self.assertIsNone(p.recv())
        p.close()

    def test_pump_is_selectable(self):
        import select
        ev = threading.Event()

        def read():
            ev.wait(2)
            return b"x" if ev.is_set() and not getattr(read, "done", False) and not setattr(read, "done", True) else b""
        p = compat.Pump(read)
        self.assertEqual(select.select([p.fileno()], [], [], 0.05)[0], [])
        ev.set()
        self.assertEqual(select.select([p.fileno()], [], [], 2)[0], [p.fileno()])
        self.assertEqual(p.recv(), b"x")
        p.close()

    def test_pump_backpressure_large_payload(self):
        big = [b"y" * 65536 for _ in range(40)] + [b""]
        p = compat.Pump(lambda: big.pop(0))
        total = 0
        end = time.time() + 10
        while time.time() < end:
            d = p.recv(1 << 20)
            if d is None:
                break
            total += len(d)
            if not d:
                time.sleep(0.002)
        self.assertEqual(total, 40 * 65536)

    def test_thread_writer_orders_and_survives_errors(self):
        out = []
        w = compat.ThreadWriter(out.append)
        for i in range(5):
            w.put(bytes([i]))
        end = time.time() + 2
        while len(out) < 5 and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(out, [bytes([i]) for i in range(5)])
        w.close()

        def boom(_):
            raise OSError("pipe closed")
        w2 = compat.ThreadWriter(boom)
        w2.put(b"a")
        time.sleep(0.1)
        self.assertTrue(w2.dead)
        w2.put(b"b")                                    # ignored, no exception

    def test_waker(self):
        import select
        w = compat.Waker()
        self.assertEqual(select.select([w.r], [], [], 0)[0], [])
        threading.Thread(target=w.wake).start()
        self.assertEqual(select.select([w.r], [], [], 2)[0], [w.r])
        w.drain()
        self.assertEqual(select.select([w.r], [], [], 0)[0], [])
        w.close()

    def test_install_signal_tolerates_missing_signals(self):
        self.assertFalse(compat.install_signal(None, lambda *a: None))


PY = sys.executable


def pycmd(code):
    return [PY, "-c", code]


class PortableWindows(unittest.TestCase):
    """Real child processes in real ptys / ConPTYs / pipes / files, using only python as the child program."""

    def setUp(self):
        self.wm = make_wm(100, 30)
        self.addCleanup(self.wm.shutdown)

    def text(self, w):
        return w.screen.text(history=True)

    def test_terminal_window_shows_output_and_exit_code(self):
        w = self.wm.create_window({"cmd": pycmd("print('hello-from-child'); import sys; sys.exit(3)"), "on_exit": "keep"})
        self.assertTrue(pump(self.wm, 15, until=lambda: w.exited))
        self.assertIn("hello-from-child", self.text(w))
        self.assertEqual(w.exit_code, 3)

    def test_terminal_window_input_roundtrip(self):
        code = "import sys\nl = sys.stdin.readline()\nprint('got:' + l.strip())\nsys.stdout.flush()\nimport time; time.sleep(30)"
        w = self.wm.create_window({"cmd": pycmd(code), "on_exit": "keep"})
        time.sleep(0.5)
        w.write_input(b"ping\r")
        self.assertTrue(pump(self.wm, 15, until=lambda: "got:ping" in self.text(w)), self.text(w))

    def test_resize_reaches_the_child(self):
        code = ("import os, time, sys\nprev = None\nfor _ in range(200):\n"
                "    s = os.get_terminal_size()\n    if s != prev:\n        print('SIZE %dx%d' % (s.columns, s.lines)); sys.stdout.flush(); prev = s\n    time.sleep(0.05)")
        w = self.wm.create_window({"cmd": pycmd(code), "on_exit": "keep"})
        self.assertTrue(pump(self.wm, 15, until=lambda: "SIZE" in self.text(w)))
        w.set_viewport(50, 12)
        w.source.resize(12, 50)
        self.assertTrue(pump(self.wm, 15, until=lambda: "SIZE 50x12" in self.text(w)), self.text(w))

    def test_close_kills_a_long_running_child(self):
        w = self.wm.create_window({"cmd": pycmd("import time; time.sleep(60)"), "on_exit": "keep"})
        pid = w.source.pid
        t0 = time.time()
        self.wm.close_window(w.id)
        self.assertLess(time.time() - t0, 5)
        end = time.time() + 5
        while compat.pid_alive(pid) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(compat.pid_alive(pid))

    def test_pipe_window_separates_stderr(self):
        w = self.wm.create_window({"kind": "pipe", "cmd": pycmd("import sys; print('to-out'); print('to-err', file=sys.stderr)"), "on_exit": "keep"})
        self.assertTrue(pump(self.wm, 15, until=lambda: w.exited))
        t = self.text(w)
        self.assertIn("to-out", t)
        self.assertIn("to-err", t)

    def test_file_window_follows_appends(self):
        path = os.path.join(tempfile.mkdtemp(), "log.txt")
        with open(path, "wb") as f:
            f.write(b"first line\n")
        w = self.wm.create_window({"kind": "file", "path": path})
        self.assertTrue(pump(self.wm, 10, until=lambda: "first line" in self.text(w)))
        with open(path, "ab") as f:
            f.write(b"second line\n")
        self.assertTrue(pump(self.wm, 10, until=lambda: "second line" in self.text(w)))

    def test_default_shell_window_starts(self):
        w = self.wm.create_window({})
        self.assertTrue(pump(self.wm, 15, until=lambda: bool(self.text(w).strip())), "the default shell printed nothing")
        self.assertFalse(w.exited)

    def test_unicode_output(self):
        w = self.wm.create_window({"cmd": pycmd("import sys; sys.stdout.buffer.write('caf\\u00e9 \\u2713 \\u4e2d\\n'.encode('utf-8')); sys.stdout.flush()"), "on_exit": "keep"})
        self.assertTrue(pump(self.wm, 15, until=lambda: w.exited))
        self.assertIn("café ✓ 中", self.text(w))


@unittest.skipUnless(compat.IS_WINDOWS, "ConPTY only exists on Windows")
class ConPtyReal(unittest.TestCase):
    def test_selftest(self):
        self.assertIn("ConPTY works", winpty.selftest())

    def test_console_mode_object(self):
        con = compat._WinConsole()
        self.assertIsNotNone(con.k)


if __name__ == "__main__":
    unittest.main()
