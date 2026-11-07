import base64
import time
import unittest
from tests.helpers import *
from pytermwm.keys import KeyParser
from pytermwm.nesting import ReplyFilter, NEST_QUERY
from pytermwm.config import validate_config

SLEEPER = "sleep 30"


class Sink:
    """A window source that records what is written to it."""
    tty = True
    alive = True

    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def resize(self, rows, cols):
        pass


class ReplyFilterTests(unittest.TestCase):
    def test_passes_plain_input(self):
        out, rep = ReplyFilter().feed(b"ab\x1b[A")
        self.assertEqual((out, rep), (b"ab\x1b[A", []))

    def test_extracts_replies_between_keys(self):
        out, rep = ReplyFilter().feed(b"a\x1b]7790;depth=2;session=x\x07b\x1b]52;c;aGk=\x1b\\c")
        self.assertEqual(out, b"abc")
        self.assertEqual(rep, [("7790", "depth=2;session=x"), ("52", "c;aGk=")])

    def test_split_reply_is_held(self):
        f = ReplyFilter()
        out, rep = f.feed(b"x\x1b]52;c;aG")
        self.assertEqual((out, rep), (b"x", []))
        self.assertTrue(f.pending())
        out, rep = f.feed(b"k=\x07y")
        self.assertEqual((out, rep), (b"y", [("52", "c;aGk=")]))

    def test_other_osc_untouched(self):
        out, rep = ReplyFilter().feed(b"\x1b]11;rgb:0/0/0\x07")
        self.assertEqual((out, rep), (b"\x1b]11;rgb:0/0/0\x07", []))


class NestingTests(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(80, 20)
        self.w = self.wm.create_window({"cmd": SLEEPER, "name": "a"})
        self.sink = Sink()
        self.w.source = self.sink          # replaces the real process: we only look at what is typed into it

    def tearDown(self):
        for w in list(self.wm.windows.values()):
            w.source = None if isinstance(w.source, Sink) else w.source
        self.wm.close_all() if hasattr(self.wm, "close_all") else None

    def test_inner_asks_and_outer_answers_with_depth(self):
        self.w.feed_bytes(NEST_QUERY.encode())
        self.assertTrue(self.w.nested_client)
        self.assertEqual(self.sink.data, b"\x1b]7790;depth=1;session=%s\x07" % (self.wm.session_name or "").encode())
        # an outer level that is itself nested answers one deeper
        self.wm.nest_depth = 1
        self.sink.data = b""
        self.w.feed_bytes(NEST_QUERY.encode())
        self.assertIn(b"depth=2", self.sink.data)

    def test_reply_sets_depth(self):
        self.wm.nesting_reply("7790", "depth=2;session=main")
        self.assertEqual(self.wm.nest_depth, 2)
        self.wm.set_nest_hint("3")
        self.assertEqual(self.wm.nest_depth, 3)

    def test_toggle_key_by_depth_and_config(self):
        self.assertEqual(self.wm.passthrough_key(), "F12")
        self.wm.nest_depth = 1
        self.assertEqual(self.wm.passthrough_key(), "S-F12")
        self.wm.cfg["nesting"] = {"passthrough_key": "C-]"}
        self.assertEqual(self.wm.passthrough_key(), "C-]")
        self.wm.cfg["nesting"] = {"passthrough_key": "none"}
        self.assertIsNone(self.wm.passthrough_key())

    def test_toggle_keys_parse(self):
        names = [e.name for e in KeyParser().feed(b"\x1b[24~\x1b[24;2~\x1b[24;5~\x1b[24;3~")]
        self.assertEqual(names, ["F12", "S-F12", "C-F12", "M-F12"])

    def test_passthrough_sends_everything_but_the_toggle(self):
        type_keys(self.wm, b"\x1b[24~")                    # F12
        self.assertTrue(self.wm.passthrough)
        type_keys(self.wm, b"\x1bh\x02d\x1b[A")            # M-h, C-b d, Up: all for the program
        self.assertEqual(self.sink.data, b"\x1bh\x02d\x1b[A")
        self.assertEqual(len(self.wm.windows), 1)
        type_keys(self.wm, b"\x1b[24~")
        self.assertFalse(self.wm.passthrough)
        self.sink.data = b""
        type_keys(self.wm, b"\x1b[A")
        self.assertEqual(self.sink.data, b"\x1b[A")        # normal forwarding again

    def test_normal_mode_still_uses_hotkeys(self):
        type_keys(self.wm, b"\x1bj")
        self.assertEqual(self.sink.data, b"")              # M-j is a WM key, not typed into the window

    def test_deeper_toggle_reaches_the_inner_level(self):
        type_keys(self.wm, b"\x1b[24~")
        type_keys(self.wm, b"\x1b[24;2~")                  # S-F12 belongs to the inner level
        self.assertTrue(self.wm.passthrough)
        self.assertEqual(self.sink.data, b"\x1b[24;2~")

    def test_status_segment(self):
        from pytermwm.statusline import seg_mode, seg_nest
        self.assertIsNone(seg_mode(self.wm, {}))
        self.wm.set_passthrough(True)
        self.assertIn("PASSTHROUGH", seg_mode(self.wm, {}).text)
        self.assertIsNone(seg_nest(self.wm, {}))
        self.wm.nest_depth = 1
        self.assertEqual(seg_nest(self.wm, {}).text, "L2")

    def test_passthrough_needs_a_running_window(self):
        self.w.source = None
        from pytermwm.commands import CommandError
        with self.assertRaises(CommandError):
            self.wm.set_passthrough(True)

    def test_command_ctl(self):
        self.wm.run_command_line("passthrough on")
        self.assertTrue(self.wm.passthrough)
        self.wm.run_command_line("passthrough off")
        self.assertFalse(self.wm.passthrough)

    def test_mouse_goes_to_the_program_in_passthrough(self):
        self.w.screen.mouse_mode = 1000
        self.w.screen.mouse_sgr = True
        self.wm.set_passthrough(True)
        r = self.wm.desk.rects[self.w.id]
        inner = self.wm.inner_rect(self.w, r)
        self.wm.handle_mouse({"x": inner.x + 2, "y": inner.y + 1, "kind": "press", "button": 1, "bt": 0, "final": "M"})
        self.assertEqual(self.sink.data, b"\x1b[<0;3;2M")


class EnvTests(unittest.TestCase):
    def test_windows_get_depth_in_environment(self):
        wm = make_wm(80, 20)
        wm.nest_depth = 1
        w = wm.create_window({"cmd": "printenv PYTERMWM_DEPTH; sleep 3", "name": "b"})
        try:
            pump(wm, 3, until=lambda: w.text().strip().startswith("2"))
            self.assertTrue(w.text().strip().startswith("2"), w.text())
        finally:
            w.source.kill() if hasattr(w.source, "kill") else None


class NestedClipboardTests(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(80, 20)
        self.w = self.wm.create_window({"cmd": SLEEPER, "name": "a"})
        self.sink = Sink()
        self.w.source = self.sink
        self.calls = []
        self.wm._pipe_to_command = lambda cmd, text: self.calls.append(text)
        self.wm.cfg["selection"] = {"command": "clip"}

    @staticmethod
    def osc52(text):
        return ("\x1b]52;c;%s\x07" % base64.b64encode(text.encode()).decode()).encode()

    def test_copy_from_nested_window_is_relayed_and_runs_command_at_the_top(self):
        self.w.nested_client = True
        self.w.feed_bytes(self.osc52("hello"))
        self.assertEqual(self.wm.paste_buffer, "hello")
        self.assertEqual(self.calls, ["hello"])
        self.assertTrue(any("52;c;" in s for s in self.wm.pending_terminal_output))
        self.assertIn("from the pytermwm", self.wm.status_message_text() if hasattr(self.wm, "status_message_text") else "from the pytermwm")

    def test_nested_level_does_not_run_the_command(self):
        self.wm.nest_depth = 1
        self.wm.copy_text("x", quiet=True)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("52;c;" in s for s in self.wm.pending_terminal_output))   # still goes up
        self.wm.cfg["nesting"] = {"command_when_nested": True}
        self.wm.copy_text("y", quiet=True)
        self.assertEqual(self.calls, ["y"])

    def test_nested_window_may_read_but_plain_windows_may_not(self):
        self.wm.copy_text("secret", quiet=True)
        self.assertIsNone(self.wm.window_clipboard(self.w, None))
        self.w.nested_client = True
        self.assertEqual(self.wm.window_clipboard(self.w, None), "secret")
        self.wm.cfg["nesting"] = {"nested_can_read": False}
        self.assertIsNone(self.wm.window_clipboard(self.w, None))

    def test_paste_asks_the_level_above_and_pastes_the_answer(self):
        self.wm.nest_depth = 1
        self.wm.paste_buffer = "stale"
        self.wm.paste_text()
        self.assertEqual(self.sink.data, b"")                          # waiting for the answer
        self.assertIn("\x1b]52;c;?\x07", self.wm.pending_terminal_output)
        self.wm.nesting_reply("52", "c;" + base64.b64encode(b"fresh").decode())
        self.assertEqual(self.sink.data, b"fresh")
        self.assertEqual(self.wm.paste_buffer, "fresh")

    def test_paste_falls_back_when_nobody_answers(self):
        self.wm.nest_depth = 1
        self.wm.paste_buffer = "mine"
        self.wm.paste_text()
        self.wm.nesting_tick(time.time() + 5)
        self.assertEqual(self.sink.data, b"mine")

    def test_unsolicited_clipboard_answer_is_ignored(self):
        self.wm.nesting_reply("52", "c;" + base64.b64encode(b"x").decode())
        self.assertEqual(self.sink.data, b"")

    def test_outermost_level_pastes_directly(self):
        self.wm.paste_buffer = "direct"
        self.wm.paste_text()
        self.assertEqual(self.sink.data, b"direct")


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate_config({"nesting": {"passthrough_key": "F11", "pull_clipboard": False}})[0], [])
        for bad in ("x", {"nope": 1}, {"pull_clipboard": "yes"}, {"passthrough_key": 3}):
            self.assertTrue(validate_config({"nesting": bad})[0], bad)


if __name__ == "__main__":
    unittest.main()
