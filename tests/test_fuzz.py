"""Robustness tests for the terminal emulator: no input may crash it or break its invariants.

The streams are pseudo random but seeded, so a failure is reproducible (the seed is in the message)."""
import random
import unittest

from tests.helpers import *
from pytermwm.ansi import Screen
from pytermwm.colors import TAIL

CSI_FINALS = "@ABCDEFGHIJKLMPSTXZ`abcdefghlmnpqrstuvwxyz{|}~"
ESC_FINALS = "78=>DEHMNOPZ\\c()*+-.#%_^ "
PRIVATE = ["", "?", ">", "!", "=", " "]
TEXTS = ["a", "hello", "\r\n", "\n", "\t", "\b", "é", "日本語", "😀", "é", "‍", "\x07", "\x00", "\x7f", "\x0e", "\x0f",
         "\x1b", "\x9b", "�", "x" * 300]


def random_csi(rng):
    n = rng.randint(0, 5)
    params = ";".join(str(rng.choice([0, 1, 2, 3, 5, 7, 24, 25, 38, 48, 255, 256, 1000, 65535, 99999999, ""])) for _ in range(n))
    if rng.random() < 0.1:
        params = params.replace(";", ":")
    return "\x1b[" + rng.choice(PRIVATE) + params + rng.choice(CSI_FINALS)


def random_osc(rng):
    body = rng.choice(["0;title", "2;", "7;file:///tmp/x", "8;;http://x", "52;c;aGVsbG8=", "4;1;rgb:ff/00/00", "104", "133;A", "777;notify;a;b",
                       "999;" + "z" * 500, ""])
    return "\x1b]" + body + rng.choice(["\x07", "\x1b\\", "", "\x18"])


def random_stream(rng, n):
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.45:
            out.append(rng.choice(TEXTS))
        elif r < 0.75:
            out.append(random_csi(rng))
        elif r < 0.85:
            out.append(random_osc(rng))
        elif r < 0.92:
            out.append("\x1b" + rng.choice(ESC_FINALS))
        elif r < 0.95:
            out.append("\x1bP" + "".join(chr(rng.randint(32, 126)) for _ in range(rng.randint(0, 20))) + rng.choice(["\x1b\\", ""]))
        else:
            out.append("".join(chr(rng.randint(0, 0xFFFF)) for _ in range(rng.randint(1, 6))).encode("utf-8", "replace").decode("utf-8", "replace"))
    return "".join(out)


def check_invariants(t: unittest.TestCase, s: Screen, seed):
    msg = "seed %r" % (seed,)
    t.assertEqual(len(s.lines), s.rows, msg)
    for row in s.lines:
        t.assertEqual(len(row), s.cols, msg)
    t.assertTrue(0 <= s.x < s.cols, "%s x=%d cols=%d" % (msg, s.x, s.cols))
    t.assertTrue(0 <= s.y < s.rows, "%s y=%d rows=%d" % (msg, s.y, s.rows))
    t.assertTrue(0 <= s.top <= s.bottom < s.rows, "%s region %d..%d" % (msg, s.top, s.bottom))
    for row in s.lines:
        for i, c in enumerate(row):
            t.assertEqual(len(c), 4, msg)
            if c[3] & TAIL:
                t.assertTrue(i > 0, "%s tail cell in column 0" % msg)
    t.assertLessEqual(len(s.history), max(s.history_limit, 0), msg)


class FuzzTests(unittest.TestCase):
    def test_random_streams_keep_invariants(self):
        for seed in range(300):
            rng = random.Random(seed)
            s = Screen(rng.randint(1, 30), rng.randint(1, 100), rng.choice([0, 5, 200]))
            for _ in range(rng.randint(1, 8)):
                s.feed(random_stream(rng, rng.randint(1, 120)))
                check_invariants(self, s, seed)
                s.take_responses()
                if rng.random() < 0.3:
                    s.resize(rng.randint(1, 40), rng.randint(1, 120))
                    check_invariants(self, s, seed)
            s.text(history=True)
            s.view(s.rows, 0)

    def test_byte_at_a_time_equals_one_shot(self):
        for seed in range(15):
            rng = random.Random(1000 + seed)
            data = random_stream(rng, 80)
            a, b = Screen(10, 40, 50), Screen(10, 40, 50)
            a.feed(data)
            for ch in data:
                b.feed(ch)
            self.assertEqual(a.text(history=True), b.text(history=True), "seed %d" % seed)
            self.assertEqual((a.x, a.y), (b.x, b.y), "seed %d" % seed)

    def test_arbitrary_random_text(self):
        rng = random.Random(7)
        s = Screen(12, 50, 20)
        for i in range(200):
            chunk = "".join(chr(rng.choice([rng.randint(0, 127), rng.randint(128, 0x2FFF), rng.randint(0x1F300, 0x1F64F)]))
                            for _ in range(rng.randint(1, 60)))
            s.feed(chunk)
        check_invariants(self, s, "text")

    def test_huge_parameters_are_bounded(self):
        s = Screen(24, 80, 10)
        for seq in ("\x1b[99999999999999999999A", "\x1b[99999999999999999999;99999999999999999999H", "\x1b[999999999L",
                    "\x1b[999999999@", "\x1b[999999999X", "\x1b[999999999S", "\x1b[999999999T", "\x1b[999999999b",
                    "\x1b[8;999999;999999t", "\x1b[999999999P", "\x1b[999999999M", "\x1b[1;99999999r"):
            s.feed("x" + seq + "y")
            check_invariants(self, s, seq)

    def test_very_long_unterminated_sequences(self):
        s = Screen(24, 80, 10)
        for start in ("\x1b]0;", "\x1bP", "\x1b[", "\x1b_", "\x1b^"):
            s.feed(start + "a" * 200000)
            s.feed("\x18")                      # CAN cancels any pending sequence
            s.feed("ok")
            check_invariants(self, s, start)
        self.assertIn("ok", s.text())

    def test_alt_screen_and_resize_churn(self):
        rng = random.Random(3)
        s = Screen(24, 80, 100)
        for i in range(300):
            s.feed(rng.choice(["\x1b[?1049h", "\x1b[?1049l", "\x1b[?47h", "\x1b[?47l", "\x1b7", "\x1b8", "hello\r\n" * 5,
                               "\x1b[2J", "\x1b[3J", "\x1bc"]))
            if i % 7 == 0:
                s.resize(rng.randint(1, 50), rng.randint(1, 150))
            check_invariants(self, s, i)

    def test_responses_stay_small(self):
        s = Screen(24, 80, 0)
        s.feed("\x1b[6n" * 1000 + "\x1b[c" * 1000 + "\x1b[?u" * 100)
        self.assertLess(len(s.take_responses()), 200000)


if __name__ == "__main__":
    unittest.main()
