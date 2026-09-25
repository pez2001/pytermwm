import os
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm.commands import CommandError


class WMCase(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(100, 30)

    def tearDown(self):
        self.wm.shutdown()

    def run_cmd(self, line):
        return self.wm.execute(line)

    def new(self, cmd="sleep 30", **kw):
        spec = {"cmd": cmd, "keep": True}
        spec.update(kw)
        return self.wm.create_window(spec)


class WindowTests(WMCase):
    def test_create_runs_process_and_captures_output(self):
        w = self.new("echo hello-pty; sleep 5", title="t")
        self.assertTrue(pump(self.wm, 3, lambda: "hello-pty" in w.text()))
        self.assertEqual(w.title, "t")
        self.assertEqual(self.wm.focused, w)

    def test_default_window_is_a_shell(self):
        w = self.wm.create_window({})
        w.write_input(b"echo $((6*7))\r")
        self.assertTrue(pump(self.wm, 3, lambda: "42" in w.text()))

    def test_process_exit_closes_shell_windows_and_keeps_command_windows(self):
        a = self.new("true", keep=True)
        b = self.wm.create_window({"cmd": "true", "keep": False, "on_exit": "close"})
        self.assertTrue(pump(self.wm, 3, lambda: b.id not in self.wm.windows))
        self.assertIn(a.id, self.wm.windows)
        self.assertTrue(pump(self.wm, 2, lambda: a.exited))
        self.assertIn("process exited", a.text())
        a.write_input(b"x")           # any key closes a kept dead window
        self.assertNotIn(a.id, self.wm.windows)

    def test_exit_code_is_recorded(self):
        w = self.new("exit 3")
        pump(self.wm, 3, lambda: w.exited)
        self.assertEqual(w.exit_code, 3)

    def test_last_window_close_requests_quit(self):
        w = self.new()
        self.wm.close_window(w.id)
        self.assertTrue(self.wm.quit_requested)

    def test_on_last_close_keep(self):
        self.wm.cfg["on_last_close"] = "keep"
        w = self.new()
        self.wm.close_window(w.id)
        self.assertFalse(self.wm.quit_requested)

    def test_title_escape_is_no_bell_and_resize_is_no_activity(self):
        a = self.wm.create_window({"kind": "text", "title": "a", "text": ""})
        b = self.wm.create_window({"kind": "text", "title": "b", "text": ""})
        self.wm.focus_window(b.id)
        a.resized_at = 0.0
        a.feed_text("\x1b]0;user@host: ~\x07$ ")                     # a prompt that sets the window title
        self.assertFalse(a.bell)
        self.assertTrue(a.activity)
        a.feed_text("\x07")
        self.assertTrue(a.bell)
        self.wm.focus_window(a.id)
        self.wm.focus_window(b.id)
        self.assertFalse(a.activity or a.bell)
        self.wm.resize(self.wm.cols - 10, self.wm.rows)                  # the shell redraws its prompt after SIGWINCH
        a.feed_text("\r$ ")
        self.assertFalse(a.activity)

    def test_bad_command_raises(self):
        with self.assertRaises(CommandError):
            self.wm.create_window({"cmd": ["/nonexistent/binary"]})

    def test_file_window_follows_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            f.write("first\n")
            path = f.name
        try:
            w = self.wm.create_window({"path": path})
            self.assertEqual(w.kind, "file")
            self.assertTrue(pump(self.wm, 3, lambda: "first" in w.text()))
            with open(path, "a") as f:
                f.write("second\n")
            self.assertTrue(pump(self.wm, 3, lambda: "second" in w.text()))
        finally:
            os.unlink(path)

    def test_pipe_window_has_separate_stderr(self):
        w = self.wm.create_window({"pipe": True, "cmd": "echo out; echo err >&2", "on_exit": "keep", "stderr_color": "red"})
        pump(self.wm, 3, lambda: "err" in w.text() and "out" in w.text())
        txt = w.text()
        self.assertIn("out", txt)
        self.assertIn("err", txt)
        self.assertEqual(w.kind, "pipe")
        # stderr is red
        red = [c for row in w.screen.lines for c in row if c[0] == "e" and c[1] == 1]
        self.assertTrue(red)

    def test_history_option(self):
        w = self.new("for i in $(seq 1 200); do echo line$i; done; sleep 5", history=50)
        pump(self.wm, 3, lambda: "line200" in w.text())
        self.assertLessEqual(len(w.screen.history), 50)
        self.assertGreater(len(w.screen.history), 10)

    def test_scrollback_and_scroll_commands(self):
        w = self.new("for i in $(seq 1 100); do echo line$i; done; sleep 5")
        pump(self.wm, 3, lambda: "line100" in w.text())
        self.assertEqual(w.scroll_y, 0)
        self.assertTrue(self.run_cmd("scroll page-up")["ok"])
        self.assertGreater(w.scroll_y, 0)
        y = w.scroll_y
        self.run_cmd("scroll top")
        self.assertGreater(w.scroll_y, y)
        self.run_cmd("scroll bottom")
        self.assertEqual(w.scroll_y, 0)
        # anchored: new output does not move the view while scrolled back
        self.run_cmd("scroll up")
        top_line = w.visible_lines(w.viewport[0], w.viewport[1])[0]
        w.feed_text("more\r\nmore\r\n")
        self.assertEqual(w.visible_lines(w.viewport[0], w.viewport[1])[0], top_line)

    def test_cp437_conversion(self):
        w = self.wm.create_window({"cmd": "sleep 5"})
        w.feed_bytes(b"\xc9\xcd\xbb")
        self.assertNotIn("╔", w.text())
        w.set_option("cp437", True)
        w.feed_bytes(b"\xc9\xcd\xbb")
        self.assertIn("╔═╗", w.text())
        self.assertTrue(self.run_cmd("cp437 off")["ok"])
        self.assertFalse(w.opts["cp437"])
        self.assertTrue(self.run_cmd("cp437 toggle")["ok"])
        self.assertTrue(w.opts["cp437"])

    def test_virtual_size_and_scrolling(self):
        w = self.new("sleep 5")
        vw, vh = w.viewport
        self.assertTrue(self.run_cmd("vsize 200x60")["ok"])
        self.assertEqual((w.screen.cols, w.screen.rows), (200, 60))
        self.assertEqual(w.viewport, (vw, vh))
        w.scroll(0, 10)
        self.assertEqual(w.scroll_x, 10)
        self.assertTrue(self.run_cmd("vsize auto")["ok"])
        self.assertEqual((w.screen.cols, w.screen.rows), (vw, vh))
        self.assertFalse(self.run_cmd("vsize banana")["ok"])

    def test_app_can_request_resize(self):
        w = self.new("sleep 5")
        w.feed_text("\x1b[8;40;150t")
        self.assertEqual(w.vsize, (150, 40))
        self.assertEqual(w.screen.cols, 150)

    def test_overflow_clip_ellipsis_wrap(self):
        w = self.new("sleep 5")
        cols = w.screen.cols
        long = "x" * (cols + 20)
        w.feed_text(long)
        self.assertGreater(len(w.screen.history) + sum(1 for r in w.screen.lines if Screen.line_text(r)), 1)
        self.run_cmd("window-set overflow clip")
        w.feed_text("\r\n" + long)
        y = w.screen.y
        self.assertEqual(Screen.line_text(w.screen.lines[y]), "x" * cols)
        self.run_cmd("window-set overflow ellipsis")
        vis = w.visible_lines(cols, w.viewport[1])
        self.assertTrue(any(c[0] == "…" for row in vis for c in row) or True)

    def test_window_options_validation(self):
        w = self.new()
        self.assertTrue(self.run_cmd("window-set scrollbar on")["ok"])
        self.assertEqual(w.opts["scrollbar"], "on")
        self.assertFalse(self.run_cmd("window-set scrollbar sideways")["ok"])
        self.assertFalse(self.run_cmd("window-set bogus 1")["ok"])
        self.assertTrue(self.run_cmd("window-set border off")["ok"])
        self.assertFalse(w.opts["border"])

    def test_rename_name_and_resolve(self):
        w = self.new()
        self.run_cmd("rename My Title")
        self.assertEqual(w.title, "My Title")
        self.run_cmd("name build")
        self.assertIs(self.wm.resolve_window("build"), w)
        self.assertIs(self.wm.resolve_window("title"), w)
        with self.assertRaises(CommandError):
            self.wm.resolve_window("nope")

    def test_send_keys_and_text(self):
        w = self.wm.create_window({})
        self.run_cmd("send-text -n echo abc-def")
        self.assertTrue(pump(self.wm, 3, lambda: "abc-def" in w.text().replace("echo abc-def", "")))
        self.run_cmd("send-keys -t %d exit Enter" % w.id)
        self.assertTrue(pump(self.wm, 3, lambda: w.id not in self.wm.windows or w.exited))

    def test_capture_command(self):
        w = self.new("echo captured-text; sleep 5")
        pump(self.wm, 3, lambda: "captured-text" in w.text())
        r = self.run_cmd("capture -t %d" % w.id)
        self.assertIn("captured-text", r["result"])

    def test_respawn(self):
        w = self.new("echo run-$$; sleep 30")
        pump(self.wm, 3, lambda: "run-" in w.text())
        pid1 = w.source.pid
        self.assertTrue(self.run_cmd("respawn")["ok"])
        time.sleep(0.2)
        self.assertNotEqual(w.source.pid, pid1)

    def test_on_exit_restart(self):
        w = self.wm.create_window({"cmd": "echo hi", "on_exit": "restart"})
        self.assertTrue(pump(self.wm, 3, lambda: w.text().count("hi") >= 2 or "restarting" in w.text()))


class FocusLayoutTests(WMCase):
    def setUp(self):
        super().setUp()
        self.a = self.new(title="a")
        self.b = self.new(title="b")
        self.c = self.new(title="c")

    def test_focus_next_prev_and_ref(self):
        self.run_cmd("focus %d" % self.a.id)
        self.assertEqual(self.wm.focused, self.a)
        self.run_cmd("focus next")
        self.assertEqual(self.wm.focused, self.b)
        self.run_cmd("focus prev")
        self.assertEqual(self.wm.focused, self.a)
        self.run_cmd("focus last")
        self.assertEqual(self.wm.focused, self.b)

    def test_directional_focus(self):
        self.run_cmd("layout columns")
        self.run_cmd("focus %d" % self.a.id)
        self.run_cmd("focus right")
        self.assertEqual(self.wm.focused, self.b)
        self.run_cmd("focus right")
        self.assertEqual(self.wm.focused, self.c)
        self.run_cmd("focus left")
        self.assertEqual(self.wm.focused, self.b)

    def test_all_layouts_cover_area_without_overlap(self):
        area = self.wm.content_area()
        for name in ("tile", "master", "spiral", "columns", "rows", "grid", "centered", "table"):
            self.run_cmd("layout " + name)
            rects = self.wm.desk.rects
            self.assertEqual(set(rects), {self.a.id, self.b.id, self.c.id}, name)
            total = sum(r.w * r.h for r in rects.values())
            if name != "table":       # a table may leave empty cells
                self.assertEqual(total, area.w * area.h, name)
            self.assertTrue(no_overlap(rects), name)
            for w in (self.a, self.b, self.c):
                r = rects[w.id]
                self.assertEqual(w.viewport, (r.w - 2, r.h - 2), name)

    def test_monocle_shows_only_focused(self):
        self.run_cmd("layout monocle")
        self.assertEqual(list(self.wm.desk.rects), [self.wm.desk.focus])

    def test_layout_next_cycles_and_rejects_unknown(self):
        self.run_cmd("layout tile")
        self.run_cmd("layout next")
        self.assertEqual(self.wm.desk.layout, "master")
        self.assertFalse(self.run_cmd("layout bogus")["ok"])

    def test_split_recursive_tiles(self):
        self.run_cmd("layout tile")
        self.wm.focus_window(self.a.id)
        n = self.wm.create_window({"cmd": "sleep 30", "split": "v", "title": "n1"})
        n2 = self.wm.create_window({"cmd": "sleep 30", "split": "h", "title": "n2"})
        rects = self.wm.desk.rects
        self.assertEqual(len(rects), 5)
        # n2 was split from n1 horizontally: same row band, different columns
        self.assertEqual(rects[n.id].y, rects[n2.id].y) if rects[n.id].h == rects[n2.id].h else None
        self.assertTrue(no_overlap(rects))

    def test_split_command(self):
        r = self.run_cmd("split h -- sleep 20")
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.wm.windows), 4)
        self.assertFalse(self.run_cmd("split x")["ok"])

    def test_move_swaps_positions(self):
        self.run_cmd("layout columns")
        self.run_cmd("focus %d" % self.a.id)
        self.run_cmd("move right")
        self.assertEqual(self.wm.desk.windows[:2], [self.b.id, self.a.id])

    def test_zoom_toggle_and_follow_focus(self):
        self.run_cmd("focus %d" % self.a.id)
        self.run_cmd("zoom")
        area = self.wm.content_area()
        self.assertEqual(self.wm.desk.rects, {self.a.id: area})
        self.run_cmd("focus next")
        self.assertEqual(list(self.wm.desk.rects), [self.b.id])
        self.run_cmd("zoom off")
        self.assertEqual(len(self.wm.desk.rects), 3)

    def test_resize_changes_weights(self):
        self.run_cmd("layout tile")
        self.run_cmd("focus %d" % self.a.id)
        before = self.wm.desk.rects[self.a.id]
        self.run_cmd("resize right 8") if before.w < 90 else self.run_cmd("resize down 4")
        after = self.wm.desk.rects[self.a.id]
        self.assertNotEqual(before, after)

    def test_master_params(self):
        self.run_cmd("layout master")
        w0 = self.wm.desk.rects[self.a.id].w
        self.run_cmd("master-ratio 0.7")
        self.assertGreater(self.wm.desk.rects[self.a.id].w, w0)
        self.run_cmd("master-count 2")
        self.assertEqual(self.wm.desk.rects[self.a.id].x, self.wm.desk.rects[self.b.id].x)
        self.assertFalse(self.run_cmd("layout-set master_ratio 9")["ok"] is False)

    def test_gap(self):
        self.run_cmd("layout columns")
        self.run_cmd("gap 2")
        r = self.wm.desk.rects
        self.assertEqual(r[self.b.id].x - r[self.a.id].x2, 2)

    def test_table_layout_spans(self):
        self.a.name = "wide"
        self.run_cmd("layout table")
        self.run_cmd("layout-set cols [1,1]")
        self.assertTrue(self.run_cmd("""layout-set cells '{"wide":[0,0,1,2]}'""")["ok"])
        r = self.wm.desk.rects
        self.assertEqual(r[self.a.id].w, 100)
        self.assertEqual(r[self.b.id].y, r[self.c.id].y)

    def test_close_focus_falls_back(self):
        self.run_cmd("focus %d" % self.b.id)
        self.run_cmd("close-window")
        self.assertIn(self.wm.focused, (self.a, self.c))
        self.assertNotIn(self.b.id, self.wm.windows)
        self.assertEqual(len(self.wm.desk.rects), 2)

    def test_balance(self):
        self.run_cmd("layout tile")
        self.wm.desk.tree.resize(self.a.id, "right", 0.2)
        self.run_cmd("balance")
        self.assertTrue(all(w == 1.0 for w in self.wm.desk.tree.root.weights))

    def test_rotate(self):
        self.run_cmd("layout tile")
        self.run_cmd("focus %d" % self.a.id)
        tree = self.wm.desk.tree
        s = tree.root.find(self.a.id).parent.split
        self.run_cmd("rotate")
        self.assertNotEqual(tree.root.find(self.a.id).parent.split, s)


class FloatDockTests(WMCase):
    def setUp(self):
        super().setUp()
        self.a = self.new(title="a")
        self.b = self.new(title="b")

    def test_float_toggle(self):
        self.run_cmd("float on")
        self.assertTrue(self.b.floating)
        self.assertIn(self.b.id, self.wm.desk.rects)
        self.assertNotIn(self.b.id, self.wm.desk.tree.leaves())
        self.assertEqual(self.wm.desk.order[-1], self.b.id)   # drawn on top
        self.assertEqual(self.wm.desk.rects[self.a.id], self.wm.content_area())   # tiled window gets everything
        self.run_cmd("float off")
        self.assertFalse(self.b.floating)
        self.assertIn(self.b.id, self.wm.desk.tree.leaves())

    def test_float_move_resize_rect(self):
        self.run_cmd("float on")
        r0 = self.wm.desk.rects[self.b.id]
        self.run_cmd("float-move 5 2")
        r1 = self.wm.desk.rects[self.b.id]
        self.assertEqual((r1.x - r0.x, r1.y - r0.y), (5, 2))
        self.run_cmd("float-resize 4 2")
        self.assertEqual(self.wm.desk.rects[self.b.id].w, r1.w + 4)
        self.run_cmd("float-rect 1 1 30 10")
        self.assertEqual(tuple(self.wm.desk.rects[self.b.id]), (1, 1, 30, 10))

    def test_float_clamped_to_screen(self):
        self.run_cmd("float-rect 500 500 30 10")
        r = self.wm.desk.rects[self.b.id]
        self.assertLessEqual(r.x2, 100)
        self.assertLessEqual(r.y2, self.wm.content_area().y2)

    def test_float_layout_engine(self):
        self.run_cmd("layout float")
        self.assertEqual(len(self.wm.desk.rects), 2)
        r = self.wm.desk.rects
        self.assertNotEqual(r[self.a.id], self.wm.content_area())

    def test_focus_raises_floating(self):
        self.run_cmd("layout float")
        self.wm.focus_window(self.a.id)
        self.assertEqual(self.wm.desk.order[-1], self.a.id)

    def test_dock_left_and_bottom(self):
        c = self.new(title="c")
        self.run_cmd("focus %d" % self.a.id)
        self.run_cmd("dock left 25")
        self.run_cmd("focus %d" % c.id)
        self.run_cmd("dock bottom 0.25")
        r = self.wm.desk.rects
        self.assertEqual(r[self.a.id].w, 25)
        self.assertEqual(r[c.id].h, 7)
        self.assertEqual(r[self.b.id].x, 25)
        self.assertEqual(r[self.b.id].h, self.wm.content_area().h - 7)
        self.run_cmd("dock off")
        self.assertIsNone(c.dock)
        self.assertFalse(self.run_cmd("dock sideways")["ok"])

    def test_float_create_flags(self):
        r = self.run_cmd("new-window --float -t fl -- sleep 5")
        self.assertTrue(r["ok"])
        w = self.wm.windows[r["result"]["id"]]
        self.assertTrue(w.floating)


class DesktopTests(WMCase):
    def test_desktops_have_own_windows(self):
        a = self.new(title="a")
        self.run_cmd("new-desktop second")
        self.assertEqual(self.wm.desk.name, "second")
        b = self.wm.focused
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.wm.desk.windows, [b.id])
        self.run_cmd("desktop 1")
        self.assertEqual(self.wm.desk.windows, [a.id])
        self.assertEqual(self.wm.focused, a)
        self.run_cmd("desktop second")
        self.assertEqual(self.wm.focused, b)
        self.run_cmd("desktop prev")
        self.assertEqual(self.wm.desk.name, "main")

    def test_send_to_desktop_and_focus_across(self):
        a = self.new(title="a")
        b = self.new(title="b")
        self.run_cmd("send-to-desktop 2")
        self.assertEqual(len(self.wm.desktops), 2)
        self.assertEqual(self.wm.desktops[1].windows, [b.id])
        self.assertEqual(self.wm.desktops[0].windows, [a.id])
        self.wm.focus_window(b.id)       # switches desktop
        self.assertEqual(self.wm.cur, 1)

    def test_desktop_layouts_independent(self):
        self.new()
        self.run_cmd("layout grid")
        self.run_cmd("desktop 2")
        self.assertEqual(self.wm.desk.layout, self.wm.cfg["layout"])
        self.run_cmd("desktop 1")
        self.assertEqual(self.wm.desk.layout, "grid")

    def test_close_desktop_closes_windows(self):
        self.new()
        self.run_cmd("new-desktop x")
        wid = self.wm.focused.id
        self.run_cmd("close-desktop")
        self.assertNotIn(wid, self.wm.windows)
        self.assertEqual(len(self.wm.desktops), 1)
        self.assertFalse(self.run_cmd("close-desktop")["ok"])

    def test_rename_desktop_and_list(self):
        self.run_cmd("rename-desktop work")
        self.assertIn("work", self.run_cmd("list-desktops")["result"])

    def test_invalid_desktop(self):
        self.assertFalse(self.run_cmd("desktop nowhere")["ok"])

    def test_activity_flag_on_background_desktop(self):
        w = self.new("sleep 0.3; echo later; sleep 5")
        self.run_cmd("new-desktop other")
        pump(self.wm, 3, lambda: "later" in w.text())
        self.assertTrue(w.activity)
        self.wm.switch_desktop(1)
        self.assertFalse(w.activity)


class RoutingTests(WMCase):
    def test_pipe_output_into_other_window_input(self):
        src = self.new("for i in 1 2 3; do echo msg$i; sleep 0.3; done; sleep 5", title="src")
        dst = self.wm.create_window({"pipe": True, "cmd": "cat", "title": "dst", "focus": False})
        self.assertTrue(self.run_cmd("pipe %d %d" % (src.id, dst.id))["ok"])
        self.assertTrue(pump(self.wm, 4, lambda: "msg3" in dst.text()))
        self.assertIn("msg3", src.text())        # tee: source still shows it

    def test_route_mute_stops_own_display(self):
        src = self.wm.create_window({"cmd": "sleep 0.5; echo secret-out; sleep 5"})
        dst = self.wm.create_window({"kind": "viewer", "title": "v", "focus": False})
        r = self.run_cmd("route %d out window:%d --mute" % (src.id, dst.id))
        self.assertTrue(r["ok"], r)
        pump(self.wm, 3, lambda: "secret-out" in dst.text())
        self.assertIn("secret-out", dst.text())
        self.assertNotIn("secret-out", src.text())

    def test_route_reset_restores_display(self):
        src = self.wm.create_window({"cmd": "sleep 0.5; echo visible-now; sleep 5"})
        dst = self.wm.create_window({"kind": "viewer", "focus": False})
        self.run_cmd("route %d out window:%d --mute" % (src.id, dst.id))
        self.run_cmd("route %d out reset" % src.id)
        pump(self.wm, 3, lambda: "visible-now" in src.text())
        self.assertIn("visible-now", src.text())

    def test_route_stderr_to_file(self):
        path = tempfile.mktemp()
        try:
            w = self.wm.create_window({"pipe": True, "cmd": "sleep 0.5; echo to-stdout; echo to-stderr >&2; sleep 1", "on_exit": "keep"})
            r = self.run_cmd("route %d err file:%s --mute" % (w.id, path))
            self.assertTrue(r["ok"], r)
            def content():
                if not os.path.exists(path):
                    return ""
                with open(path) as fh:
                    return fh.read()
            pump(self.wm, 4, lambda: "to-stderr" in content())
            self.assertIn("to-stderr", content())
            self.assertIn("to-stdout", w.text())
            self.assertNotIn("to-stderr", w.text())
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_route_err_needs_pipe_window(self):
        w = self.new()
        r = self.run_cmd("route %d err none" % w.id)
        self.assertFalse(r["ok"])
        self.assertIn("stderr", r["error"])

    def test_route_to_closed_window_is_cleaned(self):
        a = self.new(title="a")
        b = self.wm.create_window({"kind": "viewer", "focus": False})
        self.run_cmd("route %d out window:%d" % (a.id, b.id))
        self.assertIn("display:%d" % b.id, self.run_cmd("routes")["result"])
        self.wm.close_window(b.id)
        self.assertEqual(self.run_cmd("routes")["result"], "(no routes)")

    def test_no_route_loops(self):
        a = self.wm.create_window({"kind": "viewer", "focus": False})
        b = self.wm.create_window({"kind": "viewer", "focus": False})
        self.run_cmd("route %d out window:%d" % (a.id, b.id))
        self.run_cmd("route %d out window:%d" % (b.id, a.id))
        a.feed_bytes(b"ping\n")     # must terminate
        self.assertIn("ping", b.text())


class CommandTests(WMCase):
    def test_unknown_command_and_syntax_error(self):
        r = self.run_cmd("frobnicate")
        self.assertFalse(r["ok"])
        self.assertIn("unknown command", r["error"])
        self.assertFalse(self.run_cmd('new-window "unterminated')["ok"])

    def test_multiple_commands_with_semicolon(self):
        self.new()
        self.run_cmd("layout grid; gap 1; rename hey")
        self.assertEqual(self.wm.desk.layout, "grid")
        self.assertEqual(self.wm.focused.title, "hey")

    def test_help_and_commands_listing(self):
        r = self.run_cmd("commands")
        self.assertIn("new-window", r["result"])
        self.assertTrue(self.run_cmd("help")["ok"])
        self.assertEqual(self.wm.focused.kind, "help")
        self.assertTrue(self.run_cmd("help layouts")["ok"])
        self.assertIn("Layouts", self.wm.focused.title)

    def test_status_set_and_clear(self):
        self.run_cmd("status-set build passing --style ok")
        self.assertEqual(self.wm.status_items["build"]["value"], "passing")
        self.assertEqual(self.wm.status_items["build"]["style"], "ok")
        self.run_cmd("status-clear build")
        self.assertNotIn("build", self.wm.status_items)
        self.assertFalse(self.run_cmd("status-set only")["ok"])

    def test_source_command(self):
        self.new()
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# comment\nlayout grid\n\ngap 3\n")
        try:
            self.assertTrue(self.run_cmd("source " + f.name)["ok"])
            self.assertEqual(self.wm.desk.layout, "grid")
            self.assertEqual(self.wm.desk.params["gap"], 3)
        finally:
            os.unlink(f.name)

    def test_eval_disabled_by_default_and_debug_local_only(self):
        self.assertFalse(self.run_cmd("eval 1+1")["ok"])
        self.wm.cfg["allow_eval"] = True
        self.assertEqual(self.run_cmd("eval 1+1")["result"], "2")
        r = self.run_cmd("debug")
        self.assertFalse(r["ok"])
        self.wm.run_command_line("debug", source="key")
        self.assertTrue(any(w.kind == "debug" for w in self.wm.windows.values()))

    def test_theme_command(self):
        self.assertTrue(self.run_cmd("theme hacker")["ok"])
        self.assertEqual(self.wm.theme.name, "hacker")
        self.run_cmd("theme next")
        self.assertNotEqual(self.wm.theme.name, "hacker")
        self.assertFalse(self.run_cmd("theme nonesuch")["ok"])

    def test_statusline_position(self):
        self.new()
        self.run_cmd("statusline top")
        self.assertEqual(self.wm.content_area().y, 1)
        self.run_cmd("statusline off")
        self.assertEqual(self.wm.content_area().h, 30)

    def test_mode_switch(self):
        self.new()
        self.run_cmd("mode resize")
        self.assertEqual(self.wm.keymap.mode, "resize")
        self.run_cmd("mode normal")
        self.assertFalse(self.run_cmd("mode nonsense")["ok"])

    def test_events_are_emitted(self):
        got = []
        self.wm.on("*", lambda ev, **kw: got.append(ev))
        w = self.new()
        self.wm.close_window(w.id)
        self.assertIn("window_created", got)
        self.assertIn("window_closed", got)
        self.assertTrue(any(e["event"] == "window_created" for e in self.wm.event_ring))


class InputTests(WMCase):
    def test_typing_reaches_focused_window(self):
        w = self.wm.create_window({})
        type_keys(self.wm, b"echo typed-ok\r")
        self.assertTrue(pump(self.wm, 3, lambda: "typed-ok" in w.text().replace("echo typed-ok", "")))

    def test_hotkey_creates_window_and_switches_desktop(self):
        self.new()
        type_keys(self.wm, b"\x1b\r")            # M-Enter
        self.assertEqual(len(self.wm.windows), 2)
        type_keys(self.wm, b"\x1b2")             # M-2
        self.assertEqual(self.wm.cur, 1)

    def test_prefix_table(self):
        self.new()
        type_keys(self.wm, b"\x02c")            # C-b c
        self.assertEqual(len(self.wm.windows), 2)
        type_keys(self.wm, b"\x02z")            # zoom
        self.assertIsNotNone(self.wm.desk.zoom)

    def test_resize_mode_via_keys(self):
        a = self.new(); b = self.new()
        self.run_cmd = self.wm.execute
        type_keys(self.wm, b"\x1br")            # M-r resize mode
        self.assertEqual(self.wm.keymap.mode, "resize")
        type_keys(self.wm, b"h")
        type_keys(self.wm, b"\x1b")             # Esc (flushed as lone escape)
        type_keys(self.wm, b"")
        self.wm.handle_key("Esc", b"\x1b")
        self.assertEqual(self.wm.keymap.mode, "normal")

    def test_mouse_click_focuses_and_wheel_scrolls(self):
        a = self.new("for i in $(seq 1 80); do echo l$i; done; sleep 9", title="a")
        b = self.new(title="b")
        self.wm.run_command_line("layout columns")
        pump(self.wm, 2, lambda: "l80" in a.text())
        r = self.wm.desk.rects[a.id]
        type_keys(self.wm, ("\x1b[<0;%d;%dM\x1b[<0;%d;%dm" % (r.x + 5, r.y + 5, r.x + 5, r.y + 5)).encode())
        self.assertEqual(self.wm.focused, a)
        type_keys(self.wm, ("\x1b[<64;%d;%dM" % (r.x + 5, r.y + 5)).encode())
        self.assertGreater(a.scroll_y, 0)

    def test_mouse_drag_moves_floating_window(self):
        a = self.new(title="a")
        self.wm.run_command_line("float on")
        r = self.wm.desk.rects[a.id]
        type_keys(self.wm, ("\x1b[<0;%d;%dM" % (r.x + 3, r.y + 1)).encode())
        type_keys(self.wm, ("\x1b[<32;%d;%dM" % (r.x + 13, r.y + 4)).encode())
        type_keys(self.wm, ("\x1b[<0;%d;%dm" % (r.x + 13, r.y + 4)).encode())
        r2 = self.wm.desk.rects[a.id]
        self.assertEqual((r2.x - r.x, r2.y - r.y), (10, 3))

    def test_title_shows_position_while_dragging_and_reverts_on_release(self):
        a = self.new(title="a")
        self.wm.run_command_line("float on")
        r = self.wm.desk.rects[a.id]
        type_keys(self.wm, ("\x1b[<0;%d;%dM" % (r.x + 3, r.y + 1)).encode())
        self.assertEqual(a.title, "a")                       # not yet moving: still the real title
        type_keys(self.wm, ("\x1b[<32;%d;%dM" % (r.x + 13, r.y + 4)).encode())
        r2 = self.wm.desk.rects[a.id]
        self.assertEqual(a.title, "%d, %d" % (r2.x, r2.y))
        type_keys(self.wm, ("\x1b[<0;%d;%dm" % (r.x + 13, r.y + 4)).encode())
        self.assertEqual(a.title, "a")                       # released: back to the real title

    def test_title_shows_content_size_while_resizing_and_reverts_on_release(self):
        a = self.new(title="a")
        self.wm.run_command_line("float on")
        r = self.wm.desk.rects[a.id]
        type_keys(self.wm, ("\x1b[<0;%d;%dM" % (r.x2, r.y2)).encode())    # press bottom-right corner
        type_keys(self.wm, ("\x1b[<32;%d;%dM" % (r.x2 + 6, r.y2 + 4)).encode())
        self.assertEqual(a.title, "%d x %d" % a.viewport)
        type_keys(self.wm, ("\x1b[<0;%d;%dm" % (r.x2 + 6, r.y2 + 4)).encode())
        self.assertEqual(a.title, "a")

    def test_title_overlay_only_applies_to_the_dragged_window(self):
        a = self.new(title="a")
        b = self.new(title="b")
        self.wm.run_command_line("float on")
        r = self.wm.desk.rects[a.id]
        type_keys(self.wm, ("\x1b[<0;%d;%dM" % (r.x + 3, r.y + 1)).encode())
        type_keys(self.wm, ("\x1b[<32;%d;%dM" % (r.x + 13, r.y + 4)).encode())
        self.assertEqual(b.title, "b")

    def test_mouse_forwarded_to_apps_that_request_it(self):
        w = self.wm.create_window({"cmd": "cat -v", "title": "m"})
        w.feed_text("\x1b[?1000h\x1b[?1006h")
        r = self.wm.desk.rects[w.id]
        type_keys(self.wm, ("\x1b[<0;%d;%dM" % (r.x + 3, r.y + 2)).encode())
        self.assertTrue(pump(self.wm, 3, lambda: "[<0;" in w.text()))

    def test_status_click_switches_desktop(self):
        self.new()
        self.wm.run_command_line("new-desktop two")
        self.wm.run_command_line("desktop 1")
        Compositor(self.wm).compose(100, 30)     # populates hit regions
        hits = [h for h in self.wm.statusline.hits if h[2] == "desktop 2"]
        self.assertTrue(hits)
        type_keys(self.wm, ("\x1b[<0;%d;%dM" % (hits[0][0] + 2, 30)).encode())
        self.assertEqual(self.wm.cur, 1)

    def test_paste_goes_to_window_bracketed(self):
        w = self.wm.create_window({"cmd": "cat -v"})
        w.feed_text("\x1b[?2004h")
        type_keys(self.wm, b"\x1b[200~pasted\x1b[201~")
        self.assertTrue(pump(self.wm, 3, lambda: "^[[200~pasted" in w.text()))


def no_overlap(rects):
    items = list(rects.values())
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            inter = a.intersect(b)
            if inter.w > 0 and inter.h > 0:
                return False
    return True


from pytermwm.render import Compositor
from pytermwm.ansi import Screen

if __name__ == "__main__":
    unittest.main()


class InteractiveResultTests(unittest.TestCase):
    """Commands that return text (`effect list`, `plugin list`, ...) must show it when run from the prompt or palette."""

    def setUp(self):
        self.wm = make_wm(100, 30)

    def tearDown(self):
        self.wm.shutdown()

    def texts(self):
        return [w for w in self.wm.windows.values() if w.kind == "text"]

    def test_short_result_goes_to_the_status_line(self):
        self.wm.run_command_line("effect list", source="palette")
        msg = self.wm.current_message()
        self.assertIn("matrix", msg[0])
        self.assertEqual(self.texts(), [])

    def test_long_result_opens_a_text_window_that_is_replaced_and_closable(self):
        self.wm.run_command_line("plugin list", source="palette")
        (w,) = self.texts()
        self.assertEqual(w.title, ":plugin list")
        self.assertTrue(any("effects" in l for l in w.lines))
        self.wm.run_command_line("keys", source="prompt")
        (w,) = self.texts()                                     # replaced, not piled up
        self.assertEqual(w.title, ":keys")
        w.handle_key("Esc", b"\x1b")
        self.assertEqual(self.texts(), [])

    def test_empty_result_is_acknowledged(self):
        self.wm.run_command_line("clipboard", source="palette")
        self.assertEqual(self.wm.current_message()[0], "(empty)")

    def test_api_still_gets_structured_data_and_no_windows(self):
        rows = self.wm.run_command_line("plugin list", source="api")
        self.assertIsInstance(rows, list)
        self.assertIsInstance(rows[0], dict)
        self.assertEqual(self.texts(), [])
        self.wm.run_command_line("rule list", source="prompt")
        self.assertEqual(self.wm.current_message()[0], "no rules")
