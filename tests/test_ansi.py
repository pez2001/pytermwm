import unittest
from tests.helpers import *
from pytermwm.ansi import Screen, char_width
from pytermwm.colors import BOLD, UNDERLINE, REVERSE, ITALIC, WIDE, TAIL


class AnsiTests(unittest.TestCase):
    def scr(self, rows=5, cols=20, hist=100):
        return Screen(rows, cols, hist)

    def test_plain_text_and_newlines(self):
        s = self.scr()
        s.feed("hello\r\nworld")
        self.assertEqual(s.row_text(0), "hello")
        self.assertEqual(s.row_text(1), "world")
        self.assertEqual((s.x, s.y), (5, 1))

    def test_sgr_16_and_bold(self):
        s = self.scr()
        s.feed("\x1b[1;31mA\x1b[0mB")
        a, b = s.lines[0][0], s.lines[0][1]
        self.assertEqual(a[1], 1)
        self.assertTrue(a[3] & BOLD)
        self.assertIsNone(b[1])
        self.assertEqual(b[3], 0)

    def test_sgr_256_and_truecolor(self):
        s = self.scr()
        s.feed("\x1b[38;5;208mA\x1b[48;2;1;2;3mB\x1b[38:2::9:8:7mC")
        self.assertEqual(s.lines[0][0][1], 208)
        self.assertEqual(s.lines[0][1][2], (1, 2, 3))
        self.assertEqual(s.lines[0][2][1], (9, 8, 7))

    def test_bright_colors(self):
        s = self.scr()
        s.feed("\x1b[91;104mX")
        self.assertEqual(s.lines[0][0][1], 9)
        self.assertEqual(s.lines[0][0][2], 12)

    def test_cursor_movement_and_erase(self):
        s = self.scr()
        s.feed("abcdef\x1b[3D\x1b[K")
        self.assertEqual(s.row_text(0), "abc")
        s.feed("\x1b[2;5Hx\x1b[1;1H\x1b[2J")
        self.assertEqual(s.text(), "")
        s.feed("\x1b[3;4Hz")
        self.assertEqual(s.lines[2][3][0], "z")

    def test_scroll_and_history(self):
        s = self.scr(3, 10, 50)
        for i in range(6):
            s.feed("line%d\r\n" % i)
        self.assertEqual(len(s.history), 4)
        self.assertEqual(Screen.line_text(s.history[0]), "line0")
        view = s.view(3, 3)
        self.assertEqual(Screen.line_text(view[-1]), "line3")
        self.assertEqual(Screen.line_text(view[0]), "line1")

    def test_history_limit(self):
        s = self.scr(2, 10, 3)
        for i in range(20):
            s.feed("%d\r\n" % i)
        self.assertEqual(len(s.history), 3)

    def test_scroll_region(self):
        s = self.scr(5, 10)
        s.feed("a\r\nb\r\nc\r\nd\r\ne")
        s.feed("\x1b[2;4r\x1b[4;1H\n")
        self.assertEqual([s.row_text(i) for i in range(5)], ["a", "c", "d", "", "e"])

    def test_insert_delete_lines_and_chars(self):
        s = self.scr(4, 10)
        s.feed("1\r\n2\r\n3\r\n4\x1b[2;1H\x1b[L")
        self.assertEqual([s.row_text(i) for i in range(4)], ["1", "", "2", "3"])
        s.feed("\x1b[M")
        self.assertEqual([s.row_text(i) for i in range(4)], ["1", "2", "3", ""])
        s.feed("\x1b[1;1Habcdef\x1b[1;3H\x1b[2P")
        self.assertEqual(s.row_text(0), "abef")
        s.feed("\x1b[1;2H\x1b[2@")
        self.assertEqual(s.row_text(0), "a  bef")

    def test_alt_screen_restores(self):
        s = self.scr()
        s.feed("main")
        s.feed("\x1b[?1049h\x1b[Halt")
        self.assertEqual(s.row_text(0), "alt")
        self.assertTrue(s.alt_active)
        s.feed("\x1b[?1049l")
        self.assertEqual(s.row_text(0), "main")
        self.assertEqual(len(s.history), 0)

    def test_wide_chars(self):
        s = self.scr(2, 6)
        s.feed("日本")
        self.assertTrue(s.lines[0][0][3] & WIDE)
        self.assertTrue(s.lines[0][1][3] & TAIL)
        self.assertEqual(s.row_text(0), "日本")
        self.assertEqual(s.x, 4)
        s.feed("\x1b[1;2Hx")     # overwrite the tail of a wide char
        self.assertEqual(s.lines[0][0][0], " ")

    def test_wide_char_wraps(self):
        s = self.scr(2, 3)
        s.feed("ab日")
        self.assertEqual(s.row_text(0), "ab")
        self.assertEqual(s.row_text(1), "日")

    def test_combining_chars(self):
        s = self.scr()
        s.feed("éx")
        self.assertEqual(s.lines[0][0][0], "é")
        self.assertEqual(s.lines[0][1][0], "x")

    def test_autowrap_and_deferred_wrap(self):
        s = self.scr(3, 4)
        s.feed("abcd")
        self.assertEqual((s.x, s.y), (3, 0))
        self.assertTrue(s.wrap_pending)
        s.feed("e")
        self.assertEqual(s.row_text(1), "e")

    def test_no_autowrap(self):
        s = self.scr(2, 4)
        s.feed("\x1b[?7labcdef")
        self.assertEqual(s.row_text(0), "abcf")

    def test_tabs(self):
        s = self.scr(2, 20)
        s.feed("a\tb")
        self.assertEqual(s.lines[0][8][0], "b")

    def test_dec_line_drawing(self):
        s = self.scr()
        s.feed("\x1b(0lqk\x1b(B")
        self.assertEqual(s.row_text(0), "┌─┐")

    def test_osc_title(self):
        s = self.scr()
        got = []
        s.on_title = got.append
        s.feed("\x1b]0;my title\x07x\x1b]2;other\x1b\\")
        self.assertEqual(got, ["my title", "other"])
        self.assertEqual(s.row_text(0), "x")

    def test_dsr_and_da_responses(self):
        s = self.scr()
        s.feed("\x1b[2;3H\x1b[6n\x1b[c")
        r = s.take_responses()
        self.assertIn("\x1b[2;3R", r)
        self.assertIn("\x1b[?", r)

    def test_save_restore_cursor(self):
        s = self.scr()
        s.feed("\x1b[2;3H\x1b7\x1b[1;1H\x1b8x")
        self.assertEqual(s.lines[1][2][0], "x")

    def test_resize_keeps_content(self):
        s = self.scr(4, 10)
        s.feed("a\r\nb\r\nc")
        s.resize(2, 6)
        self.assertEqual(s.row_text(1), "c")
        self.assertEqual(Screen.line_text(s.history[-1]), "a")
        s.resize(5, 12)
        self.assertEqual(s.rows, 5)
        self.assertEqual(s.row_text(0), "a")      # history is pulled back when growing

    def test_erase_uses_background(self):
        s = self.scr()
        s.feed("\x1b[44m\x1b[K")
        self.assertEqual(s.lines[0][5][2], 4)

    def test_partial_escape_across_feeds(self):
        s = self.scr()
        s.feed("\x1b[3")
        s.feed("1mA")
        self.assertEqual(s.lines[0][0][1], 1)

    def test_reverse_index(self):
        s = self.scr(3, 5)
        s.feed("a\r\nb\x1b[1;1H\x1bMx")
        self.assertEqual(s.row_text(0), "x")
        self.assertEqual(s.row_text(1), "a")

    def test_mouse_and_paste_modes(self):
        s = self.scr()
        s.feed("\x1b[?1002h\x1b[?1006h\x1b[?2004h")
        self.assertEqual(s.mouse_mode, 1002)
        self.assertTrue(s.mouse_sgr)
        self.assertTrue(s.bracketed_paste)

    def test_resize_request_callback(self):
        s = self.scr()
        got = []
        s.on_resize_request = lambda r, c: got.append((r, c))
        s.feed("\x1b[8;40;120t")
        self.assertEqual(got, [(40, 120)])

    def test_on_wrap_fires_only_on_the_real_wrap_not_the_pending_one(self):
        # Writing the last column only sets wrap_pending; the row doesn't actually advance (and
        # on_wrap must not fire) until the next printable character forces it.
        s = self.scr(3, 4)
        got = []
        s.on_wrap = lambda: got.append(1)
        s.feed("abcd")
        self.assertEqual(got, [])
        s.feed("e")
        self.assertEqual(got, [1])
        self.assertEqual(s.row_text(1), "e")

    def test_on_wrap_fires_for_a_forced_wide_char_wrap(self):
        s = self.scr(3, 4)
        got = []
        s.on_wrap = lambda: got.append(1)
        s.feed("abc\u65e5")           # wide char can't split across columns 3/4 of a 4-wide screen
        self.assertEqual(got, [1])
        self.assertEqual(s.row_text(1), "\u65e5")

    def test_on_wrap_not_called_when_autowrap_is_off(self):
        s = self.scr(2, 4)
        got = []
        s.on_wrap = lambda: got.append(1)
        s.feed("\x1b[?7labcdef")
        self.assertEqual(got, [])

    def test_on_unknown_csi_callback(self):
        s = self.scr()
        got = []
        s.on_unknown_csi = lambda final, params: got.append((final, params))
        s.feed("\x1b[5y")              # DECRQCRA-style final byte pytermwm doesn't implement
        self.assertEqual(got, [("y", [5])])

    def test_on_unknown_csi_not_called_for_handled_sequences(self):
        s = self.scr()
        got = []
        s.on_unknown_csi = lambda final, params: got.append((final, params))
        s.feed("\x1b[1;31mA\x1b[2J\x1b[3C")
        self.assertEqual(got, [])

    def test_char_width(self):
        self.assertEqual(char_width("a"), 1)
        self.assertEqual(char_width("日"), 2)
        self.assertEqual(char_width("́"), 0)


if __name__ == "__main__":
    unittest.main()
