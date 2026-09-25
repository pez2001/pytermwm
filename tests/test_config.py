import os
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm import config as C


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wm = make_wm(100, 30)

    def tearDown(self):
        self.wm.shutdown()

    def write(self, text, name="pytermwm.yml"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p


class LoadTests(ConfigCase):
    def test_default_config_is_valid(self):
        cfg = C.load_yaml_text(C.DEFAULT_CONFIG_YAML)
        errs, warns = C.validate_config(cfg)
        self.assertEqual(errs, [])

    def test_default_config_applies(self):
        cfg = C.load_yaml_text(C.DEFAULT_CONFIG_YAML)
        C.apply_config(self.wm, cfg, initial=True)
        self.assertEqual([d.name for d in self.wm.desktops], ["main", "dev", "logs"])
        self.assertEqual(len(self.wm.windows), 1)

    def test_validation_reports_problems(self):
        errs, _ = C.validate_config({"layout": "nope", "gap": "wide", "desktops": "x", "history": -1})
        joined = " ".join(errs)
        self.assertIn("layout", joined)
        self.assertIn("gap", joined)
        self.assertGreaterEqual(len(errs), 3)

    def test_unknown_keys_warn_not_fail(self):
        errs, warns = C.validate_config({"layoutt": "tile"})
        self.assertEqual(errs, [])
        self.assertTrue(any("layoutt" in w for w in warns))

    def test_include_merges_files(self):
        self.write("theme: hacker\ngap: 2\n", "base.yml")
        p = self.write("include: [base.yml]\ngap: 3\n")
        cfg, files = C.load_config_file(p)
        self.assertEqual(cfg["theme"], "hacker")
        self.assertEqual(cfg["gap"], 3)
        self.assertEqual(len(files), 2)

    def test_include_cycle_is_safe(self):
        self.write("include: [b.yml]\n", "a.yml")
        self.write("include: [a.yml]\ngap: 1\n", "b.yml")
        with self.assertRaises(C.ConfigError) as cm:
            C.load_config_file(os.path.join(self.tmp, "a.yml"))
        self.assertIn("loop", str(cm.exception))

    def test_bad_yaml_raises_config_error(self):
        p = self.write("layout: [unclosed\n")
        with self.assertRaises(C.ConfigError):
            C.load_config_file(p)

    def test_apply_theme_layout_gap_and_keys(self):
        cfg = {"theme": "hacker", "layout": "grid", "gap": 1, "keys": {"direct": {"M-x": "layout rows"}}}
        C.apply_config(self.wm, cfg, initial=True)
        self.assertEqual(self.wm.theme.name, "hacker")
        self.assertEqual(self.wm.desk.layout, "grid")
        self.assertEqual(self.wm.cfg["gap"], 1)
        self.assertEqual(self.wm.keymap.lookup("M-x")[0], "layout rows")

    def test_inline_theme_mapping(self):
        cfg = {"theme": {"extends": "hacker", "border": "double"}}
        C.apply_config(self.wm, cfg, initial=True)
        self.assertEqual(self.wm.theme.opts["border"], "double")

    def test_statusline_config(self):
        cfg = {"statusline": {"position": "top", "left": ["hostname"], "center": [], "right": ["time"]}}
        C.apply_config(self.wm, cfg, initial=True)
        self.assertEqual(self.wm.content_area().y, 1)


class ReconcileTests(ConfigCase):
    def cfg(self, windows):
        return {"desktops": ["main", "dev"], "windows": windows}

    def test_named_windows_created_on_desktops(self):
        C.apply_config(self.wm, self.cfg([
            {"name": "a", "cmd": "sleep 30", "desktop": "main"},
            {"name": "b", "cmd": "sleep 30", "desktop": "dev", "title": "Bee"}]), initial=True)
        a, b = self.wm.resolve_window("a"), self.wm.resolve_window("b")
        self.assertEqual(a.desktop.name, "main")
        self.assertEqual(b.desktop.name, "dev")
        self.assertEqual(b.title, "Bee")

    def test_reapply_updates_in_place_without_restart(self):
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30", "title": "one"}]), initial=True)
        a = self.wm.resolve_window("a")
        pid = a.source.pid
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30", "title": "two", "border": False}]))
        self.assertIs(self.wm.resolve_window("a"), a)
        self.assertEqual(a.title, "two")
        self.assertFalse(a.opts["border"])
        self.assertEqual(a.source.pid, pid)

    def test_changed_command_restarts_window(self):
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30"}]), initial=True)
        a = self.wm.resolve_window("a")
        pid = a.source.pid
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 31"}]))
        time.sleep(0.1)
        a2 = self.wm.resolve_window("a")
        self.assertNotEqual(a2.source.pid, pid)

    def test_removed_window_is_closed_but_unmanaged_kept(self):
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30"}, {"name": "b", "cmd": "sleep 30"}]), initial=True)
        manual = self.wm.create_window({"cmd": "sleep 30", "title": "manual"})
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30"}]))
        names = [w.name for w in self.wm.windows.values()]
        self.assertIn("a", names)
        self.assertNotIn("b", names)
        self.assertIn(manual.id, self.wm.windows)

    def test_layout_per_desktop(self):
        cfg = {"desktops": [{"name": "main", "layout": "grid"}, {"name": "dev", "layout": "rows"}]}
        C.apply_config(self.wm, cfg, initial=True)
        self.assertEqual(self.wm.desktops[0].layout, "grid")
        self.assertEqual(self.wm.desktops[1].layout, "rows")

    def test_desktop_removed_from_config_keeps_windows(self):
        C.apply_config(self.wm, {"desktops": ["a", "b", "c"]}, initial=True)
        w = self.wm.create_window({"cmd": "sleep 30"})
        C.apply_config(self.wm, {"desktops": ["a", "b"]})
        self.assertIn(w.id, self.wm.windows)

    def test_window_kinds_from_config(self):
        C.apply_config(self.wm, self.cfg([
            {"name": "cpu", "kind": "chart", "source": "cpu"},
            {"name": "stat", "kind": "status"},
            {"name": "log", "kind": "log"}]), initial=True)
        kinds = {w.name: w.kind for w in self.wm.windows.values()}
        self.assertEqual(kinds, {"cpu": "chart", "stat": "status", "log": "log"})

    def test_invalid_window_spec_errors_without_changes(self):
        C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30"}]), initial=True)
        n = len(self.wm.windows)
        with self.assertRaises(C.ConfigError):
            C.apply_config(self.wm, self.cfg([{"name": "a", "cmd": "sleep 30"}, {"name": "x", "kind": "warp-core"}]))
        self.assertEqual(len(self.wm.windows), n)


class LiveReloadTests(ConfigCase):
    def test_file_change_is_applied_on_tick(self):
        p = self.write("layout: tile\ngap: 0\nwindows:\n  - {name: a, cmd: 'sleep 30'}\n")
        mgr = C.attach_config(self.wm, p)
        self.assertEqual(self.wm.cfg["gap"], 0)
        time.sleep(0.05)
        self.write("layout: tile\ngap: 2\nwindows:\n  - {name: a, cmd: 'sleep 30'}\n  - {name: b, cmd: 'sleep 30'}\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        mgr.last_check = 0
        self.wm.tick(time.time())
        self.assertEqual(self.wm.cfg["gap"], 2)
        self.assertIn("b", [w.name for w in self.wm.windows.values()])
        self.assertIsNone(mgr.last_error)

    def test_invalid_change_keeps_running_state_and_reports(self):
        p = self.write("gap: 1\nwindows:\n  - {name: a, cmd: 'sleep 30'}\n")
        mgr = C.attach_config(self.wm, p)
        self.write("gap: banana\nlayout: nope\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        mgr.last_check = 0
        self.wm.tick(time.time())
        self.assertEqual(self.wm.cfg["gap"], 1)
        self.assertIsNotNone(mgr.last_error)
        self.assertIn("a", [w.name for w in self.wm.windows.values()])
        # and recovers once fixed
        self.write("gap: 3\n")
        os.utime(p, (time.time() + 10, time.time() + 10))
        mgr.last_check = 0
        self.wm.tick(time.time())
        self.assertEqual(self.wm.cfg["gap"], 3)
        self.assertIsNone(mgr.last_error)

    def test_include_file_change_triggers_reload(self):
        inc = self.write("gap: 4\n", "inc.yml")
        p = self.write("include: [inc.yml]\n")
        mgr = C.attach_config(self.wm, p)
        self.assertEqual(self.wm.cfg["gap"], 4)
        self.write("gap: 5\n", "inc.yml")
        os.utime(inc, (time.time() + 5, time.time() + 5))
        mgr.last_check = 0
        self.wm.tick(time.time())
        self.assertEqual(self.wm.cfg["gap"], 5)

    def test_reload_command(self):
        p = self.write("gap: 1\n")
        C.attach_config(self.wm, p)
        self.write("gap: 2\n")
        self.assertTrue(self.wm.execute("reload")["ok"])
        self.assertEqual(self.wm.cfg["gap"], 2)

    def test_reload_without_file_is_an_error(self):
        self.assertFalse(self.wm.execute("reload")["ok"])

    def test_config_get_command(self):
        self.wm.cfg["gap"] = 7
        self.assertEqual(self.wm.execute("config-get gap")["result"], 7)


if __name__ == "__main__":
    unittest.main()
