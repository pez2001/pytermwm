"""The CPU sparkline (and other bars/gauges) default to Unicode 1/8-cell block characters that some fonts do not
have -- notably the Linux virtual console's built-in font, and often PuTTY's -- which then show tofu squares
instead. This covers the config key, the auto-detected hint from an attaching terminal, and the segments/windows
that must respect whichever glyph set is in effect. See docs/user_guide.md "Selection and clipboard" neighbour
section and docs/configuration.md `charts:` for the user-facing behaviour."""
import os
import unittest
from unittest import mock

from tests.helpers import *
from pytermwm import config as C
from pytermwm import statusline
from pytermwm.charts import GLYPH_SETS
from pytermwm.terminal import detect_glyphs


class DetectGlyphsTests(unittest.TestCase):
    def test_linux_console_reports_blocks(self):
        with mock.patch.dict(os.environ, {"TERM": "linux"}):
            self.assertEqual(detect_glyphs(), "blocks")

    def test_anything_else_reports_unicode(self):
        for term in ("xterm-256color", "putty", "screen", "", "dumb"):
            with mock.patch.dict(os.environ, {"TERM": term}):
                self.assertEqual(detect_glyphs(), "unicode")


class WmChartGlyphsTests(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(80, 24)

    def tearDown(self):
        self.wm.shutdown()

    def test_default_is_unicode(self):
        self.assertEqual(self.wm.chart_glyphs(), "unicode")

    def test_the_attaching_terminal_hint_is_used_when_there_is_no_config_override(self):
        self.wm.chart_glyphs_hint = "blocks"
        self.assertEqual(self.wm.chart_glyphs(), "blocks")
        self.wm.chart_glyphs_hint = "unicode"
        self.assertEqual(self.wm.chart_glyphs(), "unicode")

    def test_an_explicit_config_setting_always_wins(self):
        self.wm.chart_glyphs_hint = "unicode"
        self.wm.cfg["charts"] = {"glyphs": "ascii"}
        self.assertEqual(self.wm.chart_glyphs(), "ascii")
        self.wm.chart_glyphs_hint = "blocks"                          # even if the terminal hints otherwise
        self.assertEqual(self.wm.chart_glyphs(), "ascii")

    def test_a_bad_config_value_falls_back_to_the_hint_rather_than_crashing(self):
        self.wm.cfg["charts"] = {"glyphs": "nonsense"}
        self.wm.chart_glyphs_hint = "blocks"
        self.assertEqual(self.wm.chart_glyphs(), "blocks")


class ConfigValidationTests(unittest.TestCase):
    def test_valid_glyph_sets_pass(self):
        for g in GLYPH_SETS:
            errors, warnings = C.validate_config({"charts": {"glyphs": g}})
            self.assertEqual(errors, [], g)

    def test_bad_shape_and_bad_value_are_rejected(self):
        errors, _ = C.validate_config({"charts": "nope"})
        self.assertTrue(errors)
        errors, _ = C.validate_config({"charts": {"glyphs": "cga"}})
        self.assertIn("charts.glyphs", errors[0])

    def test_charts_is_a_known_key(self):
        errors, warnings = C.validate_config({"charts": {"glyphs": "blocks"}})
        self.assertFalse(any("charts" in w and "unknown" in w.lower() for w in warnings), warnings)


class CpuSegmentTests(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(80, 24)
        self.patcher = mock.patch("pytermwm.statusline.SAMPLER.cpu", return_value=(50.0, [50.0]))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.wm.shutdown()

    def _spark_part(self, text):
        return text.split(" ", 2)[-1]

    def test_default_uses_unicode_blocks(self):
        seg = statusline.seg_cpu(self.wm, {})
        self.assertTrue(any(ch in "▁▂▃▄▅▆▇█" for ch in self._spark_part(seg.text)), seg.text)

    def test_config_override_avoids_the_missing_glyphs(self):
        self.wm.cfg["charts"] = {"glyphs": "ascii"}
        seg = statusline.seg_cpu(self.wm, {})
        self.assertTrue(all(ord(c) < 128 for c in seg.text), seg.text)

    def test_terminal_hint_avoids_the_missing_glyphs_without_any_config(self):
        self.wm.chart_glyphs_hint = "blocks"
        seg = statusline.seg_cpu(self.wm, {})
        self.assertNotIn("▁", seg.text)
        self.assertTrue(set(self._spark_part(seg.text)) <= set(" ░▒▓█"), seg.text)

    def test_spark_can_be_disabled_independently(self):
        seg = statusline.seg_cpu(self.wm, {"spark": False})
        self.assertEqual(seg.text, "CPU 50%")


class AttachHintTests(unittest.TestCase):
    """The attach client sends its own environment's glyph guess in the HELLO message (see the real end-to-end
    coverage of the server side, with an actual pty-attached client, in test_server.py's ChartGlyphAttachTests)."""

    def test_hello_payload_includes_glyphs(self):
        with mock.patch("pytermwm.client.detect_glyphs", return_value="blocks"), \
             mock.patch("pytermwm.client.detect_depth", return_value=8), \
             mock.patch("pytermwm.client.term_size", return_value=(80, 24)), \
             mock.patch("pytermwm.protocol.connect") as connect, \
             mock.patch("pytermwm.protocol.MessageBuffer"), \
             mock.patch("pytermwm.compat.Waker"):
            sock = mock.MagicMock()
            connect.return_value = sock
            sock.recv.return_value = b""                              # make the read loop exit immediately
            try:
                from pytermwm import client as client_mod
                client_mod.attach("t-hint")
            except Exception:
                pass
            sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
            self.assertIn(b'"glyphs": "blocks"', sent)


class PvBarTests(unittest.TestCase):
    def test_pv_format_line_respects_its_own_terminal(self):
        from pytermwm.pv import format_line
        with mock.patch("pytermwm.pv.detect_glyphs", return_value="ascii"):
            line = format_line("job", 30, 100, 5.0, 6.0, width=10)
        bar_part = line.split("job ", 1)[1].split(" ", 1)[0]
        self.assertTrue(all(ord(c) < 128 for c in bar_part), bar_part)


if __name__ == "__main__":
    unittest.main()
