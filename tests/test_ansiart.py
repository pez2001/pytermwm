import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.helpers import *
from pytermwm import config as C
from pytermwm.ansi import Screen
from pytermwm.colors import DIM
from pytermwm.contrib import ansiart as A
from pytermwm.contrib.effects import AnsiBackground, EFFECTS, NAMES

ESC = b"\x1b"


class _CapturedLog:
    """Collects log messages from `name` without needing assertLogs (which requires at least one
    record and is only on Python 3.4+ anyway, but this also makes "assert nothing was logged"
    straightforward across versions)."""

    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.records = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(record.getMessage())

    def __enter__(self):
        self._old_level = self.logger.level
        self.logger.addHandler(self._handler)
        self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self._old_level)

    @property
    def text(self):
        return "\n".join(self.records)


def sauce(width=80, ice=False, title="art"):
    rec = b"SAUCE00" + title.encode().ljust(35) + b" " * 20 + b" " * 20 + b"20260101" + (0).to_bytes(4, "little")
    rec += bytes([1, 1]) + width.to_bytes(2, "little") + (25).to_bytes(2, "little") + b"\x00" * 4 + b"\x00" + bytes([1 if ice else 0]) + b"\x00" * 22
    assert len(rec) == 128, len(rec)
    return b"\x1a" + rec


def text_of(rows):
    return ["".join(c[0] for c in r).rstrip() for r in rows]


class LoaderTests(unittest.TestCase):
    def test_cp437_colours_and_blocks(self):
        art = A.art_from_bytes(ESC + b"[1;31m\xdb\xb2" + ESC + b"[0;44m\xdc" + ESC + b"[0m\r\nsecond", "auto")
        self.assertEqual(text_of(art.rows), ["█▓▄", "second"])
        a, b, c = art.rows[0][:3]
        self.assertEqual((a[1], a[2]), (9, 0))                    # bold red -> bright red on black
        self.assertEqual((c[1], c[2]), (7, 4))                    # default grey on blue
        self.assertEqual(art.rows[1][0][:3], ("s", 7, 0))

    def test_control_code_pictures_and_eof(self):
        art = A.art_from_bytes(b"\x01\x02\x03 \x0e\x0f\x1a hidden after EOF", "cp437")
        self.assertEqual(text_of(art.rows), ["☺☻♥ ♫☼"])

    def test_sauce_is_removed_and_gives_width_and_ice(self):
        art = A.art_from_bytes(b"x" * 10 + sauce(width=132, ice=True, title="My art"), "auto")
        self.assertEqual(art.title, "My art")
        self.assertTrue(art.ice)
        self.assertEqual(art.width, 10)                            # plain text: the longest line wins
        art = A.art_from_bytes(ESC + b"[31m" + b"x" * 200 + sauce(width=132), "auto")
        self.assertEqual((art.width, art.height), (132, 2))        # art with escapes wraps at the SAUCE width
        art = A.art_from_bytes(ESC + b"[31m" + b"x" * 200, "auto")
        self.assertEqual((art.width, art.height), (80, 3))         # ... or at 80 columns
        art = A.art_from_bytes(ESC + b"[31mx" + b" " * 20 + b"y", "auto")
        self.assertEqual(art.width, 22)                            # empty columns on the right are cut
        art = A.art_from_bytes(ESC + b"[31mx" + b" " * 20 + b"y" + sauce(width=132), "auto")
        self.assertEqual(art.width, 22)

    def test_ice_turns_blink_into_bright_background(self):
        data = ESC + b"[5;42m \r\n"
        self.assertEqual(A.art_from_bytes(data, "auto", ice=True).rows[0][0][2], 10)
        self.assertTrue(A.art_from_bytes(data, "auto", ice=False).rows[0][0][3] & 16)

    def test_plain_ascii_and_utf8(self):
        art = A.art_from_bytes(b"ab\r\n  cdef\r\n\r\n\r\n", "auto")
        self.assertEqual((art.width, art.height), (6, 2))          # trailing blank lines are dropped
        art = A.art_from_bytes("日本 ok".encode("utf-8"), "auto")
        self.assertEqual(art.width, 7)
        self.assertEqual(A.clip_row(art.rows[0], 1, 4)[0][0], " ")  # never half a wide character
        self.assertEqual(A.clip_row(art.rows[0], 0, 1)[0][0], " ")

    def test_cursor_movement_and_tall_art(self):
        art = A.art_from_bytes(ESC + b"[5;10Hx" + b"\n" * 60 + b"end", "auto")
        self.assertEqual(art.rows[4][9][0], "x")
        self.assertEqual(art.rows[-1][0][0], "e")
        self.assertGreater(art.height, 60)


class BackgroundCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def first_text(self, cells):
        return ["".join(c[0] for c in r).rstrip() for r in cells]


class RenderTests(BackgroundCase):
    def test_small_art_is_centered_or_top_left_and_static(self):
        p = self.write("small.asc", "ab\ncd")
        e = AnsiBackground(path=p)
        f1 = e.render(10, 6, 100.0)
        self.assertEqual(self.first_text(f1), ["", "", "    ab", "    cd", "", ""])
        self.assertEqual(e.render(10, 6, 500.0), f1)              # never moves
        top = AnsiBackground(path=p, align="top")
        self.assertEqual(self.first_text(top.render(10, 6, 1.0))[:2], ["ab", "cd"])

    def test_tall_art_scrolls_down_then_back_up(self):
        p = self.write("tall.asc", "\n".join("line%02d" % i for i in range(40)))
        e = AnsiBackground(path=p, scroll=10, pause=1, hold=1)
        seen = []
        t = 100.0
        for _ in range(140):
            t += 0.25
            seen.append(self.first_text(e.render(20, 10, t))[0].strip())
        self.assertEqual(seen[0], "line00")
        first = [int(s[4:]) for s in seen if s]
        down = first.index(max(first))
        self.assertEqual(max(first), 30)                         # bottom reached: 40 lines, 10 visible
        self.assertTrue(all(b >= a for a, b in zip(first[:down], first[1:down + 1])))
        self.assertTrue(any(b < a for a, b in zip(first, first[1:])))

    def test_wide_art_scrolls_sideways(self):
        p = self.write("wide.asc", "0123456789" * 8 + "\nsecond")
        e = AnsiBackground(path=p, scroll=5, pause=0.5, hold=1)
        xs = set()
        t = 10.0
        for _ in range(100):
            t += 0.2
            row0 = self.first_text(e.render(30, 1, t))[0]
            xs.add(row0[:3])
        self.assertGreater(len(xs), 5)

    def test_directory_cycles_files_and_rescans(self):
        self.write("b.txt", "BBB")
        self.write("a.ans", ESC + b"[31mAAA")
        self.write("sub/c.asc", "CCC")
        self.write("notes.md", "not art")
        e = AnsiBackground(path=self.dir, hold=5, scroll=5, align="top")
        self.assertEqual([os.path.basename(f) for f in e.files], ["a.ans", "b.txt", "c.asc"])
        seen = []
        t = 0.0
        for _ in range(80):
            t += 0.5                                              # (an effect never advances more than half a second per frame)
            txt = self.first_text(e.render(100, 3, t))[0]
            if not seen or seen[-1] != txt:
                seen.append(txt)
        self.assertEqual(seen[:4], ["AAA", "BBB", "CCC", "AAA"])
        self.write("d.txt", "DDD")
        for _ in range(120):
            t += 0.5
            txt = self.first_text(e.render(100, 3, t))[0]
            if txt not in seen:
                seen.append(txt)
        self.assertIn("DDD", seen)                                # new files are picked up at the end of a round

    def test_random_order_and_dim_and_unreadable_files(self):
        for i in range(6):
            self.write("f%d.txt" % i, "F%d" % i)
        e = AnsiBackground(path=self.dir, order="random", dim=True, align="top")
        self.assertEqual(sorted(os.path.basename(f) for f in e.files), ["f%d.txt" % i for i in range(6)])
        cells = e.render(10, 3, 1.0)
        self.assertTrue(cells[0][0][3] & DIM)
        big = self.write("zzz.txt", b"x" * (A.MAX_BYTES + 10))
        os.remove(os.path.join(self.dir, "f0.txt"))               # vanished
        e2 = AnsiBackground(path=self.dir, hold=1, align="top")
        t = 0.0
        for _ in range(60):
            t += 0.5
            e2.render(10, 3, t)
        self.assertNotIn(big, e2.files)

    def test_sample_and_errors(self):
        e = AnsiBackground()
        cells = e.render(60, 12, 1.0)
        self.assertTrue(any("pytermwm" in t for t in self.first_text(cells)))
        for bad in (dict(path=os.path.join(self.dir, "missing")), dict(path=self.dir), dict(hold=0), dict(order="x"), dict(align="x"), dict(scroll=0)):
            with self.assertRaises(ValueError, msg=bad):
                AnsiBackground(**bad)
        self.assertIn("ansi", NAMES)
        self.assertIn("ansi", EFFECTS)

    def test_frame_is_reused_until_something_moves(self):
        p = self.write("x.asc", "hello")
        e = AnsiBackground(path=p)
        self.assertIs(e.render(20, 5, 1.0), e.render(20, 5, 1.1))
        self.assertIsNot(e.render(20, 5, 1.2), e.render(21, 5, 1.3))


class CommandTests(BackgroundCase):
    def setUp(self):
        super().setUp()
        self.wm = make_wm(80, 24)
        self.addCleanup(self.wm.shutdown)

    def test_effect_ansi_command_options_and_errors(self):
        p = self.write("a.asc", "hello")
        r = self.wm.execute("effect ansi %s hold=30 scroll=8 order=random dim=yes align=top" % p)
        self.assertTrue(r["ok"], r)
        b = self.wm.background
        self.assertEqual((b.name, b.hold, b.scroll, b.order, b.dim, b.align), ("ansi", 30.0, 8.0, "random", True, "top"))
        for bad in ("effect ansi /no/such/file", "effect ansi %s hold=abc" % p, "effect ansi %s colour=red" % p, "effect ansi %s hold" % p):
            self.assertFalse(self.wm.execute(bad)["ok"], bad)
        self.assertTrue(self.wm.execute("effect ansi")["ok"])       # the built-in sample
        self.assertIn("ansi", self.wm.execute("effect list")["result"])

    def test_config_mapping_and_frame(self):
        p = self.write("a.asc", "BACKGROUND-ART")
        C.apply_config(self.wm, {"effect": {"name": "ansi", "path": p, "hold": 9, "align": "top"}})
        self.assertEqual(self.wm.background.hold, 9.0)
        scr, frame = screen_of_frame(self.wm)
        self.assertTrue(any("BACKGROUND-ART" in "".join(c[0] for c in row) for row in frame.cells))
        C.apply_config(self.wm, {"effect": ""})
        self.assertIsNone(self.wm.background)


class DebugDiagnosticsTests(unittest.TestCase):
    """PYTERMWM_ANSIART_DEBUG / art_from_bytes(debug=True): the opt-in tracing added for PTW-079,
    which pinpoints the source line responsible when an ANSI art file renders content shifted to
    the wrong place -- the near-universal cause is one source line running past the render width
    and autowrapping onto extra row(s) that push everything below it down."""

    def test_overflowing_line_is_reported_and_others_are_not(self):
        data = b"short\r\n" + b"x" * 25 + b"\r\nend"
        with _CapturedLog("pytermwm.ansiart") as log:
            art = A.art_from_bytes(data, "auto", width=10, debug=True)
        self.assertIn("sauce=", log.text)
        self.assertIn("source line 2 overflowed and autowrapped onto 2 extra row(s)", log.text)
        self.assertNotIn("source line 1 overflowed", log.text)
        self.assertNotIn("source line 3 overflowed", log.text)
        # tracing must never change what is actually rendered
        self.assertEqual(art.rows, A.art_from_bytes(data, "auto", width=10, debug=False).rows)

    def test_off_by_default_and_the_env_var_switches_it_on(self):
        data = b"x" * 25 + b"\r\n"
        with _CapturedLog("pytermwm.ansiart") as log:
            A.art_from_bytes(data, "auto", width=10)
        self.assertEqual(log.records, [])
        with mock.patch.dict(os.environ, {"PYTERMWM_ANSIART_DEBUG": "1"}, clear=False):
            with _CapturedLog("pytermwm.ansiart") as log:
                A.art_from_bytes(data, "auto", width=10)
        self.assertIn("overflowed", log.text)
        with mock.patch.dict(os.environ, {"PYTERMWM_ANSIART_DEBUG": "off"}, clear=False):
            with _CapturedLog("pytermwm.ansiart") as log:
                A.art_from_bytes(data, "auto", width=10)
        self.assertEqual(log.records, [])

    def test_unrecognised_escape_sequence_is_logged(self):
        data = ESC + b"[5yshort"                 # a CSI final pytermwm's Screen doesn't implement
        with _CapturedLog("pytermwm.ansiart") as log:
            A.art_from_bytes(data, "auto", debug=True)
        self.assertIn("unrecognised escape sequence", log.text)
        self.assertIn("ESC[y", log.text)

    def test_explicit_debug_flag_overrides_the_env_var(self):
        data = b"x" * 25 + b"\r\n"
        with mock.patch.dict(os.environ, {"PYTERMWM_ANSIART_DEBUG": "1"}, clear=False):
            with _CapturedLog("pytermwm.ansiart") as log:
                A.art_from_bytes(data, "auto", width=10, debug=False)
        self.assertEqual(log.records, [])


def _have_pyte():
    try:
        import pyte  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_have_pyte(), "pyte (reference VT100 emulator) not installed")
class ReferenceTerminalCrossCheckTests(unittest.TestCase):
    """Cross-checks pytermwm's Screen against `pyte`, an independent, widely-used VT100/xterm
    emulator, on the same byte stream. This is what settled a real user report of "wrong line
    wrapping" in a BBS ANSI art file (PTW-079): pytermwm's rendering matched pyte's byte-for-byte,
    including at the row that looked wrong, proving the file's own escape sequences -- not
    pytermwm's autowrap/CUF handling -- produced that content. Guards against a future regression
    in that VT100 fidelity."""

    def _pyte_rows(self, text, cols, rows):
        import pyte
        screen = pyte.Screen(cols, rows)
        pyte.Stream(screen).feed(text)
        return [line.rstrip() for line in screen.display]

    def test_dense_ansi_art_matches_the_reference_emulator(self):
        # A busy mix of SGR colour changes, absolute and relative cursor moves, and lines that
        # overflow the width and must autowrap -- similar in spirit to real BBS "brick" art.
        text = (
            "\x1b[1;31mAB\x1b[0m\x1b[5C\x1b[33mCDE\r\n"
            + "".join("\x1b[%dm%s" % (30 + (i % 8), chr(0x2580 + (i % 16))) for i in range(30))
            + "\r\n\x1b[2;10Hxy\x1b[0m"
        )
        cols, rows = 20, 10
        scr = Screen(rows, cols, rows)
        scr.newline_mode = True
        scr.feed(text)
        ptw_rows = [scr.row_text(i) for i in range(rows)]
        pyte_rows = self._pyte_rows(text, cols, rows)
        pyte_rows += [""] * (len(ptw_rows) - len(pyte_rows))
        self.assertEqual([r.rstrip() for r in ptw_rows], [r.rstrip() for r in pyte_rows[:len(ptw_rows)]])


if __name__ == "__main__":
    unittest.main()
