"""Layout engines.

Every engine maps an ordered list of window ids plus a screen area to
rectangles.  Engines: ``tile`` (recursive tree), ``master``, ``spiral``,
``columns``, ``rows``, ``monocle``, ``centered`` (auto tiling), ``grid``,
``table``, ``float`` and docking (a property of windows, applied to any engine).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple


class Rect(NamedTuple):
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    def contains(self, px, py):
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def shrink(self, l=0, t=0, r=0, b=0):
        return Rect(self.x + l, self.y + t, max(0, self.w - l - r), max(0, self.h - t - b))

    def intersect(self, o: "Rect") -> "Rect":
        x1, y1 = max(self.x, o.x), max(self.y, o.y)
        x2, y2 = min(self.x2, o.x2), min(self.y2, o.y2)
        return Rect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))


AUTO_MODES = ("master", "spiral", "columns", "rows", "monocle", "centered")
ENGINE_NAMES = ("tile",) + AUTO_MODES + ("grid", "table", "float")


def split_sizes(total: int, weights: Sequence[float], gap: int = 0, minimum: int = 1) -> List[int]:
    """Divide ``total`` cells according to ``weights`` (integers, sum == total - gaps)."""
    n = len(weights)
    if n == 0:
        return []
    avail = max(0, total - gap * (n - 1))
    wsum = float(sum(weights)) or 1.0
    sizes = [max(minimum if avail >= minimum * n else 0, int(avail * w / wsum)) for w in weights]
    diff = avail - sum(sizes)
    # distribute the remainder (or take back an overshoot) starting from the last entries
    i = n - 1
    guard = 0
    while diff != 0 and guard < 10000:
        guard += 1
        if diff > 0:
            sizes[i] += 1
            diff -= 1
        else:
            if sizes[i] > minimum:
                sizes[i] -= 1
                diff += 1
        i = (i - 1) % n
    return sizes


# ----------------------------------------------------------------- tile tree
class Tile:
    """Node of the recursive tiling tree.

    ``split`` is 'h' (children side by side) or 'v' (children stacked) for
    inner nodes; leaves carry a window id.
    """

    def __init__(self, wid: Optional[int] = None, split: Optional[str] = None,
                 children: Optional[List["Tile"]] = None, weights: Optional[List[float]] = None):
        self.wid = wid
        self.split = split
        self.children: List[Tile] = children or []
        self.weights: List[float] = weights or [1.0] * len(self.children)
        self.parent: Optional[Tile] = None
        for c in self.children:
            c.parent = self

    @property
    def leaf(self) -> bool:
        return self.wid is not None

    def leaves(self) -> List[int]:
        if self.leaf:
            return [self.wid]
        out = []
        for c in self.children:
            out.extend(c.leaves())
        return out

    def find(self, wid: int) -> Optional["Tile"]:
        if self.leaf:
            return self if self.wid == wid else None
        for c in self.children:
            r = c.find(wid)
            if r:
                return r
        return None

    def to_dict(self) -> dict:
        if self.leaf:
            return {"window": self.wid}
        return {"split": self.split, "weights": self.weights,
                "children": [c.to_dict() for c in self.children]}

    @staticmethod
    def from_dict(d: dict, resolve=lambda x: x) -> "Tile":
        if "window" in d:
            return Tile(wid=resolve(d["window"]))
        kids = [Tile.from_dict(c, resolve) for c in d.get("children", [])]
        w = d.get("weights") or [1.0] * len(kids)
        if len(w) != len(kids):
            w = [1.0] * len(kids)
        return Tile(split=d.get("split", "h"), children=kids, weights=list(w))


class TileTree:
    def __init__(self, root: Optional[Tile] = None):
        self.root = root

    def leaves(self) -> List[int]:
        return self.root.leaves() if self.root else []

    def __contains__(self, wid):
        return bool(self.root and self.root.find(wid))

    def insert(self, new: int, target: Optional[int] = None, direction: Optional[str] = None,
               before: bool = False, area: Optional[Rect] = None):
        """Insert leaf ``new`` by splitting ``target``.

        direction: 'h' (side by side) or 'v' (stacked); default picks by the
        target's shape (wider than tall x2 -> 'h').
        """
        if self.root is None:
            self.root = Tile(wid=new)
            return
        node = self.root.find(target) if target is not None else None
        if node is None:
            leaves = self.root.leaves()
            node = self.root.find(leaves[-1])
        if direction not in ("h", "v"):
            direction = "h"
        newleaf = Tile(wid=new)
        parent = node.parent
        if parent is not None and parent.split == direction:
            idx = parent.children.index(node)
            pos = idx if before else idx + 1
            w = parent.weights[idx]
            parent.weights[idx] = w / 2.0
            parent.children.insert(pos, newleaf)
            parent.weights.insert(pos, w / 2.0)
            newleaf.parent = parent
            return
        inner = Tile(split=direction)
        kids = [newleaf, node] if before else [node, newleaf]
        inner.children = kids
        inner.weights = [1.0, 1.0]
        if parent is None:
            self.root = inner
            inner.parent = None
        else:
            idx = parent.children.index(node)
            parent.children[idx] = inner
            inner.parent = parent
        for k in kids:
            k.parent = inner

    def remove(self, wid: int):
        node = self.root.find(wid) if self.root else None
        if node is None:
            return
        parent = node.parent
        if parent is None:
            self.root = None
            return
        idx = parent.children.index(node)
        del parent.children[idx]
        del parent.weights[idx]
        self._collapse(parent)

    def _collapse(self, node: Tile):
        if len(node.children) == 1:
            only = node.children[0]
            parent = node.parent
            if parent is None:
                self.root = only
                only.parent = None
            else:
                i = parent.children.index(node)
                parent.children[i] = only
                only.parent = parent
                node = parent
        # merge nested same-direction nodes
        cur = node
        while cur is not None:
            merged = True
            while merged:
                merged = False
                for i, c in enumerate(list(cur.children)):
                    if not c.leaf and c.split == cur.split:
                        w = cur.weights[i]
                        tot = sum(c.weights) or 1.0
                        nw = [w * x / tot for x in c.weights]
                        cur.children[i:i + 1] = c.children
                        cur.weights[i:i + 1] = nw
                        for k in c.children:
                            k.parent = cur
                        merged = True
                        break
            cur = cur.parent

    def swap(self, a: int, b: int):
        na, nb = self.root.find(a), self.root.find(b)
        if na and nb:
            na.wid, nb.wid = nb.wid, na.wid

    def rotate(self, wid: int):
        """Toggle the split direction of the parent of ``wid``."""
        node = self.root.find(wid) if self.root else None
        if node and node.parent:
            node.parent.split = "v" if node.parent.split == "h" else "h"

    def resize(self, wid: int, direction: str, delta: float):
        """Grow (delta > 0) or shrink window ``wid`` toward ``direction`` (left/right/up/down)."""
        axis = "h" if direction in ("left", "right") else "v"
        node = self.root.find(wid) if self.root else None
        while node and node.parent:
            p = node.parent
            if p.split == axis:
                idx = p.children.index(node)
                other = idx + 1 if direction in ("right", "down") else idx - 1
                if 0 <= other < len(p.children):
                    tot = p.weights[idx] + p.weights[other]
                    tsum = sum(p.weights)
                    d = delta * tsum
                    new = max(0.1 * tsum, min(tot - 0.1 * tsum, p.weights[idx] + d))
                    p.weights[other] = tot - new
                    p.weights[idx] = new
                    return True
            node = p
        return False

    def set_ratio(self, wid: int, ratio: float):
        node = self.root.find(wid) if self.root else None
        if node and node.parent and 0.05 < ratio < 0.95:
            p = node.parent
            idx = p.children.index(node)
            tot = sum(p.weights)
            rest = tot - p.weights[idx]
            if rest > 0:
                p.weights[idx] = rest * ratio / (1 - ratio)

    def layout(self, area: Rect, gap: int = 0) -> Dict[int, Rect]:
        out: Dict[int, Rect] = {}

        def walk(n: Tile, r: Rect):
            if n.leaf:
                out[n.wid] = r
                return
            if n.split == "h":
                sizes = split_sizes(r.w, n.weights, gap)
                x = r.x
                for c, s in zip(n.children, sizes):
                    walk(c, Rect(x, r.y, s, r.h))
                    x += s + gap
            else:
                sizes = split_sizes(r.h, n.weights, gap)
                y = r.y
                for c, s in zip(n.children, sizes):
                    walk(c, Rect(r.x, y, r.w, s))
                    y += s + gap

        if self.root:
            walk(self.root, area)
        return out

    def to_dict(self):
        return self.root.to_dict() if self.root else None

    @staticmethod
    def from_dict(d, resolve=lambda x: x) -> "TileTree":
        return TileTree(Tile.from_dict(d, resolve) if d else None)

    def sync(self, ids: Sequence[int]):
        """Make the tree contain exactly ``ids`` (add missing, drop stale)."""
        have = set(self.leaves())
        for w in list(have):
            if w not in ids:
                self.remove(w)
        for w in ids:
            if w not in have:
                self.insert(w, direction="h" if len(self.leaves()) % 2 == 1 else "v")


# ------------------------------------------------------------- auto engines
def _grid_dims(n: int, area: Rect, cols: Optional[int] = None) -> Tuple[int, int]:
    if n <= 0:
        return 1, 1
    if cols:
        c = max(1, min(cols, n))
    else:
        aspect = max(0.1, area.w / max(1.0, area.h * 2.0))
        c = max(1, min(n, math.ceil(math.sqrt(n * aspect) - 1e-9)))
    r = math.ceil(n / c)
    return c, r


def layout_grid(ids: Sequence[int], area: Rect, cols: Optional[int] = None, gap: int = 0,
                flow: str = "rows") -> Dict[int, Rect]:
    n = len(ids)
    if n == 0:
        return {}
    c, r = _grid_dims(n, area, cols)
    out: Dict[int, Rect] = {}
    heights = split_sizes(area.h, [1] * r, gap)
    y = area.y
    idx = 0
    for ri in range(r):
        in_row = min(c, n - idx)
        widths = split_sizes(area.w, [1] * in_row, gap)
        x = area.x
        for wi in range(in_row):
            out[ids[idx]] = Rect(x, y, widths[wi], heights[ri])
            x += widths[wi] + gap
            idx += 1
        y += heights[ri] + gap
    return out


def layout_master(ids, area, master_count=1, master_ratio=0.55, gap=0):
    n = len(ids)
    if n == 0:
        return {}
    m = max(1, min(master_count, n))
    if n <= m:
        return layout_stack(ids, area, "v", gap)
    mw = int(area.w * master_ratio)
    mw = max(1, min(area.w - 1, mw))
    left = Rect(area.x, area.y, mw, area.h)
    right = Rect(area.x + mw + gap, area.y, max(1, area.w - mw - gap), area.h)
    out = layout_stack(ids[:m], left, "v", gap)
    out.update(layout_stack(ids[m:], right, "v", gap))
    return out


def layout_stack(ids, area, axis="v", gap=0, weights=None):
    n = len(ids)
    if n == 0:
        return {}
    w = weights or [1] * n
    out = {}
    if axis == "v":
        sizes = split_sizes(area.h, w, gap)
        y = area.y
        for i, s in zip(ids, sizes):
            out[i] = Rect(area.x, y, area.w, s)
            y += s + gap
    else:
        sizes = split_sizes(area.w, w, gap)
        x = area.x
        for i, s in zip(ids, sizes):
            out[i] = Rect(x, area.y, s, area.h)
            x += s + gap
    return out


def layout_spiral(ids, area, ratio=0.5, gap=0):
    out = {}
    r = area
    n = len(ids)
    horizontal = r.w >= r.h * 2
    for i, wid in enumerate(ids):
        if i == n - 1:
            out[wid] = r
            break
        if horizontal:
            w1 = max(1, min(r.w - 1 - gap, int(r.w * ratio)))
            out[wid] = Rect(r.x, r.y, w1, r.h)
            r = Rect(r.x + w1 + gap, r.y, max(1, r.w - w1 - gap), r.h)
        else:
            h1 = max(1, min(r.h - 1 - gap, int(r.h * ratio)))
            out[wid] = Rect(r.x, r.y, r.w, h1)
            r = Rect(r.x, r.y + h1 + gap, r.w, max(1, r.h - h1 - gap))
        horizontal = not horizontal
    return out


def layout_centered(ids, area, master_ratio=0.5, gap=0):
    n = len(ids)
    if n <= 2:
        return layout_master(ids, area, 1, master_ratio, gap)
    mw = max(1, int(area.w * master_ratio))
    side = (area.w - mw - 2 * gap) // 2
    if side < 1:
        return layout_master(ids, area, 1, master_ratio, gap)
    left_ids = ids[1::2]
    right_ids = ids[2::2]
    out = {ids[0]: Rect(area.x + side + gap, area.y, area.w - 2 * side - 2 * gap, area.h)}
    out.update(layout_stack(left_ids, Rect(area.x, area.y, side, area.h), "v", gap))
    out.update(layout_stack(right_ids, Rect(area.x + area.w - side, area.y, side, area.h), "v", gap))
    return out


def layout_table(ids, area, cols=None, rows=None, cells=None, gap=0, names=None):
    """Table layout with column/row weights and explicit cell spans.

    ``cols`` / ``rows``: list of weights (or int for equal count).
    ``cells``: {window_key: (row, col, rowspan, colspan)}; ``names`` maps id -> key.
    """
    n = len(ids)
    if n == 0:
        return {}
    cells = cells or {}
    names = names or {}
    if isinstance(cols, int):
        cols = [1] * cols
    if isinstance(rows, int):
        rows = [1] * rows
    ncols = len(cols) if cols else max(1, math.ceil(math.sqrt(n)))
    cols = list(cols) if cols else [1] * ncols
    occupied = set()
    placed: Dict[int, Tuple[int, int, int, int]] = {}
    for wid in ids:
        key = names.get(wid)
        spec = cells.get(key) if key is not None else None
        if spec:
            r0, c0 = int(spec[0]), int(spec[1])
            rs = int(spec[2]) if len(spec) > 2 else 1
            cs = int(spec[3]) if len(spec) > 3 else 1
            cs = max(1, min(cs, ncols - c0)) if c0 < ncols else 1
            placed[wid] = (r0, min(c0, ncols - 1), max(1, rs), cs)
            for rr in range(r0, r0 + max(1, rs)):
                for cc in range(min(c0, ncols - 1), min(c0, ncols - 1) + cs):
                    occupied.add((rr, cc))
    r = c = 0
    for wid in ids:
        if wid in placed:
            continue
        while (r, c) in occupied:
            c += 1
            if c >= ncols:
                c = 0
                r += 1
        placed[wid] = (r, c, 1, 1)
        occupied.add((r, c))
    nrows = max(p[0] + p[2] for p in placed.values())
    rows = list(rows) if rows else []
    while len(rows) < nrows:
        rows.append(1)
    rows = rows[:max(nrows, len(rows))]
    nrows = len(rows)
    wsz = split_sizes(area.w, cols, gap)
    hsz = split_sizes(area.h, rows, gap)
    xs = [area.x]
    for s in wsz:
        xs.append(xs[-1] + s + gap)
    ys = [area.y]
    for s in hsz:
        ys.append(ys[-1] + s + gap)
    out = {}
    for wid, (r0, c0, rs, cs) in placed.items():
        r1 = min(r0 + rs, nrows)
        c1 = min(c0 + cs, ncols)
        x = xs[c0]
        y = ys[r0]
        w = xs[c1] - gap - x
        h = ys[r1] - gap - y
        out[wid] = Rect(x, y, max(1, w), max(1, h))
    return out


def cascade_rect(index: int, area: Rect) -> Rect:
    w = max(10, int(area.w * 0.6))
    h = max(5, int(area.h * 0.6))
    ox = (index * 3) % max(1, area.w - w + 1)
    oy = (index * 2) % max(1, area.h - h + 1)
    return Rect(area.x + ox, area.y + oy, min(w, area.w), min(h, area.h))


def clamp_rect(r: Rect, area: Rect, min_w=6, min_h=3) -> Rect:
    w = max(min_w, min(r.w, area.w))
    h = max(min_h, min(r.h, area.h))
    x = max(area.x, min(r.x, area.x2 - w))
    y = max(area.y, min(r.y, area.y2 - h))
    return Rect(x, y, w, h)


def carve_docks(docked: Sequence[Tuple[int, str, float]], area: Rect) -> Tuple[Dict[int, Rect], Rect]:
    """Carve docked windows from the edges of ``area``.

    docked: (wid, edge, size); size > 1 -> cells, 0<size<=1 -> fraction of the area.
    Returns (rects, remaining area).
    """
    out: Dict[int, Rect] = {}
    rem = area
    for wid, edge, size in docked:
        if edge in ("left", "right"):
            s = int(size) if size > 1 else int(area.w * size)
            s = max(3, min(s, rem.w - 3)) if rem.w > 6 else rem.w
            if edge == "left":
                out[wid] = Rect(rem.x, rem.y, s, rem.h)
                rem = Rect(rem.x + s, rem.y, rem.w - s, rem.h)
            else:
                out[wid] = Rect(rem.x2 - s, rem.y, s, rem.h)
                rem = Rect(rem.x, rem.y, rem.w - s, rem.h)
        else:
            s = int(size) if size > 1 else int(area.h * size)
            s = max(3, min(s, rem.h - 3)) if rem.h > 6 else rem.h
            if edge == "top":
                out[wid] = Rect(rem.x, rem.y, rem.w, s)
                rem = Rect(rem.x, rem.y + s, rem.w, rem.h - s)
            else:
                out[wid] = Rect(rem.x, rem.y2 - s, rem.w, s)
                rem = Rect(rem.x, rem.y, rem.w, rem.h - s)
    return out, rem


def focus_neighbor(rects: Dict[int, Rect], cur: int, direction: str) -> Optional[int]:
    """Pick the window nearest to ``cur`` in ``direction`` (left/right/up/down)."""
    if cur not in rects:
        return next(iter(rects), None)
    c = rects[cur]
    cx, cy = c.x + c.w / 2.0, c.y + c.h / 2.0
    best, bscore = None, None
    for wid, r in rects.items():
        if wid == cur:
            continue
        rx, ry = r.x + r.w / 2.0, r.y + r.h / 2.0
        if direction == "left":
            ok = r.x2 <= c.x + 1 or rx < cx - 1
            primary, secondary = cx - rx, abs(cy - ry)
            overlap = min(c.y2, r.y2) - max(c.y, r.y)
        elif direction == "right":
            ok = r.x >= c.x2 - 1 or rx > cx + 1
            primary, secondary = rx - cx, abs(cy - ry)
            overlap = min(c.y2, r.y2) - max(c.y, r.y)
        elif direction == "up":
            ok = r.y2 <= c.y + 1 or ry < cy - 1
            primary, secondary = cy - ry, abs(cx - rx)
            overlap = min(c.x2, r.x2) - max(c.x, r.x)
        else:
            ok = r.y >= c.y2 - 1 or ry > cy + 1
            primary, secondary = ry - cy, abs(cx - rx)
            overlap = min(c.x2, r.x2) - max(c.x, r.x)
        if not ok or primary <= 0:
            continue
        score = (0 if overlap > 0 else 1, primary + secondary * 2, secondary)
        if bscore is None or score < bscore:
            best, bscore = wid, score
    return best
