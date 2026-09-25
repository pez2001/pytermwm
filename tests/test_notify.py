import os
import sys
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm import compat, notify
from pytermwm.commands import CommandError
from pytermwm.config import validate_config


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(80, 24)

    def tearDown(self):
        self.wm.shutdown()

    def test_osc777_is_sanitised(self):
        seq = notify.osc777("ti;tle\x1b", "bo\x07dy;\nx")
        self.assertEqual(seq.count("\x1b"), 1)
        self.assertEqual(seq.count("\x07"), 1)
        self.assertTrue(seq.startswith("\x1b]777;notify;ti,tle;bo dy,"), seq)
        self.assertNotIn("\n", seq)

    def test_command_message_event_and_osc(self):
        out = self.wm.run_command_line('notify -t Build -s ok "all green"', source="t", raise_errors=True)
        self.assertEqual(out, "all green")
        self.assertIn("Build: all green", self.wm.current_message()[0])
        ev = [e for e in self.wm.event_ring if e["event"] == "notify"][-1]
        self.assertEqual(ev["data"], {"title": "Build", "body": "all green", "style": "ok"})
        self.assertEqual(self.wm.pending_terminal_output, ["\x1b]777;notify;Build;all green\x07"])

    def test_osc_can_be_disabled(self):
        self.wm.cfg["notify"] = {"osc": False}
        self.wm.run_command_line("notify hello", source="t", raise_errors=True)
        self.assertEqual(self.wm.pending_terminal_output, [])

    def test_usage_and_style_errors(self):
        for line in ("notify", "notify -s loud hi", "notify -x hi"):
            with self.assertRaises(CommandError):
                self.wm.run_command_line(line, source="t", raise_errors=True)

    def test_external_command_gets_quoted_values(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "out.txt")
        script = os.path.join(tmp, "n.py").replace("\\", "/")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nopen(%r, 'w', encoding='utf-8').write('|'.join(sys.argv[1:]))\n" % out)
        self.wm.cfg["notify"] = {"osc": False, "command": '"%s" "%s" {title} {body}' % (sys.executable, script)}
        self.wm.run_command_line("notify -t T1 'it is $(whoami) `id` done'", source="t", raise_errors=True)
        end = time.time() + 5
        while time.time() < end and not os.path.exists(out):
            time.sleep(0.05)
        time.sleep(0.1)
        with open(out, encoding="utf-8") as f:
            got = f.read()
        # passed as data, not interpreted by a shell (Windows: characters cmd/PowerShell treat specially become "_")
        self.assertEqual(got, "T1|it is __whoami_ _id_ done" if compat.IS_WINDOWS else "T1|it is $(whoami) `id` done")

    def test_template_values_are_not_expanded_twice(self):
        tmp = tempfile.mkdtemp()
        marker = os.path.join(tmp, "pwned").replace("\\", "/")
        out = os.path.join(tmp, "out.txt")
        script = os.path.join(tmp, "n.py").replace("\\", "/")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nopen(%r, 'w', encoding='utf-8').write('|'.join(sys.argv[1:]))\n" % out)
        self.wm.cfg["notify"] = {"osc": False, "command": '"%s" "%s" {title} {body}' % (sys.executable, script)}
        # a title that looks like a placeholder must not pull the (quoted) body into a second round of substitution
        notify.send(self.wm, "$(touch %s)" % marker, title="{body}")
        end = time.time() + 5
        while time.time() < end and not os.path.exists(out):
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertFalse(os.path.exists(marker))
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), ("{body}|__touch %s_" if compat.IS_WINDOWS else "{body}|$(touch %s)") % marker)

    def test_c1_controls_are_stripped(self):
        self.assertEqual(notify.clean("a\x9bb\x85c\x1b"), "a b c")

    def test_rule_action_on_window_exit(self):
        from pytermwm import config as C
        C.apply_config(self.wm, {"rules": [{"name": "done", "when": {"exit": True},
                                            "do": ["notify -t finished 'window $name ended with $code'"]}]})
        self.wm.create_window({"cmd": [sys.executable, "-c", "pass"], "name": "job", "keep": True})
        pump(self.wm, 5, lambda: any(e["event"] == "notify" for e in self.wm.event_ring))
        ev = [e for e in self.wm.event_ring if e["event"] == "notify"]
        self.assertTrue(ev)
        self.assertEqual(ev[0]["data"]["title"], "finished")
        self.assertEqual(ev[0]["data"]["body"], "window job ended with 0")

    def test_config_validation(self):
        self.assertEqual(validate_config({"notify": {"osc": True, "command": "x {body}"}})[0], [])
        for bad in ("x", {"osc": "yes"}, {"command": 5}, {"nope": 1}):
            self.assertTrue(validate_config({"notify": bad})[0], bad)


if __name__ == "__main__":
    unittest.main()
