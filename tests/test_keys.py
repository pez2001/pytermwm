import unittest
from tests.helpers import *
from pytermwm.keys import *


def names(evs):
    return [e.name for e in evs if e.type == "key"]


class KeyTests(unittest.TestCase):
    def test_basic_and_special(self):
        p = KeyParser()
        self.assertEqual(names(p.feed(b"a\r\t\x7f \x03")), ["a", "Enter", "Tab", "Backspace", "Space", "C-c"])

    def test_arrows_and_modifiers(self):
        p = KeyParser()
        self.assertEqual(names(p.feed(b"\x1b[A\x1b[1;5C\x1b[1;3D\x1bOB\x1b[Z")), ["Up", "C-Right", "M-Left", "Down", "S-Tab"])

    def test_function_and_nav_keys(self):
        p = KeyParser()
        self.assertEqual(names(p.feed(b"\x1bOP\x1b[15~\x1b[5~\x1b[3~\x1b[1;2P")), ["F1", "F5", "PageUp", "Delete", "S-F1"])

    def test_alt_keys(self):
        p = KeyParser()
        self.assertEqual(names(p.feed(b"\x1bx\x1b\r\x1b\x7f\x1b:")), ["M-x", "M-Enter", "M-Backspace", "M-:"])

    def test_lone_esc_needs_flush(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b"), [])
        self.assertTrue(p.pending())
        self.assertEqual(names(p.flush()), ["Esc"])

    def test_utf8_split_across_reads(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\xc3"), [])
        self.assertEqual(names(p.feed(b"\xa4")), ["ä"])

    def test_paste(self):
        p = KeyParser()
        evs = p.feed(b"\x1b[200~hello\nworld\x1b[201~x")
        self.assertEqual(evs[0].type, "paste")
        self.assertEqual(evs[0].data, "hello\nworld")
        self.assertEqual(evs[1].name, "x")

    def test_paste_split(self):
        p = KeyParser()
        p.feed(b"\x1b[200~ab")
        evs = p.feed(b"cd\x1b[201~")
        self.assertEqual(evs[0].data, "abcd")

    def test_mouse_sgr(self):
        p = KeyParser()
        e = p.feed(b"\x1b[<0;10;5M\x1b[<0;10;5m\x1b[<64;3;4M\x1b[<32;7;8M")
        self.assertEqual([x.data["kind"] for x in e], ["press", "release", "wheelup", "move"])
        self.assertEqual((e[0].data["x"], e[0].data["y"]), (9, 4))

    def test_focus_events(self):
        p = KeyParser()
        e = p.feed(b"\x1b[I\x1b[O")
        self.assertEqual([x.data for x in e], [True, False])

    def test_key_to_bytes_roundtrip(self):
        p = KeyParser()
        for name in ["a", "Enter", "Tab", "Up", "Down", "Left", "Right", "Home", "End", "PageUp", "F1", "F5", "C-c", "M-x",
                     "C-Up", "Backspace", "Esc", "S-Tab", "Delete", "F12"]:
            data = key_to_bytes(name)
            self.assertIsNotNone(data, name)
            got = names(p.feed(data) + p.flush())
            self.assertEqual(got, [name], "roundtrip of %s via %r" % (name, data))

    def test_app_cursor(self):
        self.assertEqual(key_to_bytes("Up", True), b"\x1bOA")
        self.assertEqual(key_to_bytes("Up", False), b"\x1b[A")

    def test_keys_to_bytes(self):
        self.assertEqual(keys_to_bytes("echo Enter"), b"echo\r")
        self.assertEqual(keys_to_bytes(["ls", "Enter", "C-c"]), b"ls\r\x03")


class KeymapTests(unittest.TestCase):
    def test_direct_and_prefix(self):
        k = Keymap()
        self.assertEqual(k.lookup("M-Enter"), ("new-window", True))
        self.assertEqual(k.lookup("C-b")[0], "__prefix__")
        self.assertEqual(k.lookup("c"), ("new-window", True))
        self.assertEqual(k.lookup("c"), (None, False))

    def test_prefix_twice_passes_through(self):
        k = Keymap()
        k.lookup("C-b")
        self.assertEqual(k.lookup("C-b"), (None, False))

    def test_prefix_timeout(self):
        k = Keymap()
        k.lookup("C-b", now=100)
        self.assertEqual(k.lookup("c", now=200), (None, False))

    def test_modes_are_sticky(self):
        k = Keymap()
        k.mode = "resize"
        self.assertEqual(k.lookup("h"), ("resize left", True))
        self.assertEqual(k.lookup("z"), (None, True))
        self.assertEqual(k.lookup("M-h"), (None, True))

    def test_custom_config_and_unbind(self):
        k = Keymap({"prefix": "C-a", "direct": {"F5": "reload", "M-Enter": "none"}})
        self.assertEqual(k.lookup("F5"), ("reload", True))
        self.assertEqual(k.lookup("M-Enter"), (None, False))
        self.assertEqual(k.lookup("C-a")[0], "__prefix__")

    def test_unknown_after_prefix_swallowed(self):
        k = Keymap()
        k.lookup("C-b")
        self.assertEqual(k.lookup("ö"), (None, True))


if __name__ == "__main__":
    unittest.main()
