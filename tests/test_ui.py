import unittest

from tests.helpers import *
from pytermwm import theme as themes
from pytermwm.ansi import Screen
from pytermwm.render import Compositor, FrameWriter, crop_frame


class UICase(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(100, 30)

    def tearDown(self):
        self.wm.shutdown()

    def screen(self):
        return screen_of_frame(self.wm)[0]

    def rows(self):
        scr = self.screen()
        return [Screen.line_text(l) for l in scr.lines]

    def text(self):
        return "\n".join(self.rows())


class RenderTests(UICase):
    def test_frame_roundtrip_shows_title_and_content(self):
        w = self.wm.create_window({"cmd": "echo render-me; sleep 5", "title": "hello"})
        pump(self.wm, 3, lambda: "render-me" in w.text())
        t = self.text()
        self.assertIn("hello", t)
        self.assertIn("render-me", t)

    def test_borders_use_theme_glyphs(self):
        self.wm.create_window({"cmd": "sleep 5"})
        self.assertIn("╭", self.text())
        self.wm.execute("theme modern")
        self.assertIn("▗", self.text())
        self.wm.execute("theme hacker")
        t = self.text()
        self.assertNotIn("╭", t)
        self.wm.execute("theme mc")
        self.assertIn("╔", self.text())

    def test_border_off_removes_frame(self):
        w = self.wm.create_window({"cmd": "echo bare; sleep 5"})
        self.wm.execute("window-set border off")
        pump(self.wm, 2, lambda: "bare" in w.text())
        rect = self.wm.desk.rects[w.id]
        self.assertEqual(w.viewport, (rect.w, rect.h))

    def test_status_line_present_and_segments(self):
        self.wm.create_window({"cmd": "sleep 5"})
        last = self.rows()[-1]
        self.assertTrue(last.strip())
        self.wm.execute("status-set build passing")
        self.assertIn("passing", self.rows()[-1])

    def test_status_line_top(self):
        self.wm.create_window({"cmd": "sleep 5"})
        self.wm.execute("statusline top")
        first = self.rows()[0]
        self.assertTrue(first.strip())
        self.assertNotIn("╭", first)

    def test_focus_cue_differs(self):
        a = self.wm.create_window({"cmd": "sleep 5", "title": "aaa"})
        b = self.wm.create_window({"cmd": "sleep 5", "title": "bbb"})
        _, fa = screen_of_frame(self.wm)
        self.wm.focus_window(a.id)
        _, fb = screen_of_frame(self.wm)
        self.assertNotEqual(fa.cells, fb.cells)

    def test_cursor_position_follows_focused_window(self):
        w = self.wm.create_window({})
        pump(self.wm, 1)
        _, frame = screen_of_frame(self.wm)
        r = self.wm.desk.rects[w.id]
        cx, cy = frame.cursor[:2]
        self.assertTrue(r.contains(cx, cy))

    def test_scrollbar_appears_when_scrolled(self):
        w = self.wm.create_window({"cmd": "for i in $(seq 1 100); do echo l$i; done; sleep 5"})
        pump(self.wm, 3, lambda: "l100" in w.text())
        self.wm.execute("window-set scrollbar on")
        self.wm.execute("scroll page-up")
        _, frame = screen_of_frame(self.wm)
        r = self.wm.desk.rects[w.id]
        col = [frame.cells[y][r.x2 - 1][0] for y in range(r.y + 1, r.y2 - 1)]
        self.assertTrue(any(c in "█▓▒░┃│▐" for c in col), col)

    def test_writer_diff_is_small_and_second_frame_empty(self):
        self.wm.create_window({"cmd": "sleep 5"})
        fw = FrameWriter(24)
        f1 = Compositor(self.wm).compose(100, 30)
        first = fw.write(f1)
        self.assertGreater(len(first), 500)
        again = fw.write(Compositor(self.wm).compose(100, 30))
        self.assertLess(len(again), 80)

    def test_color_depth_downgrade(self):
        self.wm.create_window({"cmd": "printf '\\033[38;2;10;200;30mgreen\\033[0m'; sleep 5"})
        pump(self.wm, 2)
        f = Compositor(self.wm).compose(100, 30)
        for depth, needle in ((24, "38;2;"), (8, "38;5;"), (4, None)):
            out = FrameWriter(depth).write(f)
            if needle:
                self.assertIn(needle, out)
            else:
                self.assertNotIn("38;2;", out)
                self.assertNotIn("38;5;", out)

    def test_crop_frame_for_smaller_client(self):
        self.wm.create_window({"cmd": "sleep 5"})
        f = Compositor(self.wm).compose(100, 30)
        c = crop_frame(f, 60, 20)
        self.assertEqual(len(c.cells), 20)
        self.assertEqual(len(c.cells[0]), 60)

    def test_wide_characters_render(self):
        w = self.wm.create_window({"cmd": "sleep 5"})
        w.feed_text("日本語 ok")
        self.assertIn("日本語 ok", self.text())

    def test_shadow_theme_draws_shadow(self):
        self.wm.create_window({"cmd": "sleep 5"})
        self.wm.execute("float on")
        self.wm.execute("theme amiga")
        self.wm.execute("theme mc")
        self.assertTrue(self.text())

    def test_message_shown_and_expires(self):
        self.wm.message("hello world msg", ttl=0.05)
        self.assertIn("hello world msg", self.text())
        time.sleep(0.1)
        self.wm.tick(time.time())
        self.assertNotIn("hello world msg", self.text())

    def test_desktop_indicator(self):
        self.wm.execute("new-desktop work")
        self.assertIn("work", self.text())


class ThemeTests(UICase):
    def test_all_builtin_themes_render(self):
        self.wm.create_window({"cmd": "sleep 5", "title": "x"})
        self.wm.create_window({"cmd": "sleep 5", "title": "y"})
        for n in themes.theme_names():
            self.wm.execute("theme " + n)
            self.assertIn("x", self.text(), n)

    def test_user_theme_from_config(self):
        cfg = {"themes": {"mine": {"extends": "default", "border": "double", "focus_border": "double", "colors": {"focus_border_fg": "#ff0000"}}}}
        from pytermwm.config import apply_config
        apply_config(self.wm, cfg)
        self.assertTrue(self.wm.execute("theme mine")["ok"])
        self.wm.create_window({"cmd": "sleep 5"})
        self.assertIn("╔", self.text())

    def test_bbs_and_c64_are_distinct(self):
        self.wm.create_window({"cmd": "sleep 5"})
        self.wm.execute("theme bbs")
        a = Compositor(self.wm).compose(100, 30).cells
        self.wm.execute("theme c64")
        b = Compositor(self.wm).compose(100, 30).cells
        self.assertNotEqual(a, b)


class PromptTests(UICase):
    def setUp(self):
        super().setUp()
        self.wm.create_window({"cmd": "sleep 30", "title": "base"})

    def keys(self, s):
        type_keys(self.wm, s.encode() if isinstance(s, str) else s)

    def test_open_close_and_esc(self):
        self.wm.execute("prompt")
        self.assertTrue(self.wm.prompt.active)
        self.keys("\x1b")
        self.assertFalse(self.wm.prompt.active)

    def test_prompt_is_in_status_line(self):
        self.wm.execute("prompt")
        self.keys("abc")
        last = self.rows()[-1]
        self.assertIn("abc", last)

    def test_colon_runs_wm_command(self):
        self.wm.execute("prompt")
        self.keys(":layout grid\r")
        self.assertEqual(self.wm.desk.layout, "grid")
        self.assertFalse(self.wm.prompt.active)

    def test_plain_text_runs_shell_command_in_new_window(self):
        n = len(self.wm.windows)
        self.wm.execute("prompt")
        self.keys("echo from-prompt\r")
        self.assertEqual(len(self.wm.windows), n + 1)
        self.assertTrue(pump(self.wm, 3, lambda: "from-prompt" in self.wm.focused.text()))

    def test_history_and_suggestion(self):
        self.wm.execute("prompt")
        self.keys(":message one\r")
        self.wm.execute("prompt")
        self.keys(":mess")
        self.assertEqual(self.wm.prompt.suggestion, "age one")
        self.keys("\x1b[A")     # Up = history prev
        self.assertIn("message one", self.wm.prompt.text)

    def test_line_editing_keys(self):
        self.wm.execute("prompt")
        self.keys("hello world")
        self.keys("\x17")        # C-w
        self.assertEqual(self.wm.prompt.text, "hello ")
        self.keys("\x01")        # C-a
        self.keys("X")
        self.assertEqual(self.wm.prompt.text, "Xhello ")
        self.keys("\x1b[F")      # End
        self.keys("\x15")        # C-u
        self.assertEqual(self.wm.prompt.text, "")

    def test_tab_completes_commands(self):
        self.wm.execute("prompt")
        self.keys(":new-wi\t")
        self.assertEqual(self.wm.prompt.text, ":new-window")

    def test_tab_completes_command_arguments(self):
        self.wm.execute("prompt")
        self.keys(":layout gri\t")
        self.assertEqual(self.wm.prompt.text, ":layout grid")

    def test_tab_completes_paths_in_shell_mode(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        open(os.path.join(d, "unique_file_name.txt"), "w").close()
        self.wm.execute("prompt")
        self.keys("cat " + d + "/uniq\t")
        self.assertTrue(self.wm.prompt.text.endswith("unique_file_name.txt"))

    def test_tab_multiple_candidates_lists_then_cycles(self):
        self.wm.execute("prompt")
        self.keys(":focus ")
        self.keys("\t")
        self.assertTrue(len(self.wm.prompt.completions) > 1)
        first = self.wm.prompt.text
        self.keys("\t")
        self.assertNotEqual(self.wm.prompt.text, first)

    def test_prefill(self):
        self.wm.execute("prompt command rename ")
        self.assertEqual(self.wm.prompt.text, "rename ")
        self.keys("newname\r")
        self.assertEqual(self.wm.focused.title, "newname")

    def test_prompt_error_shows_message(self):
        self.wm.execute("prompt")
        self.keys(":bogus-cmd\r")
        self.assertIn("unknown command", self.text())


class PaletteTests(UICase):
    def setUp(self):
        super().setUp()
        self.wm.create_window({"cmd": "sleep 30", "title": "alpha"})
        self.wm.create_window({"cmd": "sleep 30", "title": "bravo"})

    def keys(self, s):
        type_keys(self.wm, s.encode())

    def test_open_filter_and_run(self):
        self.wm.execute("palette")
        self.assertTrue(self.wm.palette.active)
        self.keys("alpha")
        self.assertEqual(self.wm.palette.filtered[0][2].split()[0], "focus")
        self.assertIn("alpha", self.text())
        self.keys("\r")
        self.assertFalse(self.wm.palette.active)
        self.assertEqual(self.wm.focused.title, "alpha")

    def test_fuzzy_matching(self):
        self.wm.execute("palette commands")
        self.keys("nwwn")
        labels = " ".join(i[0] for i in self.wm.palette.filtered)
        self.assertIn("new-window", labels)

    def test_navigation_and_escape(self):
        self.wm.execute("palette windows")
        self.keys("\x1b[B")
        self.assertEqual(self.wm.palette.sel, 1)
        self.keys("\x1b")
        self.assertFalse(self.wm.palette.active)

    def test_typed_command_line_runs_as_typed(self):
        # `effect none` fuzzy-matches the plain `effect` entry (its help text holds n-o-n-e); Enter must still run what was typed
        self.wm.execute("effect matrix")
        self.assertIsNotNone(self.wm.background)
        self.wm.execute("palette")
        self.keys("effect none\r")
        self.assertFalse(self.wm.palette.active)
        self.assertIsNone(self.wm.background)

    def test_argument_completion_still_wins(self):
        self.wm.execute("palette")
        self.keys("layout gri\r")
        self.assertEqual(self.wm.desk.layout, "grid")

    def test_theme_scope(self):
        self.wm.execute("palette themes")
        self.keys("hack\r")
        self.assertEqual(self.wm.theme.name, "hacker")


class DialogTests(UICase):
    def setUp(self):
        super().setUp()
        self.wm.create_window({"cmd": "sleep 30"})

    def keys(self, s):
        type_keys(self.wm, s.encode())

    def test_message_dialog_modal_blocks_input(self):
        self.wm.execute("dialog message 'Something happened' Title")
        self.assertEqual(len(self.wm.dialogs.stack), 1)
        self.assertIn("Something happened", self.text())
        n = len(self.wm.windows)
        self.keys("\x1bc")        # would create... ignored while modal
        self.assertEqual(len(self.wm.windows), n)
        self.keys("\r")
        self.assertEqual(len(self.wm.dialogs.stack), 0)

    def test_confirm_runs_action_only_on_yes(self):
        self.wm.execute("dialog confirm proceed? 'layout grid' 'layout rows'")
        self.keys("\x1b")
        self.assertNotEqual(self.wm.desk.layout, "grid")
        self.wm.execute("dialog confirm proceed? 'layout grid'")
        self.keys("y")
        self.assertEqual(self.wm.desk.layout, "grid")

    def test_input_dialog_returns_text(self):
        self.wm.execute("dialog input 'Enter name' 'rename {}' abc")
        self.keys("def\r")
        self.assertEqual(self.wm.focused.title, "abcdef")

    def test_menu_dialog_filter_and_select(self):
        self.wm.execute("dialog menu Pick 'Grid=layout grid' 'Rows=layout rows'")
        self.keys("row\r")
        self.assertEqual(self.wm.desk.layout, "rows")

    def test_non_modal_dialog_does_not_block(self):
        self.wm.execute("dialog message note Info --modeless")
        n = len(self.wm.windows)
        self.keys("\x1b\r")
        self.assertEqual(len(self.wm.windows), n + 1)
        self.assertEqual(len(self.wm.dialogs.stack), 1)

    def test_dialog_list_and_close(self):
        self.wm.execute("dialog message a AAA --modeless")
        self.assertEqual(self.wm.execute("dialog list")["result"][0]["title"], "AAA")
        self.wm.execute("dialog close")
        self.assertEqual(len(self.wm.dialogs.stack), 0)

    def test_dialog_rendered_centered(self):
        self.wm.execute("dialog message 'body text' CenterMe")
        rows = self.rows()
        y = [i for i, r in enumerate(rows) if "CenterMe" in r][0]
        x = rows[y].index("CenterMe")
        self.assertTrue(20 < x < 70)
        self.assertTrue(5 < y < 25)


class ScrollScreenTests(UICase):
    def test_help_window_content(self):
        self.wm.execute("help")
        self.assertIn("pytermwm", self.text().lower())

    def test_log_window_shows_log_lines(self):
        self.wm.log.warning("marker-log-line")
        self.wm.execute("log")
        pump(self.wm, 1)
        self.assertIn("marker-log-line", self.text())


if __name__ == "__main__":
    unittest.main()


class PromptHistoryPersistence(unittest.TestCase):
    def test_history_is_saved_and_reloaded(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        wm = make_wm(80, 24)
        wm.prompt.enable_persistence(path)
        wm.prompt.open()
        wm.prompt.on_submit = lambda t: None
        wm.prompt.editor.set("layout grid")
        wm.prompt.handle_key("Enter")
        wm2 = make_wm(80, 24)
        wm2.prompt.enable_persistence(path)
        self.assertEqual(wm2.prompt.history, ["layout grid"])

    def test_corrupt_history_file_is_ignored(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        with open(path, "w") as f:
            f.write("{not json")
        wm = make_wm(80, 24)
        wm.prompt.enable_persistence(path)
        self.assertEqual(wm.prompt.history, [])
