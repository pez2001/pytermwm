import os
import sys
import tempfile
import textwrap
import unittest

from tests.helpers import *
from pytermwm import project
from pytermwm.commands import CommandError
from pytermwm.config import ConfigError

PY = sys.executable.replace("\\", "/")


def write(dirpath, text, name=".pytermwm.yaml"):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(text))
    return p


class ProjectFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_find_in_parent(self):
        p = write(self.tmp, "desktops: [a]\n")
        sub = os.path.join(self.tmp, "x", "y")
        os.makedirs(sub)
        self.assertEqual(os.path.realpath(project.find_project_file(sub)), os.path.realpath(p))

    def test_not_found(self):
        d = tempfile.mkdtemp()
        # the temp dir may live below a directory with a project file; only assert the type
        r = project.find_project_file(d)
        self.assertTrue(r is None or os.path.isfile(r))

    def test_validation(self):
        for bad, frag in (({"desktops": "x"}, "list"), ({}, "nothing to do"),
                          ({"desktops": [{"layout": "tile"}]}, "missing name"),
                          ({"desktops": [{"name": "a", "layout": "nope"}]}, "unknown layout"),
                          ({"desktops": [{"name": "a"}, {"name": "a"}]}, "duplicate"),
                          ({"desktops": [{"name": "a", "windows": ["x"]}]}, "mapping"),
                          ({"desktops": ["a"], "focus": "zzz"}, "focus"),
                          ({"desktops": ["a"], "env": [1]}, "env")):
            errs, _ = project.validate(bad)
            self.assertTrue(any(frag in e for e in errs), (bad, errs))
        errs, warns = project.validate({"desktops": [{"name": "a", "windows": [{"cmd": "x", "bogus": 1}]}], "zzz": 1})
        self.assertEqual(errs, [])
        self.assertEqual(len(warns), 2)

    def test_load_reports_yaml_and_validation_errors(self):
        p = write(self.tmp, "desktops: [oops\n")
        with self.assertRaises(ConfigError):
            project.load(p)
        p = write(self.tmp, "desktops: {}\n")
        with self.assertRaises(ConfigError):
            project.load(p)

    def test_session_name(self):
        self.assertEqual(project.session_name({"name": "my app!"}, "/x/.pytermwm.yaml"), "my-app")
        self.assertEqual(project.session_name({}, "/home/me/shop/.pytermwm.yaml"), "shop")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wm = make_wm(100, 30)

    def tearDown(self):
        self.wm.shutdown()

    def doc(self):
        return {"env": {"FOO": "bar"}, "focus": "run", "layout": "columns", "desktops": [
            {"name": "code", "layout": "master", "windows": [
                {"cmd": [sys.executable, "-c", "import time; time.sleep(30)"], "name": "ed"},
                {"kind": "text", "text": "hi", "name": "note", "cwd": "sub"}]},
            {"name": "run", "windows": [{"cmd": [sys.executable, "-c", "import time; time.sleep(30)"], "name": "srv"}]}]}

    def test_apply_builds_desktops_and_windows(self):
        wm = self.wm
        res = project.apply(wm, self.doc(), self.tmp)
        self.assertEqual([d.name for d in wm.desktops], ["code", "run"])     # the empty initial desktop is reused
        names = {d.name: d for d in wm.desktops}
        self.assertIn("code", names)
        self.assertIn("run", names)
        self.assertEqual(names["code"].layout, "master")
        self.assertEqual(names["run"].layout, "columns")
        self.assertEqual(wm.desk.name, "run")                       # focus:
        ed = wm.resolve_window("ed")
        self.assertEqual(ed.desktop.name, "code")
        self.assertEqual(ed.spec["cwd"], self.tmp)
        self.assertEqual(ed.spec["env"], {"FOO": "bar"})
        self.assertEqual(wm.resolve_window("note").spec["cwd"], os.path.normpath(os.path.join(self.tmp, "sub")))
        self.assertEqual(len(res["created"]), 5)

    def test_idempotent(self):
        project.apply(self.wm, self.doc(), self.tmp)
        n_win, n_desk = len(self.wm.windows), len(self.wm.desktops)
        res = project.apply(self.wm, self.doc(), self.tmp)
        self.assertEqual((len(self.wm.windows), len(self.wm.desktops)), (n_win, n_desk))
        self.assertEqual(res["created"], [])
        self.assertEqual(len(res["existing"]), 5)

    def test_missing_pieces_are_added(self):
        project.apply(self.wm, self.doc(), self.tmp)
        d = self.doc()
        d["desktops"][1]["windows"].append({"kind": "text", "text": "x", "name": "extra"})
        res = project.apply(self.wm, d, self.tmp)
        self.assertEqual(res["created"], ["window extra"])

    def test_fresh_replaces_default_shell(self):
        wm = self.wm
        wm.create_window({})                                          # what the server does on a bare start
        self.assertEqual(len(wm.windows), 1)
        project.apply(wm, self.doc(), self.tmp, fresh=True)
        self.assertEqual(sorted(w.name for w in wm.windows.values()), ["ed", "note", "srv"])
        self.assertEqual([d.name for d in wm.desktops], ["code", "run"])

    def test_not_fresh_keeps_existing_windows(self):
        wm = self.wm
        wm.create_window({"name": "mine", "kind": "text", "text": "keep"})
        project.apply(wm, self.doc(), self.tmp, fresh=False)
        self.assertIn("mine", [w.name for w in wm.windows.values()])

    def test_invalid_doc_raises(self):
        with self.assertRaises(CommandError):
            project.apply(self.wm, {"desktops": [{"name": "a", "layout": "bad"}]}, self.tmp)

    def test_desktops_only_project_does_not_quit_the_session(self):
        wm = self.wm
        wm.create_window({})
        project.apply(wm, {"desktops": ["a", "b"]}, self.tmp, fresh=True)
        self.assertEqual(len(wm.windows), 1)                      # the default shell stays: nothing replaces it
        self.assertFalse(wm.quit_requested)
        self.assertEqual([d.name for d in wm.desktops][-1], "b")

    def test_every_desktop_gets_a_focused_window(self):
        project.apply(self.wm, self.doc(), self.tmp)
        for d in self.wm.desktops:
            self.assertIsNotNone(d.focus, d.name)
            self.assertIn(d.focus, d.windows)

    def test_windows_shorthand_is_idempotent(self):
        doc = {"windows": [{"kind": "text", "text": "x", "name": "solo"}]}
        first = project.apply(self.wm, doc, self.tmp)
        second = project.apply(self.wm, doc, self.tmp)
        self.assertEqual(len(self.wm.windows), 1)
        self.assertEqual(second["created"], [])
        self.assertIn("window solo", first["created"])

    def test_windows_shorthand_joins_a_main_desktop(self):
        doc = {"desktops": ["main", "other"], "windows": [{"kind": "text", "text": "x", "name": "w1"}]}
        project.apply(self.wm, doc, self.tmp)
        self.assertEqual(self.wm.resolve_window("w1").desktop.name, "main")

    def test_validation_edge_cases(self):
        errs, _ = project.validate({"desktops": ["a"], "windows": "notalist"})
        self.assertTrue(any("windows" in e for e in errs))
        errs, _ = project.validate({"desktops": [{"name": "a", "windows": [{"name": "x"}]}, {"name": "b", "windows": [{"name": "x"}]}]})
        self.assertTrue(any("duplicate window name" in e for e in errs), errs)

    def test_windows_shortcut_goes_to_current_desktop(self):
        project.apply(self.wm, {"windows": [{"kind": "text", "text": "x", "name": "solo"}]}, self.tmp)
        self.assertEqual(self.wm.resolve_window("solo").desktop, self.wm.desk)


class UpCommandTests(unittest.TestCase):
    def test_up_command(self):
        tmp = tempfile.mkdtemp()
        p = write(tmp, """\
            desktops:
              - name: only
                windows:
                  - {kind: text, text: hello, name: t1}
            """)
        wm = make_wm(100, 30)
        try:
            out = wm.run_command_line("up %s" % ('"%s"' % p), source="test", raise_errors=True)
            self.assertIn("2 created", str(out))
            self.assertIsNotNone(wm.resolve_window("t1"))
            with self.assertRaises(CommandError):
                wm.run_command_line('up "%s"' % os.path.join(tmp, "missing.yaml"), source="test", raise_errors=True)
        finally:
            wm.shutdown()


if __name__ == "__main__":
    unittest.main()
