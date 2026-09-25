import os
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm import config as C
from pytermwm.scripting import validate_rules, strip_ansi


class RuleCase(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(100, 30)

    def tearDown(self):
        self.wm.shutdown()

    def rules(self, rules, **cfg):
        cfg["rules"] = rules
        C.apply_config(self.wm, cfg)
        return self.wm.rules


class ValidateTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_rules([{"name": "a", "when": {"output_matches": "x"}, "do": ["layout grid"]}]), [])

    def test_problems(self):
        errs = validate_rules([
            {"when": {}, "do": ["x"]},
            {"when": {"output_matches": "("}, "do": ["x"]},
            {"when": {"idle": 1, "interval": 2}, "do": ["x"]},
            {"when": {"idle": "soon"}, "do": ["x"]},
            {"when": {"interval": 1}, "do": []},
            {"name": "d", "when": {"interval": 1}, "do": ["x"]},
            {"name": "d", "when": {"interval": 1}, "do": ["x"]},
            {"when": {"interval": 1}, "do": [{"python": "def ("}]},
            {"when": {"status": "x"}, "do": ["x"]},
            {"when": {"interval": 1}, "do": ["x"], "bogus": 1},
            "notamapping",
        ])
        self.assertGreaterEqual(len(errs), 10, errs)

    def test_config_rejects_invalid_rules(self):
        wm = make_wm()
        with self.assertRaises(C.ConfigError):
            C.apply_config(wm, {"rules": [{"when": {"nope": 1}, "do": ["x"]}]})
        wm.shutdown()

    def test_strip_ansi(self):
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m \x1b]0;title\x07ok"), "red ok")


class OutputRuleTests(RuleCase):
    def test_output_match_runs_commands_with_variables(self):
        self.rules([{"name": "r", "when": {"window": "build", "output_matches": r"ERROR (\w+)"},
                     "do": ["focus $window", "rename got-$1", "status-set last $line"]}])
        w = self.wm.create_window({"cmd": "sleep 0.3; echo 'ERROR E42 happened'; sleep 30", "name": "build"})
        other = self.wm.create_window({"cmd": "echo ERROR nope; sleep 30", "name": "other"})
        pump(self.wm, 3, lambda: w.title.startswith("got-"))
        self.assertEqual(w.title, "got-E42")
        self.assertNotEqual(other.title, "got-nope")
        self.assertEqual(self.wm.status_items["last"]["value"], "ERROR E42 happened")
        self.assertEqual(self.wm.rules.by_name["r"].fired, 1)

    def test_lines_split_across_chunks_and_colored(self):
        self.rules([{"name": "r", "when": {"output_matches": "DONE"}, "do": ["status-set s hit"]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("\x1b[32mDO")
        self.assertNotIn("s", self.wm.status_items)
        w.feed_text("NE\x1b[0m\r\n")
        self.assertIn("s", self.wm.status_items)

    def test_shell_metacharacters_in_output_cannot_inject(self):
        marker = tempfile.mktemp()
        self.rules([{"name": "r", "when": {"output_matches": "x"}, "do": [{"shell": "echo $line > %s.out" % marker}]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("x; touch %s.pwned $(touch %s.pwned2)\n" % (marker, marker))
        time.sleep(0.5)
        self.assertFalse(os.path.exists(marker + ".pwned"))
        self.assertFalse(os.path.exists(marker + ".pwned2"))
        self.assertTrue(os.path.exists(marker + ".out"))
        os.unlink(marker + ".out")

    def test_command_injection_via_output_blocked(self):
        self.rules([{"name": "r", "when": {"output_matches": "x"}, "do": ["message $line"]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        n = len(self.wm.windows)
        w.feed_text("x ; new-window -- sleep 9\n")
        self.assertEqual(len(self.wm.windows), n)

    def test_cooldown(self):
        self.rules([{"name": "r", "when": {"output_matches": "hit"}, "do": ["status-set c x"], "cooldown": 30}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("hit\nhit\nhit\n")
        self.assertEqual(self.wm.rules.by_name["r"].fired, 1)

    def test_disable_enable_and_fire(self):
        self.rules([{"name": "r", "when": {"output_matches": "hit"}, "do": ["status-set c $line"]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        self.wm.execute("rule disable r")
        w.feed_text("hit\n")
        self.assertEqual(self.wm.rules.by_name["r"].fired, 0)
        self.wm.execute("rule enable r")
        w.feed_text("hit\n")
        self.assertEqual(self.wm.rules.by_name["r"].fired, 1)
        self.assertTrue(self.wm.execute("rule fire r")["ok"])
        self.assertEqual(self.wm.rules.by_name["r"].fired, 2)
        self.assertFalse(self.wm.execute("rule fire nope")["ok"])

    def test_runaway_rule_is_throttled(self):
        # a rule that writes matching output into its own window must not spin forever
        self.rules([{"name": "loop", "when": {"output_matches": "ping"},
                     "do": [{"python": "wm.windows[int(ctx['window'])].feed_text('ping again\\n')"}]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("ping\n")
        self.assertLess(self.wm.rules.by_name["loop"].fired, 50)

    def test_reload_keeps_counters(self):
        self.rules([{"name": "r", "when": {"output_matches": "hit"}, "do": ["status-set c x"]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("hit\n")
        self.rules([{"name": "r", "when": {"output_matches": "hit"}, "do": ["status-set c y"]}])
        self.assertEqual(self.wm.rules.by_name["r"].fired, 1)

    def test_output_changed_and_python_action(self):
        self.rules([{"name": "r", "when": {"output_changed": True, "window": "w"}, "cooldown": 0,
                     "do": [{"python": "wm.status_items['py'] = {'value': ctx['title'], 'label': None, 'style': 'normal', 'time': 0, 'expires': None}"}]}])
        w = self.wm.create_window({"cmd": "sleep 30", "name": "w", "title": "TT"})
        w.feed_text("anything")
        self.assertEqual(self.wm.status_items["py"]["value"], "TT")

    def test_failing_action_is_reported_not_raised(self):
        self.rules([{"name": "r", "when": {"output_matches": "hit"}, "do": [{"python": "1/0"}, "status-set after ok"]}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("hit\n")
        self.assertIn("ZeroDivisionError", self.wm.rules.by_name["r"].last_error)
        self.assertIn("after", self.wm.status_items)


class OtherTriggerTests(RuleCase):
    def test_exit_trigger_and_codes(self):
        self.rules([{"name": "bad", "when": {"exit": "error"}, "do": ["status-set failed $name-$code"]},
                    {"name": "good", "when": {"exit": [0]}, "do": ["status-set ok $name"]}])
        self.wm.create_window({"cmd": "exit 3", "name": "j1", "keep": True})
        self.wm.create_window({"cmd": "true", "name": "j2", "keep": True})
        pump(self.wm, 3, lambda: "failed" in self.wm.status_items and "ok" in self.wm.status_items)
        self.assertEqual(self.wm.status_items["failed"]["value"], "j1-3")
        self.assertEqual(self.wm.status_items["ok"]["value"], "j2")

    def test_interval(self):
        self.rules([{"name": "tick", "when": {"interval": 0.05}, "do": ["status-set n $time"]}])
        pump(self.wm, 0.4)
        self.assertGreaterEqual(self.wm.rules.by_name["tick"].fired, 3)

    def test_idle_fires_once_until_output_resumes(self):
        self.rules([{"name": "idle", "when": {"idle": 0.15, "window": "w"}, "do": ["status-set idle yes"]}])
        w = self.wm.create_window({"cmd": "sleep 30", "name": "w"})
        w.feed_text("out\n")
        pump(self.wm, 0.6)
        self.assertEqual(self.wm.rules.by_name["idle"].fired, 1)
        w.feed_text("more\n")
        pump(self.wm, 0.5)
        self.assertEqual(self.wm.rules.by_name["idle"].fired, 2)

    def test_event_trigger_with_match(self):
        self.rules([{"name": "ev", "when": {"event": "window_created", "match": {"title": "^watch"}}, "do": ["status-set seen $title"]}])
        self.wm.create_window({"cmd": "sleep 30", "title": "other"})
        self.assertNotIn("seen", self.wm.status_items)
        self.wm.create_window({"cmd": "sleep 30", "title": "watch-me"})
        self.assertEqual(self.wm.status_items["seen"]["value"], "watch-me")

    def test_status_trigger_edge(self):
        self.rules([{"name": "hot", "when": {"status": "temp", "above": 80}, "do": ["message hot"]}])
        self.wm.execute("status-set temp 50")
        self.assertEqual(self.wm.rules.by_name["hot"].fired, 0)
        self.wm.execute("status-set temp 90")
        self.wm.execute("status-set temp 95")          # still above: no second firing
        self.assertEqual(self.wm.rules.by_name["hot"].fired, 1)
        self.wm.execute("status-set temp 10")
        self.wm.execute("status-set temp 99")
        self.assertEqual(self.wm.rules.by_name["hot"].fired, 2)

    def test_delay(self):
        self.rules([{"name": "d", "when": {"interval": 100}, "do": [{"command": "status-set late yes", "delay": 0.1}]}])
        self.wm.rules.fire("d")
        self.assertNotIn("late", self.wm.status_items)
        pump(self.wm, 0.4, lambda: "late" in self.wm.status_items)
        self.assertIn("late", self.wm.status_items)

    def test_once(self):
        self.rules([{"name": "o", "when": {"output_matches": "x"}, "do": ["status-set o $line"], "once": True}])
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("x1\nx2\n")
        self.assertEqual(self.wm.rules.by_name["o"].fired, 1)


class UndoTests(RuleCase):
    def test_undo_after_restores_focus_zoom_layout(self):
        a = self.wm.create_window({"cmd": "sleep 30", "name": "a"})
        b = self.wm.create_window({"cmd": "sleep 30", "name": "build"})
        self.wm.focus_window(a.id)
        self.rules([{"name": "r", "when": {"window": "build", "output_matches": "FAIL"},
                     "do": ["focus $window", "zoom on"], "undo_after": 0.15}])
        b.feed_text("FAIL\n")
        self.assertEqual(self.wm.focused, b)
        self.assertEqual(self.wm.desk.zoom, b.id)
        pump(self.wm, 0.6, lambda: self.wm.desk.zoom is None)
        self.assertIsNone(self.wm.desk.zoom)
        self.assertEqual(self.wm.focused, a)

    def test_explicit_undo_commands(self):
        self.rules([{"name": "r", "when": {"interval": 100}, "do": ["layout grid"], "undo": ["layout rows"], "undo_after": 0.1}])
        self.wm.rules.fire("r")
        self.assertEqual(self.wm.desk.layout, "grid")
        pump(self.wm, 0.5, lambda: self.wm.desk.layout == "rows")
        self.assertEqual(self.wm.desk.layout, "rows")


class ScriptTests(RuleCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()

    def script(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_script_registers_command_and_handlers(self):
        p = self.script("s1.py", "api.command('scripted', lambda wm, a: 'from-script')\n"
                                 "api.on_output('BUILD (\\\\d+)', lambda w, line, m: wm.execute('status-set b ' + m.group(1)))\n")
        self.rules([], scripts=[p])
        self.assertEqual(self.wm.execute("scripted")["result"], "from-script")
        w = self.wm.create_window({"cmd": "sleep 30"})
        w.feed_text("BUILD 77\n")
        self.assertEqual(self.wm.status_items["b"]["value"], "77")

    def test_script_reloads_when_changed_and_cleans_up(self):
        p = self.script("s2.py", "api.command('one', lambda wm, a: 1)\n")
        self.rules([], scripts=[p])
        self.assertTrue(self.wm.execute("one")["ok"])
        self.script("s2.py", "api.command('two', lambda wm, a: 2)\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.wm.rules._script_scan = 0
        self.wm.tick(time.time())
        self.assertTrue(self.wm.execute("two")["ok"])
        self.assertFalse(self.wm.execute("one")["ok"])

    def test_broken_script_is_isolated(self):
        bad = self.script("bad.py", "raise RuntimeError('nope')\n")
        good = self.script("good.py", "api.command('still-works', lambda wm, a: 'y')\n")
        self.rules([], scripts=[bad, good])
        self.assertTrue(self.wm.execute("still-works")["ok"])
        d = {x["name"]: x for x in self.wm.rules.describe()}
        self.assertIn("nope", d["script:bad.py"]["error"])

    def test_script_syntax_error_reported(self):
        bad = self.script("syn.py", "def (:\n")
        self.rules([], scripts=[bad])
        self.assertTrue(any("syn.py" in x["name"] and x["error"] for x in self.wm.rules.describe()))


if __name__ == "__main__":
    unittest.main()
