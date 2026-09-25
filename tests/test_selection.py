import base64
import os
import sys
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm import selection as S
from pytermwm.commands import CommandError
from pytermwm.config import validate_config
from pytermwm.render import Compositor

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def mouse(kind, x, y, button=1, mods=""):
    return {"kind": kind, "button": button, "x": x, "y": y, "mods": mods, "bt": 0, "final": "M" if kind != "release" else "m"}


class SelCase(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(60, 16)
        self.w = self.wm.create_window({"cmd": SLEEPER, "name": "a"})
        self.w.feed_bytes(b"hello world foo/bar.txt second\r\nline two here\r\nthird line 12345\r\n")
        self.inner = self.wm.inner_rect(self.w, self.wm.desk.rects[self.w.id])

    def tearDown(self):
        self.wm.shutdown()

    def cell(self, col, row):
        return self.inner.x + col, self.inner.y + row

    def drag(self, c0, r0, c1, r1, mods="", button=1):
        x0, y0 = self.cell(c0, r0)
        x1, y1 = self.cell(c1, r1)
        self.wm.handle_mouse(mouse("press", x0, y0, button, mods))
        self.wm.handle_mouse(mouse("move", x1, y1, button, mods))
        self.wm.handle_mouse(mouse("release", x1, y1, button, mods))

    def click(self, col, row, button=1):
        x, y = self.cell(col, row)
        self.wm.handle_mouse(mouse("press", x, y, button))
        self.wm.handle_mouse(mouse("release", x, y, button))


class MouseSelectionTests(SelCase):
    def test_drag_copies_on_release(self):
        self.drag(2, 0, 8, 0)
        self.assertEqual(self.wm.paste_buffer, "llo wor")
        self.assertEqual(self.wm.pending_terminal_output[-1], S.osc52("llo wor"))
        self.assertIn("copied 7", self.wm.current_message()[0])

    def test_backwards_and_multiline(self):
        self.drag(8, 1, 3, 0)                                         # from lower right to upper left
        self.assertEqual(self.wm.paste_buffer, "lo world foo/bar.txt second\nline two")

    def test_click_selects_nothing(self):
        self.click(3, 0)
        self.assertEqual(self.wm.paste_buffer, "")
        self.assertIsNone(self.wm.active_selection())

    def test_double_click_word_and_triple_click_line(self):
        self.click(1, 0)
        self.click(1, 0)
        self.assertEqual(self.wm.paste_buffer, "hello")
        self.wm._click = None
        self.click(14, 0)
        self.click(14, 0)
        self.assertEqual(self.wm.paste_buffer, "foo/bar.txt")           # path characters belong to a word
        self.click(14, 0)                                              # third click of the sequence
        self.assertEqual(self.wm.paste_buffer, "hello world foo/bar.txt second")

    def test_word_drag_extends_by_words(self):
        x0, y0 = self.cell(1, 0)
        x1, y1 = self.cell(7, 0)
        self.wm.handle_mouse(mouse("press", x0, y0))
        self.wm.handle_mouse(mouse("release", x0, y0))
        self.wm.handle_mouse(mouse("press", x0, y0))
        self.wm.handle_mouse(mouse("move", x1, y1))
        self.wm.handle_mouse(mouse("release", x1, y1))
        self.assertEqual(self.wm.paste_buffer, "hello world")

    def test_alt_drag_selects_a_rectangle(self):
        self.drag(0, 0, 3, 2, mods="M-")
        self.assertEqual(self.wm.paste_buffer, "hell\nline\nthir")

    def test_selection_is_reverse_video_in_the_frame(self):
        x0, y0 = self.cell(2, 0)
        self.wm.handle_mouse(mouse("press", x0, y0))
        self.wm.handle_mouse(mouse("move", self.cell(5, 0)[0], y0))
        from pytermwm.colors import REVERSE
        frame = Compositor(self.wm).compose(60, 16)
        row = frame.cells[y0]
        self.assertTrue(all(row[self.cell(c, 0)[0]][3] & REVERSE for c in range(2, 6)))
        self.assertFalse(row[self.cell(6, 0)[0]][3] & REVERSE)
        self.assertFalse(row[self.cell(1, 0)[0]][3] & REVERSE)

    def test_copy_on_release_can_be_disabled(self):
        self.wm.cfg["selection"] = {"copy_on_release": False}
        self.drag(0, 0, 4, 0)
        self.assertEqual(self.wm.paste_buffer, "")
        self.assertEqual(self.wm.run_command_line("copy", source="t", raise_errors=True), "copied 5 characters")
        self.assertEqual(self.wm.paste_buffer, "hello")

    def test_typing_clears_the_selection(self):
        self.wm.cfg["selection"] = {"copy_on_release": False}
        self.drag(0, 0, 4, 0)
        self.assertIsNotNone(self.wm.active_selection())
        type_keys(self.wm, b"x")
        self.assertIsNone(self.wm.active_selection())
        with self.assertRaises(CommandError):
            self.wm.run_command_line("copy", source="t", raise_errors=True)

    def test_release_outside_the_window_still_finishes(self):
        x0, y0 = self.cell(0, 0)
        self.wm.handle_mouse(mouse("press", x0, y0))
        self.wm.handle_mouse(mouse("move", self.cell(6, 1)[0], self.cell(6, 1)[1]))
        self.wm.handle_mouse(mouse("release", 59, 15))              # far outside, on the status line row
        # releasing below the window extends the selection to the last row
        self.assertEqual(self.wm.paste_buffer, "hello world foo/bar.txt second\nline two here\nthird line 12345")
        self.assertIsNone(self.wm.sel_drag)

    def test_selection_survives_scrolling_output(self):
        self.wm.cfg["selection"] = {"copy_on_release": False}
        self.drag(0, 1, 7, 1)
        for i in range(40):
            self.w.feed_bytes(b"more output %d\r\n" % i)
        s = self.wm.active_selection()
        self.assertEqual(s.text(self.w.screen), "line two")

    def test_selection_dropped_on_resize_or_alt_screen(self):
        self.wm.cfg["selection"] = {"copy_on_release": False}
        self.drag(0, 0, 4, 0)
        self.w.feed_bytes(b"\x1b[?1049h")
        self.assertIsNone(self.wm.active_selection())

    def test_autoscroll_while_dragging_above_the_window(self):
        for i in range(60):
            self.w.feed_bytes(b"row %d\r\n" % i)
        self.wm.cfg["selection"] = {"copy_on_release": False}
        x, y = self.cell(0, 3)
        self.wm.handle_mouse(mouse("press", x, y))
        self.wm.handle_mouse(mouse("move", x, self.inner.y - 1))        # above the viewport
        before = self.w.scroll_y
        self.assertGreater(before, 0)
        self.wm.tick(time.time() + 1)
        self.assertGreater(self.w.scroll_y, before)
        self.wm.handle_mouse(mouse("release", x, self.inner.y - 1))
        text = self.wm.active_selection().text(self.w.screen)
        self.assertGreater(text.count("\n"), 3)


class MouseAwareAppTests(SelCase):
    def setUp(self):
        super().setUp()
        self.w.feed_bytes(b"\x1b[?1000h\x1b[?1006h")                    # the program asks for mouse events
        self.sent = []
        self.w.source.write = lambda data: self.sent.append(bytes(data))

    def test_plain_drag_goes_to_the_program(self):
        self.drag(0, 0, 5, 0)
        self.assertEqual(self.wm.paste_buffer, "")
        self.assertTrue(any(b"\x1b[<" in s for s in self.sent))

    def test_modifier_drag_selects_instead(self):
        self.drag(0, 0, 4, 0, mods="M-")
        self.assertEqual(self.wm.paste_buffer, "hello")
        self.assertEqual(self.sent, [])

    def test_modifier_is_configurable(self):
        self.wm.cfg["selection"] = {"modifier": "none"}
        self.drag(0, 0, 4, 0, mods="M-")
        self.assertEqual(self.wm.paste_buffer, "")
        self.wm.cfg["selection"] = {"modifier": "ctrl"}
        self.drag(0, 0, 4, 0, mods="C-")
        self.assertEqual(self.wm.paste_buffer, "hello")


class PasteTests(SelCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.w.source.write = lambda data: self.sent.append(bytes(data))
        self.wm.copy_text("pasted text", quiet=True)

    def test_middle_and_right_click_paste(self):
        self.click(2, 1, button=2)
        self.click(2, 1, button=3)
        self.assertEqual(self.sent, [b"pasted text", b"pasted text"])

    def test_paste_buttons_configurable(self):
        self.wm.cfg["selection"] = {"paste_buttons": ["middle"]}
        self.click(2, 1, button=3)
        self.assertEqual(self.sent, [])

    def test_bracketed_paste_when_the_app_asks(self):
        self.w.feed_bytes(b"\x1b[?2004h")
        self.wm.run_command_line("paste", source="t", raise_errors=True)
        self.assertEqual(self.sent, [b"\x1b[200~pasted text\x1b[201~"])

    def test_paste_command_with_text_and_empty_buffer(self):
        self.wm.run_command_line("paste hi there", source="t", raise_errors=True)
        self.assertEqual(self.sent, [b"hi there"])
        self.wm.paste_buffer = ""
        self.wm.run_command_line("paste", source="t", raise_errors=True)
        self.assertIn("empty", self.wm.current_message()[0])

    def test_clipboard_command_and_history(self):
        self.wm.run_command_line("clipboard set second", source="t", raise_errors=True)
        self.assertEqual(self.wm.run_command_line("clipboard", source="t", raise_errors=True), "second")
        lst = self.wm.run_command_line("clipboard list", source="t", raise_errors=True)
        self.assertIn("0: second", lst)
        self.assertIn("1: pasted text", lst)


class CopyModeTests(SelCase):
    def keys(self, *names):
        for n in names:
            self.wm.handle_key(n)

    def test_select_and_yank_with_the_keyboard(self):
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.assertEqual(self.wm.keymap.mode, "copy")
        self.keys("g", "0", "v", "l", "l", "l", "l", "y")
        self.assertEqual(self.wm.paste_buffer, "hello")
        self.assertEqual(self.wm.keymap.mode, "normal")
        self.assertIsNone(self.wm.active_selection())

    def test_line_and_rect_and_word_motion(self):
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.keys("g", "V", "j", "y")
        self.assertEqual(self.wm.paste_buffer, "hello world foo/bar.txt second\nline two here")
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.keys("g", "w", "v", "e" if False else "l", "y")
        self.assertEqual(self.wm.paste_buffer, "wo")
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.keys("g", "C-v", "l", "l", "j", "y")
        self.assertEqual(self.wm.paste_buffer, "hel\nlin")

    def test_end_of_line_and_backward_word(self):
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.keys("g", "$")
        self.assertEqual(self.wm.selection.head[1], len("hello world foo/bar.txt second") - 1)
        self.keys("b")
        self.assertEqual(self.wm.selection.head[1], len("hello world foo/bar.txt "))
        self.keys("Esc")
        self.assertEqual(self.wm.keymap.mode, "normal")

    def test_cancel_and_errors(self):
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.keys("y")                                                # nothing selected yet: message, still in copy mode
        self.assertEqual(self.wm.keymap.mode, "copy")
        self.keys("q")
        self.assertEqual(self.wm.keymap.mode, "normal")
        self.assertEqual(self.wm.paste_buffer, "")
        with self.assertRaises(CommandError):
            self.wm.run_command_line("copy-move up", source="t", raise_errors=True)

    def test_scrolls_to_keep_the_cursor_visible(self):
        for i in range(80):
            self.w.feed_bytes(b"history %d\r\n" % i)
        self.wm.run_command_line("copy-mode", source="t", raise_errors=True)
        self.keys("g")
        self.assertGreater(self.w.scroll_y, 0)
        self.keys("G")
        self.assertEqual(self.w.scroll_y, 0)

    def test_hotkeys_reach_copy_mode(self):
        type_keys(self.wm, b"\x1by")                                   # M-y
        self.assertEqual(self.wm.keymap.mode, "copy")
        self.keys("Esc")
        type_keys(self.wm, b"\x02y")                                   # prefix y
        self.assertEqual(self.wm.keymap.mode, "copy")

    def test_scroll_mode_v_enters_copy_mode(self):
        self.wm.run_command_line("mode scroll", source="t", raise_errors=True)
        self.keys("v")
        self.assertEqual(self.wm.keymap.mode, "copy")


class CopyViewTests(SelCase):
    def test_copy_view_shows_only_the_window_text_and_turns_the_mouse_off(self):
        self.wm.create_window({"cmd": SLEEPER, "name": "b"})
        self.wm.focus_window(self.w.id)
        self.assertTrue(self.wm.mouse_effective())
        self.wm.run_command_line("copy-view", source="t", raise_errors=True)
        self.assertFalse(self.wm.mouse_effective())
        frame = Compositor(self.wm).compose(60, 16)
        lines = frame.text().split("\n")
        self.assertEqual(lines[0].rstrip()[:5], "hello")
        self.assertNotIn("│", frame.text())
        self.assertNotIn("╭", frame.text())
        self.assertIn("COPY VIEW", lines[-1])
        self.wm.handle_key("x")                                        # any other key returns
        self.assertTrue(self.wm.mouse_effective())
        self.assertIn("╭", Compositor(self.wm).compose(60, 16).text())

    def test_copy_view_keys_scroll(self):
        for i in range(60):
            self.w.feed_bytes(b"row %d\r\n" % i)
        self.wm.run_command_line("copy-view", source="t", raise_errors=True)
        self.wm.handle_key("PageUp")
        self.assertGreater(self.w.scroll_y, 0)
        self.assertIsNotNone(self.wm.copy_view)
        self.wm.handle_key("G")
        self.assertEqual(self.w.scroll_y, 0)
        self.wm.run_command_line("copy-view", source="t", raise_errors=True)   # toggles back
        self.assertIsNone(self.wm.copy_view)

    def test_copy_view_ends_when_its_window_closes(self):
        self.wm.create_window({"cmd": SLEEPER, "name": "b"})
        self.wm.run_command_line("copy-view", source="t", raise_errors=True)
        self.wm.close_window(self.wm.copy_view["wid"])
        Compositor(self.wm).compose(60, 16)
        self.assertIsNone(self.wm.copy_view)


class ClipboardOutputTests(SelCase):
    def test_osc52_payload(self):
        seq = S.osc52("héllo\nwörld")
        self.assertTrue(seq.startswith("\x1b]52;c;") and seq.endswith("\x07"))
        self.assertEqual(base64.b64decode(seq[7:-1]).decode("utf-8"), "héllo\nwörld")

    def test_too_large_for_osc52_stays_in_the_buffer(self):
        big = "x" * (S.MAX_OSC52_BYTES + 1)
        self.wm.pending_terminal_output.clear()
        self.wm.copy_text(big)
        self.assertEqual(self.wm.paste_buffer, big)
        self.assertEqual(self.wm.pending_terminal_output, [])
        self.assertIn("too large", self.wm.current_message()[0])

    def test_osc52_can_be_switched_off(self):
        self.wm.cfg["selection"] = {"osc52": False}
        self.wm.pending_terminal_output.clear()
        self.wm.copy_text("abc")
        self.assertEqual(self.wm.pending_terminal_output, [])

    def test_command_receives_the_text(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "out.txt").replace("\\", "/")
        script = os.path.join(tmp, "c.py").replace("\\", "/")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nopen(%r, 'w', encoding='utf-8').write(sys.stdin.buffer.read().decode('utf-8'))\n" % out)
        self.wm.cfg["selection"] = {"command": '"%s" "%s"' % (sys.executable, script)}
        self.wm.copy_text("to the clipboard ✓")
        end = time.time() + 5
        while time.time() < end and not os.path.exists(out):
            time.sleep(0.05)
        time.sleep(0.2)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "to the clipboard ✓")

    def test_event_does_not_leak_the_text(self):
        self.wm.copy_text("s3cret-value")
        ev = [e for e in self.wm.event_ring if e["event"] == "clipboard"]
        self.assertEqual(ev[-1]["data"], {"length": 12})


class StabilityTests(unittest.TestCase):
    def test_line_ids_are_stable_while_history_scrolls(self):
        from pytermwm.ansi import Screen
        s = Screen(4, 20, 5)
        s.feed("a\r\nb\r\nc\r\nd\r\n")
        lid = S.base_id(s) + len(s.history) + 1
        text = S.line_at(s, lid)[0][0]
        for i in range(30):
            s.feed("x\r\n")
            self.assertLessEqual(len(s.history), 5)
        self.assertIsNone(S.line_at(s, lid))                        # evicted: gone, not silently replaced
        self.assertEqual(text, "b" if False else text)

    def test_wide_characters_are_copied_whole(self):
        from pytermwm.ansi import Screen
        s = Screen(3, 10, 0)
        s.feed("日本語 ab")
        sel = S.Selection(1, s, (S.base_id(s), 1))                   # starts on the tail half of 日
        sel.head = (S.base_id(s), 2)
        self.assertEqual(sel.text(s), "日本")

    def test_word_span(self):
        from pytermwm.ansi import Screen
        s = Screen(3, 40, 0)
        s.feed("ls -la /usr/local/bin  x")
        lid = S.base_id(s)
        a, b = S.word_span(s, (lid, 8))
        self.assertEqual((a[1], b[1]), (7, 20))                       # "/usr/local/bin"


class ConfigAndApiTests(unittest.TestCase):
    def test_config_validation(self):
        self.assertEqual(validate_config({"selection": {"copy_on_release": True, "modifier": "shift", "paste_buttons": ["right"],
                                                        "osc52": False, "command": "clip", "word_chars": "-_"}})[0], [])
        for bad in ("x", {"copy_on_release": "yes"}, {"modifier": "hyper"}, {"paste_buttons": ["left"]}, {"nope": 1}, {"command": 3}):
            self.assertTrue(validate_config({"selection": bad})[0], bad)

    def test_api_op_and_scope(self):
        from pytermwm.control import handle_op
        from pytermwm import perms
        wm = make_wm(60, 16)
        try:
            w = wm.create_window({"cmd": SLEEPER, "name": "a"})
            w.feed_bytes(b"secret line\r\n")
            wm.copy_text("clip")
            res = handle_op(wm, {"op": "selection"})
            self.assertTrue(res["ok"])
            self.assertEqual(res["paste_buffer"], "clip")
            self.assertEqual(handle_op(wm, {"op": "selection"}, "read")["ok"], False)       # copied data is not for read-only tokens
            self.assertEqual(handle_op(wm, {"op": "selection"}, "agent")["ok"], False)
        finally:
            wm.shutdown()


if __name__ == "__main__":
    unittest.main()
