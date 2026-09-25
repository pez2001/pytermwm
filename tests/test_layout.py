import unittest
from tests.helpers import *
from pytermwm.layout import *


def no_overlap(rects):
    items = list(rects.values())
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            inter = a.intersect(b)
            if inter.w > 0 and inter.h > 0:
                return False
    return True


def covers(rects, area):
    total = sum(r.w * r.h for r in rects.values())
    return total == area.w * area.h


class LayoutTests(unittest.TestCase):
    A = Rect(0, 0, 80, 24)

    def test_split_sizes_sum(self):
        for total in (10, 33, 80, 101):
            for n in (1, 2, 3, 7):
                s = split_sizes(total, [1] * n)
                self.assertEqual(sum(s), total)
        self.assertEqual(sum(split_sizes(80, [1, 2, 1], gap=1)), 78)

    def test_tree_insert_and_layout(self):
        t = TileTree()
        t.insert(1)
        t.insert(2, 1, "h")
        t.insert(3, 2, "v")
        r = t.layout(self.A)
        self.assertEqual(set(r), {1, 2, 3})
        self.assertTrue(no_overlap(r))
        self.assertTrue(covers(r, self.A))
        self.assertEqual(r[1].h, 24)

    def test_tree_recursive_depth(self):
        t = TileTree()
        t.insert(1)
        for i in range(2, 9):
            t.insert(i, i - 1, "h" if i % 2 == 0 else "v")
        r = t.layout(self.A)
        self.assertEqual(len(r), 8)
        self.assertTrue(no_overlap(r))
        self.assertTrue(covers(r, self.A))

    def test_tree_remove_collapses(self):
        t = TileTree()
        t.insert(1)
        t.insert(2, 1, "h")
        t.insert(3, 2, "v")
        t.remove(3)
        self.assertEqual(t.to_dict()["split"], "h")
        t.remove(2)
        self.assertTrue(t.root.leaf)
        t.remove(1)
        self.assertIsNone(t.root)

    def test_tree_same_direction_flattens(self):
        t = TileTree()
        t.insert(1)
        t.insert(2, 1, "h")
        t.insert(3, 2, "h")
        self.assertEqual(len(t.root.children), 3)

    def test_tree_resize_and_swap_and_rotate(self):
        t = TileTree()
        t.insert(1)
        t.insert(2, 1, "h")
        before = t.layout(self.A)[1].w
        self.assertTrue(t.resize(1, "right", 0.1))
        self.assertGreater(t.layout(self.A)[1].w, before)
        t.swap(1, 2)
        self.assertEqual(t.leaves(), [2, 1])
        t.rotate(1)
        self.assertEqual(t.root.split, "v")

    def test_tree_roundtrip(self):
        t = TileTree()
        t.insert(1)
        t.insert(2, 1, "h")
        t.insert(3, 2, "v")
        t2 = TileTree.from_dict(t.to_dict())
        self.assertEqual(t2.layout(self.A), t.layout(self.A))

    def test_tree_sync(self):
        t = TileTree()
        t.sync([1, 2, 3])
        self.assertEqual(sorted(t.leaves()), [1, 2, 3])
        t.sync([2, 3, 4])
        self.assertEqual(sorted(t.leaves()), [2, 3, 4])

    def test_master(self):
        r = layout_master([1, 2, 3, 4], self.A)
        self.assertTrue(no_overlap(r))
        self.assertTrue(covers(r, self.A))
        self.assertGreater(r[1].w, r[2].w)
        self.assertEqual(r[2].x, r[3].x)

    def test_spiral_covers(self):
        for n in range(1, 8):
            r = layout_spiral(list(range(n)), self.A)
            self.assertTrue(no_overlap(r))
            self.assertTrue(covers(r, self.A))

    def test_centered(self):
        r = layout_centered([1, 2, 3, 4, 5], self.A)
        self.assertTrue(no_overlap(r))
        self.assertLess(r[2].x, r[1].x)
        self.assertGreater(r[3].x, r[1].x)

    def test_stack(self):
        r = layout_stack([1, 2, 3], self.A, "h")
        self.assertEqual([r[i].x for i in (1, 2, 3)], sorted(r[i].x for i in (1, 2, 3)))
        self.assertTrue(covers(r, self.A))

    def test_grid(self):
        for n in range(1, 13):
            r = layout_grid(list(range(n)), Rect(0, 0, 120, 40))
            self.assertEqual(len(r), n)
            self.assertTrue(no_overlap(r))
            self.assertTrue(covers(r, Rect(0, 0, 120, 40)))
        r = layout_grid([1, 2, 3, 4], self.A, cols=2)
        self.assertEqual(r[1].y, r[2].y)
        self.assertGreater(r[3].y, r[1].y)

    def test_table_spans(self):
        r = layout_table([1, 2, 3], self.A, cols=[1, 1], cells={"a": (0, 0, 1, 2)}, names={1: "a"})
        self.assertEqual(r[1].w, 80)
        self.assertEqual(r[2].y, r[3].y)
        self.assertTrue(no_overlap(r))
        self.assertTrue(covers(r, self.A))

    def test_table_weights(self):
        r = layout_table([1, 2], self.A, cols=[1, 3])
        self.assertLess(r[1].w, r[2].w)

    def test_docks(self):
        rects, rem = carve_docks([(1, "left", 20), (2, "bottom", 0.25)], self.A)
        self.assertEqual(rects[1].w, 20)
        self.assertEqual(rects[2].h, 6)
        self.assertEqual(rem.x, 20)
        self.assertEqual(rem.h, 18)

    def test_cascade_and_clamp(self):
        r = cascade_rect(3, self.A)
        c = clamp_rect(Rect(70, 20, 30, 10), self.A)
        self.assertLessEqual(c.x2, 80)
        self.assertLessEqual(c.y2, 24)
        self.assertGreater(r.w, 0)

    def test_focus_neighbor(self):
        rects = layout_grid([1, 2, 3, 4], self.A, cols=2)
        self.assertEqual(focus_neighbor(rects, 1, "right"), 2)
        self.assertEqual(focus_neighbor(rects, 1, "down"), 3)
        self.assertEqual(focus_neighbor(rects, 4, "left"), 3)
        self.assertEqual(focus_neighbor(rects, 4, "up"), 2)
        self.assertIsNone(focus_neighbor(rects, 1, "left"))


if __name__ == "__main__":
    unittest.main()
