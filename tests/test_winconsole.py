"""_WinConsole.sync_buffer_to_window / window_size (pytermwm/compat.py).

A console screen buffer narrower or shorter than its visible window makes the console host itself
wrap/clip output early -- everything pytermwm draws assuming the window's real size gets cut a
second time by the console, which looks exactly like garbled/misaligned rendering right after
attaching from a terminal that starts out already bigger than the console's buffer. Some console
hosts only reconcile buffer and window size on an actual interactive resize, which is the reported
symptom (garbled until the terminal app is resized once by hand); this reconciles it up front.

Tested against a fake kernel32 using the real ctypes structures (`_COORD` / `_SMALL_RECT` /
`_CONSOLE_SCREEN_BUFFER_INFO` are plain ctypes type definitions, safe to construct on any
platform) rather than a real `ctypes.WinDLL`, which only exists on Windows -- see
`tests/test_platform.py::ConPtyReal` for the real-Windows-only smoke test of `_WinConsole` itself.
"""
import ctypes
import unittest

from pytermwm import compat


class _FakeKernel32:
    """Stands in for the real kernel32 DLL: `buf` is the screen buffer size, `window` the visible
    window rect (left, top, right, bottom), both as they'd come back from a real
    GetConsoleScreenBufferInfo. `set_calls` records every SetConsoleScreenBufferSize call."""

    def __init__(self, buf, window):
        self.buf = buf
        self.window = window
        self.set_calls = []
        self.info_calls = 0
        self.fail_info = False

    def GetConsoleScreenBufferInfo(self, handle, ref):
        self.info_calls += 1
        if self.fail_info:
            return 0
        info = ctypes.cast(ref, ctypes.POINTER(compat._CONSOLE_SCREEN_BUFFER_INFO)).contents
        info.dwSize.X, info.dwSize.Y = self.buf
        info.srWindow.Left, info.srWindow.Top, info.srWindow.Right, info.srWindow.Bottom = self.window
        return 1

    def SetConsoleScreenBufferSize(self, handle, coord):
        self.set_calls.append((coord.X, coord.Y))
        self.buf = (coord.X, coord.Y)
        return 1


def _console(buf, window):
    """A `_WinConsole` with a fake kernel32, built without running the real (Windows-only)
    `__init__` (which loads `ctypes.WinDLL("kernel32")`)."""
    con = object.__new__(compat._WinConsole)
    con.ct = ctypes
    con._COORD = compat._COORD
    con._CSBI = compat._CONSOLE_SCREEN_BUFFER_INFO
    con.hout = 1234
    con.k = _FakeKernel32(buf, window)
    return con


class WindowSizeTests(unittest.TestCase):
    def test_reads_the_visible_window_not_the_buffer(self):
        # buffer 200x5000 (lots of scrollback), window is a 120x30 viewport into it
        con = _console(buf=(200, 5000), window=(0, 0, 119, 29))
        self.assertEqual(con.window_size(), (120, 30))

    def test_none_when_the_info_call_fails(self):
        con = _console(buf=(80, 24), window=(0, 0, 79, 23))
        con.k.fail_info = True
        self.assertIsNone(con.window_size())


class SyncBufferToWindowTests(unittest.TestCase):
    def test_grows_a_too_narrow_buffer_to_match_the_window(self):
        con = _console(buf=(80, 300), window=(0, 0, 119, 29))    # window is 120 wide, buffer only 80
        con.sync_buffer_to_window()
        self.assertEqual(con.k.set_calls, [(120, 300)])

    def test_grows_a_too_short_buffer_to_match_the_window(self):
        con = _console(buf=(80, 24), window=(0, 0, 79, 49))      # window is 50 tall, buffer only 24
        con.sync_buffer_to_window()
        self.assertEqual(con.k.set_calls, [(80, 50)])

    def test_grows_both_dimensions_at_once_when_both_are_short(self):
        con = _console(buf=(80, 24), window=(0, 0, 119, 49))
        con.sync_buffer_to_window()
        self.assertEqual(con.k.set_calls, [(120, 50)])

    def test_does_nothing_when_the_buffer_already_fits(self):
        con = _console(buf=(120, 30), window=(0, 0, 119, 29))
        con.sync_buffer_to_window()
        self.assertEqual(con.k.set_calls, [])

    def test_never_shrinks_a_buffer_bigger_than_the_window(self):
        # e.g. real scrollback: a buffer much taller than the window must be left alone
        con = _console(buf=(200, 5000), window=(0, 0, 119, 29))
        con.sync_buffer_to_window()
        self.assertEqual(con.k.set_calls, [])

    def test_is_a_noop_if_the_info_call_fails(self):
        con = _console(buf=(80, 24), window=(0, 0, 119, 29))
        con.k.fail_info = True
        con.sync_buffer_to_window()   # must not raise
        self.assertEqual(con.k.set_calls, [])


if __name__ == "__main__":
    unittest.main()
