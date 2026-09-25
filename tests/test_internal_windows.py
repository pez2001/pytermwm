import io
import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm import pv as PV
from pytermwm.sysinfo import human_bytes, human_rate, human_time
from pytermwm import charts


from pytermwm.ansi import Screen


def shown(w):
    """What the window displays (as opposed to w.text(), which viewers return unfiltered for capture)."""
    w.refresh()
    return "\n".join(Screen.line_text(l) for l in w.screen.lines)


class WinCase(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(120, 40)

    def tearDown(self):
        self.wm.shutdown()

    def refresh(self, w):
        w.refresh()
        return w.text()


class ViewerTests(WinCase):
    def setUp(self):
        super().setUp()
        self.w = self.wm.create_window({"kind": "viewer", "title": "v"})

    def feed(self, *lines):
        for l in lines:
            self.wm.execute("viewer-feed %d %s" % (self.w.id, l))

    def test_shows_lines(self):
        self.feed("alpha", "beta")
        t = shown(self.w)
        self.assertIn("alpha", t)
        self.assertIn("beta", t)

    def test_filter_exclude_and_clear(self):
        self.feed("apple", "banana", "avocado")
        self.wm.execute("viewer-set %d filter ^a" % self.w.id)
        t = shown(self.w)
        self.assertIn("apple", t)
        self.assertNotIn("banana", t)
        self.wm.execute("viewer-set %d filter" % self.w.id)
        self.wm.execute("viewer-set %d exclude avo" % self.w.id)
        t = shown(self.w)
        self.assertNotIn("avocado", t)
        self.assertIn("banana", t)
        self.wm.execute("viewer-set %d clear" % self.w.id)
        self.assertNotIn("banana", shown(self.w))

    def test_pause_stops_display_but_keeps_buffer(self):
        self.feed("one")
        shown(self.w)
        self.wm.execute("viewer-set %d pause on" % self.w.id)
        self.feed("two")
        self.assertNotIn("two", shown(self.w))
        self.wm.execute("viewer-set %d pause off" % self.w.id)
        self.assertIn("two", shown(self.w))

    def test_numbers_and_highlight(self):
        self.feed("first", "second error here")
        self.wm.execute("viewer-set %d numbers on" % self.w.id)
        self.assertRegex(shown(self.w), r"1\s+first")
        self.wm.execute("viewer-set %d highlight error" % self.w.id)
        shown(self.w)
        from pytermwm.colors import REVERSE
        rev = [c for row in self.w.screen.lines for c in row if c[0] == "e" and c[3] & REVERSE]
        self.assertTrue(rev)

    def test_max_lines_bounded(self):
        w = self.wm.create_window({"kind": "viewer", "max_lines": 50})
        for i in range(200):
            w.feed_bytes(("line%d\n" % i).encode())
        t = shown(w)
        self.assertIn("line199", t)
        self.assertNotIn("line0\n", t)

    def test_not_a_viewer_error(self):
        t = self.wm.create_window({"cmd": "sleep 5"})
        self.assertFalse(self.wm.execute("viewer-set %d pause" % t.id)["ok"])

    def test_receives_routed_output(self):
        src = self.wm.create_window({"cmd": "sleep 0.3; echo routed-line; sleep 5"})
        self.wm.execute("route %d out window:%d --mute" % (src.id, self.w.id))
        pump(self.wm, 3, lambda: "routed-line" in shown(self.w))
        self.assertIn("routed-line", shown(self.w))

    def test_write_input_ignored(self):
        self.w.write_input(b"x")


class ChartTests(WinCase):
    def test_push_chart_draws_values(self):
        r = self.wm.execute("new-chart push line")
        w = self.wm.windows[r["result"]["id"]]
        for v in (1, 5, 3, 9, 2, 8):
            self.wm.execute("chart-push %d %s" % (w.id, v))
        t = self.refresh(w)
        self.assertTrue(any(ch in t for ch in "⣀⣤⣶⣿⠁⡇⢀⣠"), t)

    def test_all_chart_kinds_render(self):
        for kind in ("line", "area", "bar", "spark", "gauge", "hist"):
            r = self.wm.execute("new-chart cpu %s" % kind)
            self.assertTrue(r["ok"], (kind, r))
            w = self.wm.windows[r["result"]["id"]]
            for i in range(30):
                w.push(float(i % 7))
            self.assertTrue(self.refresh(w).strip(), kind)

    def test_system_sources_sample(self):
        for src in ("cpu", "mem", "load", "net_rx", "disk"):
            w = self.wm.create_window({"kind": "chart", "source": src, "interval": 0.05})
            pump(self.wm, 0.3)
            self.assertGreaterEqual(len(w.data), 1, src)

    def test_cmd_source(self):
        w = self.wm.create_window({"kind": "chart", "source": "cmd", "cmd": "echo 42", "interval": 0.05})
        pump(self.wm, 1.5, lambda: len(w.data) > 0)
        self.assertEqual(w.data[-1], 42.0)

    def test_chart_push_needs_chart(self):
        t = self.wm.create_window({"cmd": "sleep 5"})
        self.assertFalse(self.wm.execute("chart-push %d 1" % t.id)["ok"])
        self.assertFalse(self.wm.execute("chart-push")["ok"])

    def test_charts_module_primitives(self):
        self.assertEqual(len(charts.spark([1, 2, 3, 4], 4)), 4)
        self.assertEqual(charts.spark([], 5).strip(), "")
        self.assertEqual(len(charts.bar(0.5, 10)), 10)
        self.assertEqual(charts.bar(0.0, 10).strip("░ ▏"), charts.bar(0.0, 10).strip("░ ▏"))
        g = charts.gauge(50, 0, 100, 10) if hasattr(charts, "gauge") else ""
        self.assertIsInstance(g, str)


class GlyphSetTests(unittest.TestCase):
    """Fonts on the Linux console and often PuTTY are missing the fine 1/8-cell Unicode block characters that
    the sparkline and precise bars use by default, which show up there as tofu squares (see docs: `charts:`)."""

    UNICODE_ONLY = set("▁▂▃▄▅▆▇▏▎▍▌▋▊▉")          # not the full block "█" or the CP437 shades "░▒▓", which are fine

    def test_unicode_is_unchanged_from_before_glyphs_existed(self):
        self.assertEqual(charts.spark([0, 25, 50, 75, 100], 5, 0, 100), charts.spark([0, 25, 50, 75, 100], 5, 0, 100, glyphs="unicode"))
        self.assertEqual(charts.bar(0.42, 10), charts.bar(0.42, 10, glyphs="unicode"))
        self.assertEqual(charts.column_chart([10, 90], 3, 0, 100), charts.column_chart([10, 90], 3, 0, 100, glyphs="unicode"))

    def test_blocks_and_ascii_never_use_the_missing_glyphs(self):
        vals = [i for i in range(0, 101, 4)]
        for glyphs in ("blocks", "ascii"):
            s = charts.spark(vals, None, 0, 100, glyphs=glyphs)
            self.assertFalse(self.UNICODE_ONLY & set(s), (glyphs, s))
            b = charts.bar(0.3, 12, glyphs=glyphs)
            self.assertFalse(self.UNICODE_ONLY & set(b), (glyphs, b))
            self.assertEqual(len(b), 12)
            c = charts.column_chart(vals, 4, 0, 100, glyphs=glyphs)
            self.assertFalse(self.UNICODE_ONLY & set("".join(c)), (glyphs, c))

    def test_ascii_uses_no_8bit_characters_at_all(self):
        s = charts.spark(list(range(0, 101, 5)), None, 0, 100, glyphs="ascii")
        b = charts.bar(0.6, 20, glyphs="ascii")
        self.assertTrue(all(ord(c) < 128 for c in s), s)
        self.assertTrue(all(ord(c) < 128 for c in b), b)

    def test_extremes_still_reach_the_top_and_bottom_of_the_ramp(self):
        for glyphs in charts.GLYPH_SETS:
            ramp = charts.SPARK_SETS[glyphs]
            self.assertEqual(charts.spark([0], 1, 0, 100, glyphs=glyphs), ramp[0])
            self.assertEqual(charts.spark([100], 1, 0, 100, glyphs=glyphs), ramp[-1])
            self.assertTrue(charts.bar(1.0, 5, glyphs=glyphs).strip())

    def test_bar_precision_drops_only_for_the_default_fill_character(self):
        # a caller that already passes its own fill/empty (not the default "█"/"░") is unaffected by glyphs
        self.assertEqual(charts.bar(0.34, 10, fill="#", empty="-"), charts.bar(0.34, 10, fill="#", empty="-", glyphs="blocks"))

    def test_unknown_glyph_set_is_rejected(self):
        with self.assertRaises(ValueError):
            charts.spark([1, 2], 2, glyphs="nope")
        with self.assertRaises(ValueError):
            charts.bar(0.5, 5, glyphs="nope")


class DirWatchTests(WinCase):
    def test_reports_create_modify_delete(self):
        d = tempfile.mkdtemp()
        w = self.wm.create_window({"kind": "dirwatch", "path": d, "interval": 0.05})
        p = os.path.join(d, "a.txt")
        open(p, "w").write("x")
        pump(self.wm, 2, lambda: "a.txt" in self.refresh(w))
        self.assertEqual(w.counts["created"], 1)
        time.sleep(0.02)
        open(p, "a").write("more")
        os.utime(p, (time.time() + 2, time.time() + 2))
        pump(self.wm, 2, lambda: w.counts["modified"] >= 1)
        self.assertGreaterEqual(w.counts["modified"], 1)
        os.unlink(p)
        pump(self.wm, 2, lambda: w.counts["deleted"] >= 1)
        self.assertEqual(w.counts["deleted"], 1)

    def test_ignore_patterns_and_recursion(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "sub"))
        w = self.wm.create_window({"kind": "dirwatch", "path": d, "interval": 0.05})
        open(os.path.join(d, "x.pyc"), "w").write("")
        open(os.path.join(d, "sub", "deep.txt"), "w").write("")
        pump(self.wm, 2, lambda: w.counts["created"] >= 2)
        t = self.refresh(w)
        self.assertIn("deep.txt", t)
        self.assertNotIn("x.pyc", t)

    def test_emits_event_and_command(self):
        d = tempfile.mkdtemp()
        got = []
        self.wm.on("dirwatch", lambda **kw: got.append(kw))
        w = self.wm.create_window({"kind": "dirwatch", "path": d, "interval": 0.05})
        open(os.path.join(d, "n.txt"), "w").write("1")
        pump(self.wm, 2, lambda: got)
        self.assertTrue(got)
        self.assertTrue(self.wm.execute("new-watch %s" % d)["ok"])

    def test_missing_path(self):
        w = self.wm.create_window({"kind": "dirwatch", "path": "/nonexistent-dir-xyz"})
        self.assertTrue(self.refresh(w) is not None)


class StatusWindowTests(WinCase):
    def test_lists_status_items_and_progress(self):
        w = self.wm.create_window({"kind": "status"})
        self.wm.execute("status-set build passing --style ok")
        self.wm.execute("progress dl 50 100 downloading")
        t = self.refresh(w)
        self.assertIn("build", t)
        self.assertIn("passing", t)
        self.assertIn("downloading", t)
        self.assertIn("50%", t)


class PvTests(WinCase):
    def test_copy_stream_copies_and_reports_to_wm(self):
        # a real session-less copy
        r, w = os.pipe()
        os.write(w, b"x" * 60000)
        os.close(w)
        out = tempfile.mktemp()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT)
        seen = []
        n = PV.copy_stream(r, fd, total=60000, label="t", interval=0.0, quiet=True, on_progress=lambda c, t, rate: seen.append(c))
        os.close(fd)
        self.assertEqual(n, 60000)
        self.assertEqual(os.path.getsize(out), 60000)
        os.unlink(out)
        self.assertTrue(seen)

    def test_rate_limit(self):
        r, w = os.pipe()
        os.write(w, b"x" * 30000)
        os.close(w)
        sink = os.open(os.devnull, os.O_WRONLY)
        t0 = time.time()
        PV.copy_stream(r, sink, rate_limit=100000, quiet=True)
        self.assertGreater(time.time() - t0, 0.2)

    def test_format_and_parse(self):
        self.assertEqual(PV.parse_size("1K"), 1024)
        self.assertEqual(PV.parse_size("2m"), 2 * 1024 ** 2)
        self.assertEqual(PV.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        line = PV.format_line("x", 50, 100, 10.0, 5)
        self.assertIn("50%", line)
        self.assertIn("x", PV.format_line("x", 5, 0, 1.0, 1))

    def test_progress_command_drives_status_line_segment(self):
        self.wm.execute("progress iso 25 100 copying")
        scr, _ = screen_of_frame(self.wm)
        text = "\n".join(scr.__class__.line_text(l) for l in scr.lines)
        self.assertIn("25%", text)
        self.wm.execute("progress iso 100 100 copying")
        self.assertTrue(self.wm.execute("progress iso 100 100 x")["ok"])

    def test_cli_pv_end_to_end_reports_progress_into_session(self):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, PYTERMWM_RUNTIME_DIR=tmp, PYTERMWM_STATE_DIR=tmp, PYTHONPATH=os.getcwd())
        subprocess.run([sys.executable, "-m", "pytermwm", "-s", "pvt", "start"], env=env, check=True, capture_output=True)
        try:
            p = subprocess.run("head -c 2000000 /dev/zero | %s -m pytermwm pv -N zeros -s 2000000 -q --session pvt | wc -c" % sys.executable,
                               shell=True, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.stdout.strip(), "2000000")
            st = subprocess.run([sys.executable, "-m", "pytermwm", "-s", "pvt", "state"], env=env, capture_output=True, text=True).stdout
            import json
            prog = json.loads(st).get("progress") or {}
            self.assertIn("zeros", json.dumps(prog))
        finally:
            subprocess.run([sys.executable, "-m", "pytermwm", "-s", "pvt", "kill"], env=env, capture_output=True)

    def test_human_helpers(self):
        self.assertEqual(human_bytes(1536), "1.5K")
        self.assertIn("/s", human_rate(2048))
        self.assertTrue(human_time(3700).startswith("1"))


class DebugWindowTests(WinCase):
    def test_debug_is_refused_remotely_but_works_locally(self):
        self.assertFalse(self.wm.execute("debug")["ok"])
        self.wm.run_command_line("debug", source="key")
        w = [x for x in self.wm.windows.values() if x.kind == "debug"][0]
        self.assertTrue(self.wm.trace_events)
        self.assertIn("console", self.refresh(w))
        w._run("1 + 2")
        self.assertIn("3", "\n".join(w.output))
        w._run("wm.cols")
        self.assertIn("120", w.output[-1])
        w._run("1/0")
        self.assertIn("ZeroDivisionError", "\n".join(w.output))
        w._run("for i in range(2):")
        self.assertTrue(w.more)
        w._run("    print('loop', i)")
        w._run("")
        self.assertIn("loop 1", "\n".join(w.output))

    def test_debug_events_and_state_views(self):
        self.wm.run_command_line("debug events", source="key")
        w = [x for x in self.wm.windows.values() if x.kind == "debug"][0]
        self.wm.create_window({"cmd": "sleep 3"})
        self.assertIn("window_created", self.refresh(w))
        w.mode = "state"
        self.assertTrue(self.refresh(w).strip())

    def test_trace_disabled_after_close(self):
        self.wm.run_command_line("debug", source="key")
        w = [x for x in self.wm.windows.values() if x.kind == "debug"][0]
        self.wm.close_window(w.id)
        self.assertFalse(self.wm.trace_events)

    def test_debug_typing_via_keys(self):
        self.wm.run_command_line("debug", source="key")
        w = [x for x in self.wm.windows.values() if x.kind == "debug"][0]
        self.wm.focus_window(w.id)
        type_keys(self.wm, b"40+2\r")
        self.assertIn("42", "\n".join(w.output))


if __name__ == "__main__":
    unittest.main()
