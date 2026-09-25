"""Golden-frame tests: the rendered screen (text and escape sequences) of every layout and theme is compared with a
stored file, so any visual change shows up as a reviewable diff.

Regenerate after an intended change:   PTW_UPDATE_GOLDEN=1 python run_tests.py -k golden
"""
import os
import unittest

from tests.helpers import *
from pytermwm import config as C
from pytermwm.layout import ENGINE_NAMES
from pytermwm.render import Compositor, FrameWriter
from pytermwm.theme import all_theme_names

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
UPDATE = bool(os.environ.get("PTW_UPDATE_GOLDEN"))
BUILTIN_THEMES = ("default", "light", "modern", "hacker", "bbs", "mc", "c64", "amiga")


def build(theme="default", layout="tile", windows=4, cols=60, rows=18):
    wm = make_wm(cols, rows)
    C.apply_config(wm, {"theme": theme, "layout": layout, "desktops": ["main"],
                        "statusline": {"position": "bottom", "left": ["desktops", "layout"], "center": [], "right": []}})
    for i in range(windows):
        wm.create_window({"kind": "text", "title": "win %d" % (i + 1), "name": "w%d" % (i + 1),
                          "text": "window %d\nsecond line\nthird line" % (i + 1)})
    wm.run_command_line("layout %s" % layout, source="test", raise_errors=True)      # (config: layout only applies at startup)
    return wm


def render(wm) -> str:
    frame = Compositor(wm).compose(wm.cols, wm.rows)
    ansi = FrameWriter(24).write(frame).replace("\x1b", "<ESC>")
    return "%s\n--- ansi (24-bit) ---\n%s\n" % (frame.text(), ansi.replace("\r", "<CR>"))


class GoldenCase(unittest.TestCase):
    def check(self, name: str, actual: str):
        path = os.path.join(GOLDEN, name + ".txt")
        if UPDATE:
            os.makedirs(GOLDEN, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(actual)
            return
        if not os.path.exists(path):
            self.fail("no golden file %s; create it with PTW_UPDATE_GOLDEN=1" % path)
        with open(path, encoding="utf-8", newline="") as f:
            expected = f.read().replace("\r\n", "\n")
        if expected != actual:
            import difflib
            diff = "\n".join(list(difflib.unified_diff(expected.split("\n"), actual.split("\n"), "golden", "actual", lineterm="", n=1))[:60])
            self.fail("frame differs from %s (PTW_UPDATE_GOLDEN=1 to accept):\n%s" % (name, diff))


class LayoutGoldens(GoldenCase):
    def test_every_layout(self):
        for layout in ENGINE_NAMES:
            with self.subTest(layout=layout):
                wm = build(layout=layout)
                try:
                    self.check("layout_" + layout, render(wm))
                finally:
                    wm.shutdown()

    def test_single_window_and_many(self):
        for n in (1, 2, 7):
            with self.subTest(windows=n):
                wm = build(windows=n)
                try:
                    self.check("tile_%d_windows" % n, render(wm))
                finally:
                    wm.shutdown()

    def test_small_screen(self):
        wm = build(cols=24, rows=8, windows=3)
        try:
            self.check("tile_small_screen", render(wm))
        finally:
            wm.shutdown()


class ThemeGoldens(GoldenCase):
    def test_builtin_theme_list(self):
        self.assertEqual([t for t in BUILTIN_THEMES if t in all_theme_names()], list(BUILTIN_THEMES))

    def test_every_theme(self):
        for theme in BUILTIN_THEMES:
            with self.subTest(theme=theme):
                wm = build(theme=theme, windows=3)
                try:
                    self.check("theme_" + theme, render(wm))
                finally:
                    wm.shutdown()


class OverlayGoldens(GoldenCase):
    def test_focus_cues(self):
        wm = build(windows=2)
        try:
            wm.run_command_line("focus w1", source="test", raise_errors=True)
            self.check("two_windows_focus_first", render(wm))
        finally:
            wm.shutdown()

    def test_floating_and_docked(self):
        wm = build(windows=2)
        try:
            wm.create_window({"kind": "text", "title": "float", "text": "floating", "floating": True, "rect": [8, 4, 30, 8]})
            wm.create_window({"kind": "text", "title": "dock", "text": "docked", "dock": "bottom:4"})
            self.check("floating_and_docked", render(wm))
        finally:
            wm.shutdown()


if __name__ == "__main__":
    unittest.main()
