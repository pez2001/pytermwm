import unittest

from tests.helpers import *
from pytermwm.prompt import LineEditor


def editor(text="hello world foo", pos=None):
    e = LineEditor(text)
    e.selectable = True
    if pos is not None:
        e.pos = pos
    return e


class LineEditorSelectionTests(unittest.TestCase):
    def test_shift_home_selects_to_start(self):
        e = editor(pos=6)
        e.handle("S-Home")
        self.assertEqual(e.selected_text(), "hello ")
        self.assertEqual(e.pos, 0)

    def test_shift_end_selects_to_end(self):
        e = editor(pos=6)
        e.handle("S-End")
        self.assertEqual(e.selected_text(), "world foo")
        self.assertEqual(e.pos, len(e.text))

    def test_shift_arrows_extend_and_shrink(self):
        e = editor(pos=2)
        e.handle("S-Right"); e.handle("S-Right")
        self.assertEqual(e.selected_text(), "ll")
        e.handle("S-Left")
        self.assertEqual(e.selected_text(), "l")
        e.handle("S-Left")
        self.assertIsNone(e.selection())

    def test_ctrl_shift_arrows_select_words(self):
        e = editor(pos=0)
        e.handle("C-S-Right")
        self.assertEqual(e.selected_text(), "hello")

    def test_plain_movement_clears_selection(self):
        e = editor(pos=6)
        e.handle("S-End")
        e.handle("Left")
        self.assertIsNone(e.selection())
        self.assertEqual(e.pos, 6)               # collapses to the start of the selection
        e.handle("S-End"); e.handle("Home")
        self.assertIsNone(e.selection())
        self.assertEqual(e.pos, 0)

    def test_typing_replaces_selection(self):
        e = editor(pos=6)
        e.handle("S-End")
        e.handle("X")
        self.assertEqual(e.text, "hello X")
        self.assertIsNone(e.selection())

    def test_backspace_and_delete_remove_selection_only(self):
        for key in ("Backspace", "Delete", "C-d"):
            e = editor(pos=6)
            e.handle("S-End")
            self.assertEqual(e.handle(key), "changed")
            self.assertEqual(e.text, "hello ", key)
            self.assertEqual(e.pos, 6)

    def test_copy_and_cut(self):
        e = editor(pos=0)
        e.handle("S-End")
        self.assertEqual(e.handle("M-w"), "copy")
        self.assertEqual(e.kill, "hello world foo")
        self.assertEqual(e.text, "hello world foo")
        self.assertEqual(e.handle("C-x"), "cut")
        self.assertEqual(e.text, "")

    def test_select_all(self):
        e = editor(pos=3)
        e.handle("M-a")
        self.assertEqual(e.selected_text(), e.text)

    def test_not_selectable_by_default(self):
        e = LineEditor("abc")
        self.assertIsNone(e.handle("S-Home"))
        self.assertIsNone(e.selection())


class PromptSelectionTests(unittest.TestCase):
    def setUp(self):
        self.wm = make_wm(80, 20)

    def tearDown(self):
        self.wm.shutdown()

    def test_prompt_shift_home_copy_to_paste_buffer(self):
        pr = self.wm.prompt
        pr.open("echo hi there")
        pr.handle_key("S-Home")
        pr.handle_key("M-w")
        self.assertEqual(self.wm.paste_buffer, "echo hi there")
        pr.handle_key("Backspace")
        self.assertEqual(pr.editor.text, "")

    def test_prompt_paste_replaces_selection(self):
        pr = self.wm.prompt
        pr.open("abc def")
        pr.handle_key("S-Home")
        pr.handle_paste("xyz")
        self.assertEqual(pr.editor.text, "xyz")
