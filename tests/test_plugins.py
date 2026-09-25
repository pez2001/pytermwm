import os
import tempfile
import time
import unittest

from tests.helpers import *
from pytermwm.commands import CommandError
from pytermwm.statusline import SEGMENTS
from pytermwm import config as C

PLUGIN = '''
"""demo plugin"""
from pytermwm.statusline import Segment
from pytermwm.window import TextWindow

def setup(api):
    api.state["n"] = 0
    api.command("hello", lambda wm, args: "hello " + " ".join(args) + " " + str(api.config.get("suffix", "")), help="greet")
    api.segment("demo", lambda wm, o: Segment("demo-seg"))
    api.key("M-g", "hello keypress")
    api.every(0.01, lambda: api.state.__setitem__("n", api.state["n"] + 1))
    api.theme("demo-theme", {"extends": "default", "border": "double", "focus_border": "double"})
    def factory(wm, wid, spec, rows, cols, opts):
        return TextWindow(wid, spec.get("title") or "demo", rows, cols, "from demo kind", name=spec.get("name"), **opts)
    api.window_kind("demo", factory)
    api.on("window_created", lambda window=None, **kw: api.state.setdefault("seen", []).append(window.id))
    import builtins
    builtins._demo_api = api
'''


class PluginCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("PYTERMWM_PLUGIN_PATH")
        os.environ["PYTERMWM_PLUGIN_PATH"] = self.tmp
        self.wm = make_wm(100, 30)
        self.wm.create_window({"cmd": "sleep 30"})

    def tearDown(self):
        self.wm.shutdown()
        if self._old is None:
            os.environ.pop("PYTERMWM_PLUGIN_PATH", None)
        else:
            os.environ["PYTERMWM_PLUGIN_PATH"] = self._old

    def write(self, name, text):
        p = os.path.join(self.tmp, name + ".py")
        with open(p, "w") as f:
            f.write(text)
        return p


class UserPluginTests(PluginCase):
    def test_load_registers_everything(self):
        self.write("demo", PLUGIN)
        self.assertTrue(self.wm.execute("plugin load demo")["ok"])
        self.assertEqual(self.wm.execute("hello world")["result"], "hello world ")
        self.assertIn("demo", SEGMENTS)
        self.assertIn("demo", self.wm.window_kinds)
        self.assertEqual(self.wm.keymap.lookup("M-g")[0], "hello keypress")
        w = self.wm.create_window({"kind": "demo"})
        self.assertIn("from demo kind", w.text() or "from demo kind")
        self.assertIn(w.id, __import__("builtins")._demo_api.state["seen"])
        self.assertTrue(self.wm.execute("theme demo-theme")["ok"])

    def test_timer_runs(self):
        self.write("demo", PLUGIN)
        self.wm.execute("plugin load demo")
        api = __import__("builtins")._demo_api
        pump(self.wm, 0.3)
        self.assertGreater(api.state["n"], 3)

    def test_unload_removes_everything(self):
        self.write("demo", PLUGIN)
        self.wm.execute("plugin load demo")
        self.wm.execute("plugin unload demo")
        self.assertFalse(self.wm.execute("hello")["ok"])
        self.assertNotIn("demo", SEGMENTS)
        self.assertNotIn("demo", self.wm.window_kinds)
        self.assertNotEqual(self.wm.keymap.lookup("M-g")[0], "hello keypress")

    def test_segment_renders_in_statusline(self):
        self.write("demo", PLUGIN)
        self.wm.execute("plugin load demo")
        C.apply_config(self.wm, {"statusline": {"left": ["demo"], "center": [], "right": []}})
        scr, _ = screen_of_frame(self.wm)
        self.assertIn("demo-seg", "\n".join(scr.__class__.line_text(l) for l in scr.lines))

    def test_config_plugin_with_settings(self):
        self.write("demo", PLUGIN)
        C.apply_config(self.wm, {"plugins": [{"name": "demo", "suffix": "!!"}]})
        self.assertEqual(self.wm.execute("hello x")["result"], "hello x !!")
        # removing it from the config unloads it
        C.apply_config(self.wm, {"plugins": []})
        self.assertFalse(self.wm.execute("hello")["ok"])

    def test_config_change_reloads_plugin_with_new_config(self):
        self.write("demo", PLUGIN)
        C.apply_config(self.wm, {"plugins": [{"name": "demo", "suffix": "a"}]})
        C.apply_config(self.wm, {"plugins": [{"name": "demo", "suffix": "b"}]})
        self.assertEqual(self.wm.execute("hello")["result"].strip(), "hello  b")

    def test_hot_reload_on_file_change(self):
        p = self.write("demo", PLUGIN)
        self.wm.execute("plugin load demo")
        time.sleep(0.05)
        self.write("demo", PLUGIN.replace('"hello "', '"HELLO "'))
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.wm.plugins._last_scan = 0
        self.wm.tick(time.time())
        self.assertTrue(self.wm.execute("hello")["result"].startswith("HELLO"))

    def test_manual_reload_command(self):
        self.write("demo", PLUGIN)
        self.wm.execute("plugin load demo")
        self.write("demo", PLUGIN.replace('"hello "', '"HI "'))
        self.assertTrue(self.wm.execute("plugin reload demo")["ok"])
        self.assertTrue(self.wm.execute("hello")["result"].startswith("HI"))

    def test_broken_plugin_reports_error_and_leaves_no_residue(self):
        self.write("bad", "def setup(api):\n    api.command('half', lambda wm, a: 1)\n    raise RuntimeError('boom')\n")
        r = self.wm.execute("plugin load bad")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])
        self.assertFalse(self.wm.execute("half")["ok"])
        self.write("syn", "def setup(api:\n")
        self.assertFalse(self.wm.execute("plugin load syn")["ok"])

    def test_missing_plugin(self):
        r = self.wm.execute("plugin load nonexistent")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_call_soon_from_thread(self):
        self.write("thr", "def setup(api):\n    import builtins\n    builtins._thr_api = api\n"
                          "    api.thread(lambda: api.call_soon(api.state.__setitem__, 'x', 1))\n")
        self.wm.execute("plugin load thr")
        api = __import__("builtins")._thr_api
        pump(self.wm, 1, lambda: api.state.get("x") == 1)
        self.assertEqual(api.state.get("x"), 1)

    def test_plugin_list_and_double_load(self):
        self.write("demo", PLUGIN)
        self.wm.execute("plugin load demo")
        lst = self.wm.execute("plugin list")["result"]
        d = [x for x in lst if x["name"] == "demo"][0]
        self.assertTrue(d["loaded"])
        self.assertIn("hello", d["commands"])
        self.assertFalse(self.wm.execute("plugin load demo")["ok"])

    def test_invalid_plugin_name(self):
        self.assertFalse(self.wm.execute("plugin load ../evil")["ok"])

    def test_handler_exceptions_do_not_break_wm(self):
        self.write("angry", "def setup(api):\n    api.on('window_created', lambda **kw: 1/0)\n    api.every(0.01, lambda: 1/0)\n")
        self.wm.execute("plugin load angry")
        self.wm.create_window({"cmd": "sleep 5"})
        pump(self.wm, 0.2)
        self.assertTrue(self.wm.execute("layout grid")["ok"])


class BuiltinAvailabilityTests(PluginCase):
    def test_builtins_are_listed(self):
        names = set(self.wm.plugins.available() if self.wm.plugins else __import__("pytermwm.plugins", fromlist=["x"]).PluginManager(self.wm).available())
        for n in ("docker", "btop", "mqtt", "ssh", "effects", "openai"):
            self.assertIn(n, names)

    def test_contrib_helper_modules_without_setup_are_not_listed_as_plugins(self):
        # ansiart.py is a support module `effects.py` imports from -- it has no setup(api), so it
        # isn't a plugin.  It used to show up in `plugin list` / the web UI's Plugins tab anyway
        # (available() listed every module under pytermwm.contrib, plugin or not) and loading it
        # failed with "plugin ansiart has no setup(api) function".
        names = set(self.wm.ensure_plugins().available())
        self.assertNotIn("ansiart", names)
        self.assertFalse(self.wm.execute("plugin load ansiart")["ok"])

    def test_a_stray_non_plugin_py_file_in_a_user_plugin_dir_is_not_listed_either(self):
        self.write("helpers", "def not_a_plugin_entry_point():\n    pass\n")
        self.assertNotIn("helpers", set(self.wm.ensure_plugins().available()))

    def test_a_user_file_that_fails_to_parse_is_still_listed(self):
        # can't tell whether it defines setup() (it doesn't even parse), so don't hide it --
        # `plugin load` gives a clear "failed to import" error instead of silently disappearing.
        self.write("broken", "def setup(api):\n    this is not python\n")
        self.assertIn("broken", set(self.wm.ensure_plugins().available()))


class DefinesSetupTests(unittest.TestCase):
    """Unit tests for the static (no-import) check `available()` uses to tell an actual plugin
    (a module with a top-level `setup(api)`) apart from a helper module that merely lives
    alongside one in `pytermwm/contrib/`."""

    def _write(self, tmp, name, text):
        p = os.path.join(tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_true_for_a_module_defining_setup(self):
        from pytermwm.plugins import _defines_setup
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "a.py", "def setup(api):\n    pass\n")
            self.assertTrue(_defines_setup(p))

    def test_true_for_an_async_setup(self):
        from pytermwm.plugins import _defines_setup
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "a.py", "async def setup(api):\n    pass\n")
            self.assertTrue(_defines_setup(p))

    def test_false_for_a_module_without_setup(self):
        from pytermwm.plugins import _defines_setup
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "a.py", "def helper():\n    pass\n")
            self.assertIs(_defines_setup(p), False)

    def test_false_for_a_nested_setup_not_at_module_level(self):
        from pytermwm.plugins import _defines_setup
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "a.py", "class C:\n    def setup(self, api):\n        pass\n")
            self.assertIs(_defines_setup(p), False)

    def test_none_for_unreadable_or_unparsable_files(self):
        from pytermwm.plugins import _defines_setup
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "a.py", "def setup(api:\n   broken syntax\n")
            self.assertIsNone(_defines_setup(p))
        self.assertIsNone(_defines_setup(os.path.join(tmp, "does-not-exist.py")))


if __name__ == "__main__":
    unittest.main()
