"""The examples in the documentation must stay valid."""
import ast
import glob
import os
import re
import unittest

from pytermwm.config import load_yaml_text, validate_config, KNOWN_KEYS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = sorted(glob.glob(os.path.join(ROOT, "docs", "*.md")) + [os.path.join(ROOT, "README.md")])


def blocks(lang):
    for path in DOCS:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for m in re.finditer(r"```%s\n(.*?)```" % lang, text, re.S):
            yield os.path.relpath(path, ROOT), m.group(1)


class DocExamples(unittest.TestCase):
    def test_yaml_examples_parse_and_validate(self):
        n = 0
        for path, code in blocks("yaml"):
            data = load_yaml_text(code)
            self.assertIsInstance(data, dict, path)
            if "desktops" in data and any(isinstance(d, dict) for d in data["desktops"]):   # a project file
                from pytermwm.project import validate as validate_project
                errors, warnings = validate_project(data)
                self.assertEqual((errors, warnings), ([], []), "%s: %s" % (path, code[:80]))
            elif set(data) <= KNOWN_KEYS:                     # a full or partial config
                errors, _ = validate_config(data)
                self.assertEqual(errors, [], "%s: %s" % (path, code[:80]))
            n += 1
        self.assertGreaterEqual(n, 5)

    def test_python_examples_compile(self):
        n = 0
        for path, code in blocks("python"):
            ast.parse(code)
            n += 1
        self.assertGreaterEqual(n, 1)

    def test_doc_links_resolve(self):
        for path in DOCS:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for target in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", text):
                full = os.path.normpath(os.path.join(os.path.dirname(path), target))
                self.assertTrue(os.path.exists(full), "%s links to missing %s" % (os.path.relpath(path, ROOT), target))


if __name__ == "__main__":
    unittest.main()
