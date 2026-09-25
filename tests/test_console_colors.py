"""Colour-depth detection and the Linux console palette override.

Two related complaints, both boiling down to the same root cause: on a terminal that can't do
truecolor, pytermwm downgrades a theme's colours to the nearest of 16 (or 256) ANSI codes
(colors.downgrade), but an SGR code is only an index -- what it actually looks like depends
entirely on *that terminal's own palette*, which pytermwm has no control over and, for a remote
emulator like PuTTY, cannot even reliably detect the real capability of (PuTTY usually reports
plain "xterm" with no COLORTERM, so it gets guessed down to 16 colours even though it can
normally do far better -- the same false-negative already documented on `detect_glyphs`). Two
independent fixes:

* `PYTERMWM_DEPTH` lets a person correct the auto-detected guess (most useful for PuTTY and other
  terminals that under-report themselves).
* On the real Linux virtual console specifically (`TERM=linux`, which a remote client can't set),
  pytermwm reprograms the console's own 16-colour palette (the private `ESC ] P` / `ESC ] R`
  escapes) to match the RGB values it already assumes in `colors.BASIC16`, so the existing
  downgrade decision is no longer just a guess about what the console will show -- it becomes
  accurate. Auto-enabled for TERM=linux; `PYTERMWM_CONSOLE_PALETTE=0` opts out.

See docs/user_guide.md "Colour depth on limited terminals" for the user-facing behaviour.
"""
import os
import pty
import select
import unittest
from unittest import mock

from pytermwm.colors import BASIC16
from pytermwm.compat import RawTerminal
from pytermwm.terminal import console_palette, detect_depth


class DetectDepthOverrideTests(unittest.TestCase):
    def test_env_override_wins_over_autodetection(self):
        with mock.patch.dict(os.environ, {"TERM": "xterm", "COLORTERM": "", "PYTERMWM_DEPTH": "24"}, clear=False):
            self.assertEqual(detect_depth(), 24)

    def test_friendly_aliases(self):
        cases = {"mono": 1, "bw": 1, "16": 4, "16color": 4, "256": 8, "256color": 8,
                  "truecolor": 24, "TrueColor": 24, "1": 1, "4": 4, "8": 8, "24": 24}
        for value, expected in cases.items():
            with mock.patch.dict(os.environ, {"TERM": "xterm", "COLORTERM": "", "PYTERMWM_DEPTH": value}, clear=False):
                self.assertEqual(detect_depth(), expected, value)

    def test_without_override_a_putty_style_xterm_guesses_16(self):
        # PuTTY typically reports plain "xterm" with no COLORTERM -- the false negative
        # PYTERMWM_DEPTH exists to let a person correct.
        with mock.patch.dict(os.environ, {"TERM": "xterm", "COLORTERM": ""}, clear=False):
            os.environ.pop("PYTERMWM_DEPTH", None)
            self.assertEqual(detect_depth(), 4)

    def test_junk_override_is_ignored_and_falls_back_to_autodetection(self):
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color", "COLORTERM": "", "PYTERMWM_DEPTH": "not-a-depth"}, clear=False):
            self.assertEqual(detect_depth(), 8)


class ConsolePaletteDetectionTests(unittest.TestCase):
    def test_linux_console_gets_the_basic16_palette(self):
        with mock.patch.dict(os.environ, {"TERM": "linux"}, clear=False):
            os.environ.pop("PYTERMWM_CONSOLE_PALETTE", None)
            self.assertEqual(console_palette(), dict(enumerate(BASIC16)))

    def test_anything_else_is_left_alone(self):
        # In particular: PuTTY and other remote emulators, which can't set TERM=linux (only the
        # kernel's own virtual console does), must never get this Linux-specific escape.
        for term in ("xterm-256color", "putty", "screen", "", "dumb"):
            with mock.patch.dict(os.environ, {"TERM": term}, clear=False):
                self.assertIsNone(console_palette())

    def test_can_be_disabled(self):
        for off in ("0", "false", "off", "no", "OFF"):
            with mock.patch.dict(os.environ, {"TERM": "linux", "PYTERMWM_CONSOLE_PALETTE": off}, clear=False):
                self.assertIsNone(console_palette())


class RawTerminalPaletteTests(unittest.TestCase):
    """RawTerminal needs a real tty for termios, so these drive it over a real pty and check the
    exact bytes written to the terminal side."""

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(self._close_slave)
        self._slave_open = True

    def _close_slave(self):
        if self._slave_open:
            os.close(self.slave)
            self._slave_open = False

    def _read_all(self):
        out = b""
        while select.select([self.master], [], [], 0.2)[0]:
            out += os.read(self.master, 65536)
        return out

    def test_enter_programs_palette_and_exit_resets_it(self):
        palette = {0: (0, 0, 0), 4: (0, 0, 238), 15: (255, 255, 255)}
        with RawTerminal(fd_in=self.slave, fd_out=self.slave, mouse=False, palette=palette):
            entered = self._read_all()
        left = self._read_all()
        self.assertIn(b"\x1b]P0000000", entered)   # index 0 -> #000000
        self.assertIn(b"\x1b]P40000ee", entered)    # index 4 -> #0000ee
        self.assertIn(b"\x1b]Pfffffff", entered)    # index 15 (hex "f") -> #ffffff
        self.assertIn(b"\x1b]R", left)               # reset on the way out

    def test_no_palette_means_no_palette_escapes_either_way(self):
        with RawTerminal(fd_in=self.slave, fd_out=self.slave, mouse=False, palette=None):
            entered = self._read_all()
        left = self._read_all()
        self.assertNotIn(b"]P", entered)
        self.assertNotIn(b"]R", left)


if __name__ == "__main__":
    unittest.main()
