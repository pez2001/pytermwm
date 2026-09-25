import json
import os
import sys
import tempfile
import unittest

from tests.helpers import *
from pytermwm import recording
from pytermwm.commands import CommandError

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def write_cast(path, header, events):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for e in events:
            f.write(json.dumps(e) + "\n")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["PYTERMWM_STATE_DIR"] = self.tmp
        self.wm = make_wm(100, 30)
        self.w = self.wm.create_window({"cmd": SLEEPER, "name": "rec"})

    def tearDown(self):
        self.wm.shutdown()

    def path(self, n="a.cast"):
        return os.path.join(self.tmp, n)

    def test_records_output_as_asciicast_v2(self):
        out = self.wm.run_command_line("record -t rec %s" % self.path(), source="t", raise_errors=True)
        self.assertIn("recording", str(out))
        self.w.feed_bytes("héllo wörld\r\n".encode("utf-8")[:3])      # split inside a multi-byte character
        self.w.feed_bytes("héllo wörld\r\n".encode("utf-8")[3:])
        self.wm.run_command_line("record-stop -t rec", source="t", raise_errors=True)
        header, events = recording.read_cast(self.path())
        self.assertEqual(header["version"], 2)
        self.assertEqual((header["width"], header["height"]), (self.w.screen.cols, self.w.screen.rows))
        text = "".join(d for t, k, d in events if k == "o")
        self.assertIn("héllo wörld", text)
        self.assertNotIn("�", text)
        self.assertEqual([t for t, _, _ in events], sorted(t for t, _, _ in events))

    def test_start_snapshots_existing_screen(self):
        self.w.feed_bytes(b"already here\r\n")
        self.wm.run_command_line("record -t rec %s" % self.path(), source="t", raise_errors=True)
        self.wm.run_command_line("record-stop -t rec", source="t", raise_errors=True)
        _, events = recording.read_cast(self.path())
        self.assertIn("already here", "".join(d for _, k, d in events))

    def test_input_only_when_asked(self):
        self.wm.run_command_line("record -t rec %s" % self.path("n.cast"), source="t", raise_errors=True)
        self.w.write_input(b"secret")
        self.wm.run_command_line("record-stop -t rec", source="t", raise_errors=True)
        self.assertFalse(any(k == "i" for _, k, _ in recording.read_cast(self.path("n.cast"))[1]))
        self.wm.run_command_line("record -t rec -i %s" % self.path("i.cast"), source="t", raise_errors=True)
        self.w.write_input(b"typed")
        self.wm.run_command_line("record-stop -t rec", source="t", raise_errors=True)
        ev = recording.read_cast(self.path("i.cast"))[1]
        self.assertEqual([d for _, k, d in ev if k == "i"], ["typed"])

    def test_resize_is_recorded(self):
        rec = recording.start(self.wm, self.w, self.path("r.cast"))
        self.w.set_viewport(50, 12)
        recording.stop(self.w)
        ev = recording.read_cast(self.path("r.cast"))[1]
        self.assertIn("50x12", [d for _, k, d in ev if k == "r"])

    def test_errors(self):
        with self.assertRaises(CommandError):
            self.wm.run_command_line("record-stop -t rec", source="t", raise_errors=True)
        self.wm.run_command_line("record -t rec %s" % self.path(), source="t", raise_errors=True)
        with self.assertRaises(CommandError):
            self.wm.run_command_line("record -t rec %s" % self.path("b.cast"), source="t", raise_errors=True)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_recordings_are_private(self):
        rec = recording.start(self.wm, self.w, self.path("priv.cast"), capture_input=True)
        recording.stop(self.w)
        self.assertEqual(os.stat(self.path("priv.cast")).st_mode & 0o077, 0)

    def test_write_errors_stop_the_recording_quietly(self):
        rec = recording.start(self.wm, self.w, self.path("e.cast"))
        rec.f.close()                                      # simulate the file going away
        self.w.feed_bytes(b"still fine\r\n")             # must not raise
        self.assertTrue(rec.closed)
        self.assertIn("still fine", self.w.text())
        info = recording.stop(self.w)
        self.assertIsNotNone(info["error"])

    def test_does_not_overwrite_without_force(self):
        with open(self.path("keep.txt"), "w", encoding="utf-8") as f:
            f.write("precious")
        with self.assertRaises(CommandError):
            self.wm.run_command_line("record -t rec %s" % self.path("keep.txt"), source="t", raise_errors=True)
        with open(self.path("keep.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "precious")
        self.wm.run_command_line("record -t rec -f %s" % self.path("keep.txt"), source="t", raise_errors=True)
        recording.stop(self.w)
        recording.read_cast(self.path("keep.txt"))

    def test_close_window_finishes_file(self):
        self.wm.run_command_line("record -t rec %s" % self.path(), source="t", raise_errors=True)
        rec = self.w.recorder
        self.w.feed_bytes(b"x")
        self.wm.close_window(self.w.id)
        self.assertTrue(rec.closed)
        recording.read_cast(self.path())                              # a valid file

    def test_marker_and_describe(self):
        self.wm.run_command_line("record -t rec %s" % self.path(), source="t", raise_errors=True)
        self.wm.run_command_line("record-mark -t rec build-ok", source="t", raise_errors=True)
        self.assertEqual(self.w.describe()["recording"]["path"], self.path())
        self.wm.run_command_line("record-stop -t rec", source="t", raise_errors=True)
        with open(self.path(), encoding="utf-8") as fh:
            self.assertTrue(any('"m"' in line and "build-ok" in line for line in fh))
        self.assertNotIn("recording", self.w.describe())

    def test_default_path_is_in_state_dir(self):
        rec = recording.start(self.wm, self.w)
        try:
            self.assertTrue(rec.path.startswith(self.tmp))
            self.assertTrue(rec.path.endswith(".cast"))
        finally:
            recording.stop(self.w)

    def test_round_trip(self):
        recording.start(self.wm, self.w, self.path())
        self.w.feed_bytes(b"line one\r\n\x1b[31mred\x1b[0m two\r\n")
        recording.stop(self.w)
        r = self.wm.create_window({"kind": "cast", "path": self.path(), "name": "play", "idle": 0})
        r.on_tick(1000.0)
        r.on_tick(1001.0)
        self.assertEqual(r.text().split("\n")[:2], ["line one", "red two"])


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wm = make_wm(100, 30)
        self.path = os.path.join(self.tmp, "p.cast")
        write_cast(self.path, {"version": 2, "width": 40, "height": 10, "title": "demo"},
                   [[0.1, "o", "one\r\n"], [1.0, "o", "two\r\n"], [2.0, "m", "mark"], [3.0, "o", "three\r\n"],
                    [3.5, "r", "30x8"]])
        self.r = self.wm.create_window({"kind": "cast", "path": self.path, "name": "play"})

    def tearDown(self):
        self.wm.shutdown()

    def play(self, seconds, start=100.0, step=0.1):
        t = start
        self.r.on_tick(t)
        end = t + seconds
        while t < end:
            t += step
            self.r.on_tick(t)

    def test_timing(self):
        self.play(0.5)
        self.assertIn("one", self.r.text())
        self.assertNotIn("two", self.r.text())
        self.play(1.0, start=200.0)
        self.assertIn("two", self.r.text())

    def test_finishes_and_resizes(self):
        self.play(4.5)
        t = self.r.text()
        self.assertIn("three", t)
        self.assertEqual(self.r.vsize, (30, 8))
        self.assertIn("done", self.r.title)

    def test_pause_speed_seek_restart(self):
        self.r.handle_key("Space", b" ")
        self.play(3)
        self.assertNotIn("one", self.r.text())
        self.r.handle_key("Space", b" ")
        self.r.handle_key("+", b"+")
        self.assertEqual(self.r.speed, 2.0)
        self.r.handle_key("Right", b"")
        self.assertIn("three", self.r.text())
        self.r.handle_key("Left", b"")                 # 3.5 - 5 s: back to the very start
        self.assertEqual(self.r.pos, 0)
        self.assertNotIn("three", self.r.text())
        self.r.seek(1.5)
        self.assertIn("two", self.r.text())
        self.assertNotIn("three", self.r.text())
        self.r.handle_key("r", b"r")
        self.assertNotIn("one", self.r.text())
        self.assertEqual(self.r.pos, 0)

    def test_idle_compression(self):
        write_cast(self.path, {"version": 2, "width": 20, "height": 5}, [[0.1, "o", "a"], [500.0, "o", "b"]])
        w = self.wm.create_window({"kind": "cast", "path": self.path, "idle": 1.0})
        self.assertAlmostEqual(w.duration, 1.1, places=3)
        write_cast(self.path, {"version": 2, "width": 20, "height": 5, "idle_time_limit": 0.5}, [[0.1, "o", "a"], [500.0, "o", "b"]])
        w = self.wm.create_window({"kind": "cast", "path": self.path, "idle": None})
        self.assertAlmostEqual(w.duration, 0.6, places=3)

    def test_loop(self):
        r = self.wm.create_window({"kind": "cast", "path": self.path, "loop": True, "idle": 0.5})
        t = 100.0
        r.on_tick(t)
        for _ in range(200):
            t += 0.1
            r.on_tick(t)
            if r.pos >= len(r.events):
                break
        self.assertEqual(r.pos, len(r.events))
        self.assertIn("three", r.text())
        r.on_tick(t + 0.1)                         # at the end and looping: starts over
        self.assertLess(r.pos, len(r.events))
        self.assertNotIn("three", r.text())

    def test_bad_files(self):
        for content, frag in (("", "empty"), ("not json\n", "header"), ('{"version": 1}\n', "v2"),
                              ('{"version": 2, "width": 1, "height": 1}\nbroken\n', "event")):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(content)
            with self.assertRaises(CommandError) as cm:
                self.wm.create_window({"kind": "cast", "path": self.path})
            self.assertIn(frag, str(cm.exception))
        with self.assertRaises(CommandError):
            self.wm.create_window({"kind": "cast", "path": os.path.join(self.tmp, "nope.cast")})
        with self.assertRaises(CommandError):
            self.wm.create_window({"kind": "cast"})

    def test_hostile_casts_are_rejected_or_clamped(self):
        for header, events, frag in (
                ({"version": 2, "width": "wide", "height": 5}, [], "width"),
                ({"version": 2, "width": [1], "height": 5}, [], "width"),
                ({"version": 2, "width": 5, "height": 5, "idle_time_limit": "soon"}, [], "idle"),
                ({"version": 2, "width": 5, "height": 5}, [[float("inf"), "o", "x"]], "time"),
                ({"version": 2, "width": 5, "height": 5}, [[float("nan"), "o", "x"]], "time")):
            write_cast(self.path, header, events)
            with self.assertRaises(CommandError) as cm:
                self.wm.create_window({"kind": "cast", "path": self.path})
            self.assertIn(frag, str(cm.exception))
        write_cast(self.path, {"version": 2, "width": 20000, "height": 20000}, [[0.1, "r", "99999x99999"], [0.2, "o", "x"]])
        w = self.wm.create_window({"kind": "cast", "path": self.path})
        self.assertEqual(w.vsize, (recording.MAX_COLS, recording.MAX_ROWS))
        w.on_tick(1.0)
        w.on_tick(3.0)
        self.assertLessEqual(w.vsize[0], recording.MAX_COLS)
        self.assertLessEqual(w.vsize[1], recording.MAX_ROWS)

    def test_replay_does_not_trigger_rules(self):
        from pytermwm import config as C
        C.apply_config(self.wm, {"rules": [{"name": "watch", "when": {"output_matches": "ERROR"}, "do": ["notify hit"]}]})
        write_cast(self.path, {"version": 2, "width": 20, "height": 5}, [[0.1, "o", "ERROR here\r\n"]])
        w = self.wm.create_window({"kind": "cast", "path": self.path, "idle": 0})
        w.on_tick(10.0)
        w.on_tick(11.0)
        self.assertIn("ERROR", w.text())
        pump(self.wm, 0.3)
        self.assertEqual(self.wm.rules.by_name["watch"].fired, 0)
        self.assertFalse(w.activity)

    def test_replay_command(self):
        out = self.wm.run_command_line('replay "%s" --speed 4 --loop -t demo' % self.path, source="t", raise_errors=True)
        self.assertIn("window", str(out))
        w = list(self.wm.windows.values())[-1]
        self.assertEqual((w.kind, w.speed, w.loop), ("cast", 4.0, True))

    def test_keys_go_through_wm(self):
        self.wm.focus_window(self.r.id) if hasattr(self.wm, "focus_window") else None
        self.assertTrue(self.r.handle_key("q", b"q"))
        self.assertNotIn(self.r.id, self.wm.windows)


if __name__ == "__main__":
    unittest.main()


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["PYTERMWM_STATE_DIR"] = self.tmp
        self.wm = make_wm(60, 16)
        self.wm.create_window({"kind": "text", "title": "hello", "text": "screenshot <me> & █ block"})

    def tearDown(self):
        self.wm.shutdown()

    def run_(self, line):
        return self.wm.run_command_line(line, source="t", raise_errors=True)

    def test_svg_ansi_and_text(self):
        import xml.dom.minidom
        svg = self.run_("screenshot %s" % os.path.join(self.tmp, "s.svg"))
        with open(svg, encoding="utf-8") as f:
            doc = xml.dom.minidom.parseString(f.read())              # well-formed, text escaped
        texts = " ".join(t.firstChild.data for t in doc.getElementsByTagName("text") if t.firstChild)
        self.assertIn("screenshot <me> &", texts)
        self.assertIn("1:main", texts)
        self.assertTrue(doc.getElementsByTagName("rect"))
        txt = self.run_("screenshot %s" % os.path.join(self.tmp, "s.txt"))
        with open(txt, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 16)
        self.assertIn("screenshot <me>", lines[1])
        ans = self.run_("screenshot %s" % os.path.join(self.tmp, "s.ans"))
        with open(ans, encoding="utf-8", newline="") as f:
            scr = Screen(20, 80, 0)                                   # `cat file.ans` in a (wider) terminal
            scr.feed(f.read())
        self.assertIn("screenshot <me>", Screen.line_text(scr.lines[1]))

    def test_refuses_overwrite_and_unknown_format(self):
        p = os.path.join(self.tmp, "a.svg")
        self.run_("screenshot " + p)
        with self.assertRaises(CommandError):
            self.run_("screenshot " + p)
        self.run_("screenshot -f " + p)
        with self.assertRaises(CommandError):
            self.run_("screenshot %s" % os.path.join(self.tmp, "a.png"))

    def test_default_path_is_in_the_state_dir(self):
        p = self.run_("screenshot")
        self.assertTrue(p.startswith(self.tmp) and p.endswith(".svg") and os.path.exists(p), p)

    def test_screen_recording_replays_to_the_screen(self):
        from pytermwm import screenshot
        p = os.path.join(self.tmp, "screen.cast")
        self.run_("record-screen " + p)
        rec = self.wm.screen_recorder
        rec.frame(screenshot.compose(self.wm), 0.0)
        self.run_("theme dos")
        rec.frame(screenshot.compose(self.wm), 1.0)
        self.wm.resize(50, 14)
        rec.frame(screenshot.compose(self.wm), 2.0)
        self.assertIn("saved", self.run_("record-screen-stop"))
        self.assertIsNone(self.wm.screen_recorder)
        with self.assertRaises(CommandError):
            self.run_("record-screen-stop")
        header, events = recording.read_cast(p)
        self.assertEqual((header["version"], header["width"], header["height"]), (2, 60, 16))
        self.assertIn((2.0, "r", "50x14"), [(t, k, d) for t, k, d in events])
        scr = Screen(14, 50, 0)
        for _t, kind, data in events:
            if kind == "r":
                c, r = data.split("x")
                scr = Screen(int(r), int(c), 0)
            elif kind == "o":
                scr.feed(data)
        text = "\n".join(Screen.line_text(l) for l in scr.lines)
        self.assertIn("screenshot <me>", text)
        self.assertIn("C:\\>", text)                                  # the dos theme's status line


class ThemeTests(unittest.TestCase):
    def test_new_themes_render(self):
        wm = make_wm(60, 16)
        try:
            wm.create_window({"kind": "text", "title": "t", "text": "x"})
            for name, needle in (("nes", "PLAYER 1"), ("matrix", "wake up, neo"), ("dos", "C:\\>")):
                wm.run_command_line("theme " + name, source="t", raise_errors=True)
                self.assertEqual(wm.theme.name, name)
                scr, _frame = screen_of_frame(wm)
                self.assertIn(needle, Screen.line_text(scr.lines[-1]))
        finally:
            wm.shutdown()
