"""Keyboard/mouse input parsing and key bindings."""
from __future__ import annotations

import codecs
import re
import time
from typing import Callable, Dict, List, Optional, Tuple, Union

# ------------------------------------------------------------------ key names
_CSI_FINAL = {"A": "Up", "B": "Down", "C": "Right", "D": "Left", "H": "Home", "F": "End",
              "P": "F1", "Q": "F2", "R": "F3", "S": "F4", "E": "Begin"}
_TILDE = {1: "Home", 2: "Insert", 3: "Delete", 4: "End", 5: "PageUp", 6: "PageDown",
          7: "Home", 8: "End", 11: "F1", 12: "F2", 13: "F3", 14: "F4", 15: "F5",
          17: "F6", 18: "F7", 19: "F8", 20: "F9", 21: "F10", 23: "F11", 24: "F12"}
_SS3 = {"A": "Up", "B": "Down", "C": "Right", "D": "Left", "H": "Home", "F": "End",
        "P": "F1", "Q": "F2", "R": "F3", "S": "F4"}

_NAME_TO_CSI = {"Up": "A", "Down": "B", "Right": "C", "Left": "D", "Home": "H", "End": "F"}
_NAME_TO_TILDE = {v: k for k, v in _TILDE.items() if k not in (7, 8)}
_F14 = {"F1": "P", "F2": "Q", "F3": "R", "F4": "S"}

SPECIAL_NAMES = {"Enter", "Tab", "S-Tab", "Esc", "Backspace", "Space", "Up", "Down", "Left", "Right",
                 "Home", "End", "PageUp", "PageDown", "Insert", "Delete"} | {"F%d" % i for i in range(1, 13)}


def _mods_from_param(p: int) -> str:
    m = max(0, p - 1)
    s = ""
    if m & 4:
        s += "C-"
    if m & 2:
        s += "M-"
    if m & 1:
        s += "S-"
    return s


class Event:
    __slots__ = ("type", "name", "raw", "data")

    def __init__(self, type_: str, name: str = "", raw: bytes = b"", data=None):
        self.type, self.name, self.raw, self.data = type_, name, raw, data

    def __repr__(self):
        return "Event(%s,%r)" % (self.type, self.name or self.data)


class KeyParser:
    """Incremental parser: bytes -> list of events (key / mouse / paste / focus)."""

    def __init__(self):
        self.buf = b""
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.paste: Optional[List[bytes]] = None
        self.esc_time = 0.0

    def pending(self) -> bool:
        return bool(self.buf)

    def feed(self, data: bytes) -> List[Event]:
        self.buf += data
        return self._parse(final=False)

    def flush(self) -> List[Event]:
        """Call after an idle timeout: a lone ESC becomes an Esc key press."""
        return self._parse(final=True)

    def _parse(self, final: bool) -> List[Event]:
        out: List[Event] = []
        b = self.buf
        i = 0
        n = len(b)
        while i < n:
            if self.paste is not None:
                end = b.find(b"\x1b[201~", i)
                if end < 0:
                    self.paste.append(b[i:])
                    i = n
                    break
                self.paste.append(b[i:end])
                text = b"".join(self.paste).decode("utf-8", "replace")
                out.append(Event("paste", "paste", b"".join(self.paste), text))
                self.paste = None
                i = end + 6
                continue
            c = b[i]
            if c == 0x1B:
                if i + 1 >= n:
                    if final:
                        out.append(Event("key", "Esc", b"\x1b"))
                        i += 1
                    break
                nxt = b[i + 1]
                if nxt == 0x5B:  # CSI
                    j = i + 2
                    while j < n and not (0x40 <= b[j] <= 0x7E):
                        j += 1
                    if j >= n:
                        if final:
                            out.append(Event("key", "M-[", b[i:i + 2]))
                            i += 2
                            continue
                        break
                    seq = b[i:j + 1]
                    ev = self._csi(seq)
                    if ev is None:
                        pass
                    elif ev.type == "paste_start":
                        self.paste = []
                    else:
                        out.append(ev)
                    i = j + 1
                    continue
                if nxt == 0x4F:  # SS3
                    if i + 2 >= n:
                        if final:
                            out.append(Event("key", "M-O", b[i:i + 2]))
                            i += 2
                            continue
                        break
                    ch = chr(b[i + 2])
                    name = _SS3.get(ch)
                    out.append(Event("key", name or ("M-O" + ch), b[i:i + 3]))
                    i += 3
                    continue
                if nxt == 0x1B:
                    # ESC ESC ... : Esc followed by something; emit Esc for the first
                    out.append(Event("key", "Esc", b"\x1b"))
                    i += 1
                    continue
                # ESC + char = Alt+char
                j = i + 1
                ev, used = self._plain(b, j, meta=True)
                if ev is None:
                    if final:
                        out.append(Event("key", "Esc", b"\x1b"))
                        i += 1
                        continue
                    break
                ev.raw = b[i:j + used]
                out.append(ev)
                i = j + used
                continue
            ev, used = self._plain(b, i)
            if ev is None:
                break
            out.append(ev)
            i += used
        self.buf = b[i:]
        return out

    def _plain(self, b: bytes, i: int, meta: bool = False):
        c = b[i]
        pre = "M-" if meta else ""
        if c == 0x0D:
            return Event("key", pre + "Enter", b[i:i + 1]), 1
        if c == 0x0A:
            return Event("key", pre + "C-j", b[i:i + 1]), 1
        if c == 0x09:
            return Event("key", pre + "Tab", b[i:i + 1]), 1
        if c == 0x7F:
            return Event("key", pre + "Backspace", b[i:i + 1]), 1
        if c == 0x00:
            return Event("key", pre + "C-Space", b[i:i + 1]), 1
        if c == 0x20:
            return Event("key", pre + "Space", b[i:i + 1]), 1
        if c < 0x20:
            if c <= 0x1A:
                return Event("key", pre + "C-" + chr(c + 96), b[i:i + 1]), 1
            return Event("key", pre + "C-" + "\\]^_"[c - 0x1C], b[i:i + 1]), 1
        if c < 0x80:
            return Event("key", pre + chr(c), b[i:i + 1]), 1
        # utf-8 sequence
        if c >= 0xF0:
            need = 4
        elif c >= 0xE0:
            need = 3
        elif c >= 0xC0:
            need = 2
        else:
            return Event("key", pre + "?", b[i:i + 1]), 1
        if i + need > len(b):
            return None, 0
        try:
            ch = b[i:i + need].decode("utf-8")
        except UnicodeDecodeError:
            return Event("key", pre + "?", b[i:i + 1]), 1
        return Event("key", pre + ch, b[i:i + need]), need

    def _csi(self, seq: bytes) -> Optional[Event]:
        body = seq[2:-1].decode("ascii", "replace")
        final = chr(seq[-1])
        if body.startswith("<") and final in "Mm":
            try:
                bt, x, y = [int(v) for v in body[1:].split(";")]
            except ValueError:
                return None
            mods = ""
            if bt & 4:
                mods += "S-"
            if bt & 8:
                mods += "M-"
            if bt & 16:
                mods += "C-"
            motion = bool(bt & 32)
            base = bt & ~(4 | 8 | 16 | 32)
            if base >= 64:
                kind = "wheelup" if base == 64 else "wheeldown" if base == 65 else "wheel%d" % base
                button = 0
            else:
                button = base + 1 if base < 3 else 0
                kind = "move" if motion else ("release" if final == "m" else "press")
            return Event("mouse", "Mouse", seq, {"kind": kind, "button": button, "x": x - 1, "y": y - 1,
                                                 "mods": mods, "bt": bt, "final": final})
        if final == "I" and not body:
            return Event("focus", "FocusIn", seq, True)
        if final == "O" and not body:
            return Event("focus", "FocusOut", seq, False)
        if final == "~" and body == "200":
            return Event("paste_start")
        if final == "~" and body == "201":
            return None
        parts = body.split(";") if body else []
        try:
            nums = [int(p.split(":")[0]) if p else 0 for p in parts]
        except ValueError:
            return Event("key", "?", seq)
        if final == "Z":
            return Event("key", "S-Tab", seq)
        if final == "u":     # kitty / fixterms
            code = nums[0] if nums else 0
            mods = _mods_from_param(nums[1]) if len(nums) > 1 else ""
            names = {13: "Enter", 9: "Tab", 27: "Esc", 127: "Backspace", 32: "Space"}
            base = names.get(code, chr(code) if 32 < code < 0x110000 else "?")
            return Event("key", mods + base, seq)
        if final == "~":
            code = nums[0] if nums else 0
            name = _TILDE.get(code)
            if name is None:
                return Event("key", "?", seq)
            mods = _mods_from_param(nums[1]) if len(nums) > 1 else ""
            return Event("key", mods + name, seq)
        if final in _CSI_FINAL:
            mods = _mods_from_param(nums[1]) if len(nums) > 1 else ""
            return Event("key", mods + _CSI_FINAL[final], seq)
        return Event("key", "?", seq)


# ------------------------------------------------------------------ names -> bytes
def key_to_bytes(name: str, app_cursor: bool = False) -> Optional[bytes]:
    """Translate a key name (as produced by the parser) into bytes for a program."""
    mods = ""
    base = name
    while len(base) > 2 and base[1] == "-" and base[0] in "CMS":
        mods += base[0]
        base = base[2:]
    ctrl, meta, shift = "C" in mods, "M" in mods, "S" in mods
    seq: Optional[bytes] = None
    if base == "Enter":
        seq = b"\r"
    elif base == "Tab":
        seq = b"\x1b[Z" if shift else b"\t"
        shift = False
    elif base == "Esc":
        seq = b"\x1b"
    elif base == "Backspace":
        seq = b"\x7f"
    elif base == "Space":
        seq = b"\x00" if ctrl else b" "
        ctrl = False
    elif base in _NAME_TO_CSI:
        code = _NAME_TO_CSI[base]
        m = 1 + (1 if shift else 0) + (2 if meta else 0) + (4 if ctrl else 0)
        if m > 1:
            return b"\x1b[1;%d%s" % (m, code.encode())
        return (b"\x1bO" if app_cursor else b"\x1b[") + code.encode()
    elif base in _NAME_TO_TILDE:
        code = _NAME_TO_TILDE[base]
        m = 1 + (1 if shift else 0) + (2 if meta else 0) + (4 if ctrl else 0)
        if m > 1:
            return b"\x1b[%d;%d~" % (code, m)
        return b"\x1b[%d~" % code
    elif base in _F14:
        m = 1 + (1 if shift else 0) + (2 if meta else 0) + (4 if ctrl else 0)
        if m > 1:
            return b"\x1b[1;%d%s" % (m, _F14[base].encode())
        return b"\x1bO" + _F14[base].encode()
    elif len(base) == 1 or (len(base) > 1 and base not in SPECIAL_NAMES and len(base.encode()) <= 4 and len(base) == 1):
        ch = base
        if ctrl:
            o = ord(ch.lower())
            if 97 <= o <= 122:
                seq = bytes([o - 96])
            elif ch in "@[\\]^_":
                seq = bytes([ord(ch) & 0x1F])
            elif ch == "?":
                seq = b"\x7f"
            else:
                seq = ch.encode()
            ctrl = False
        else:
            seq = ch.encode("utf-8")
    if seq is None:
        return None
    if meta and not base.startswith("F") and base not in _NAME_TO_CSI:
        seq = b"\x1b" + seq
    return seq


def keys_to_bytes(tokens, app_cursor: bool = False) -> bytes:
    """Convert a list (or whitespace separated string) of key names / literal text."""
    if isinstance(tokens, str):
        tokens = tokens.split(" ") if tokens else []
    out = b""
    for t in tokens:
        b = key_to_bytes(t, app_cursor) if (t in SPECIAL_NAMES or re.match(r"^([CMS]-)+.+$", t)) else None
        out += b if b is not None else t.encode("utf-8")
    return out


# ------------------------------------------------------------------ keymaps
DEFAULT_KEYS = {
    "prefix": "C-b",
    "prefix_timeout": 2.0,
    "direct": {
        "M-Enter": "new-window",
        "M-h": "focus left", "M-j": "focus down", "M-k": "focus up", "M-l": "focus right",
        "M-Left": "focus left", "M-Down": "focus down", "M-Up": "focus up", "M-Right": "focus right",
        "M-H": "move left", "M-J": "move down", "M-K": "move up", "M-L": "move right",
        "M-Tab": "focus next", "M-`": "focus last",
        "M-q": "close-window", "M-f": "zoom", "M-t": "float toggle", "M-Space": "layout next",
        "M-\\": "split h", "M--": "split v", "M-r": "mode resize", "M-[": "mode scroll",
        "M-p": "palette", "M-:": "prompt", "M-;": "prompt", "M-/": "help", "M-?": "help",
        "M-n": "desktop new", "M-,": "desktop prev", "M-.": "desktop next",
        "M-PageUp": "scroll page-up", "M-PageDown": "scroll page-down",
        "M-c": "window-set cp437 toggle", "M-z": "layout zoom-master",
        "M-y": "copy-mode", "M-Y": "copy-view", "M-v": "paste",
        "M-1": "desktop 1", "M-2": "desktop 2", "M-3": "desktop 3", "M-4": "desktop 4",
        "M-5": "desktop 5", "M-6": "desktop 6", "M-7": "desktop 7", "M-8": "desktop 8", "M-9": "desktop 9",
        "M-!": "send-to-desktop 1", "M-@": "send-to-desktop 2", "M-#": "send-to-desktop 3",
        "M-$": "send-to-desktop 4", "M-%": "send-to-desktop 5", "M-^": "send-to-desktop 6",
        "M-&": "send-to-desktop 7", "M-*": "send-to-desktop 8", "M-(": "send-to-desktop 9",
    },
    "prefix_table": {
        "c": "new-window", "n": "focus next", "p": "focus prev", "x": "close-window", "z": "zoom",
        "|": "split h", "%": "split h", "-": "split v", "\"": "split v",
        "Space": "layout next", "d": "detach", ":": "prompt", ",": "prompt rename ",
        "[": "mode scroll", "y": "copy-mode", "Y": "copy-view", "]": "paste", "?": "help", "l": "log", "t": "float toggle", "o": "focus next",
        "Left": "focus left", "Right": "focus right", "Up": "focus up", "Down": "focus down",
        "0": "desktop 10", "1": "desktop 1", "2": "desktop 2", "3": "desktop 3", "4": "desktop 4",
        "5": "desktop 5", "6": "desktop 6", "7": "desktop 7", "8": "desktop 8", "9": "desktop 9",
        "C": "desktop new", "&": "close-desktop", "w": "palette windows", "s": "palette sessions",
        "r": "mode resize", "m": "mode move", "T": "theme next", "e": "cp437 toggle", "q": "quit-dialog",
        "D": "debug", "S": "session-save",
    },
    "modes": {
        "resize": {
            "h": "resize left", "j": "resize down", "k": "resize up", "l": "resize right",
            "Left": "resize left", "Down": "resize down", "Up": "resize up", "Right": "resize right",
            "H": "resize left 5", "J": "resize down 5", "K": "resize up 5", "L": "resize right 5",
            "=": "layout balance", "Esc": "mode normal", "q": "mode normal", "Enter": "mode normal",
        },
        "move": {
            "h": "float-move -2 0", "l": "float-move 2 0", "k": "float-move 0 -1", "j": "float-move 0 1",
            "Left": "float-move -2 0", "Right": "float-move 2 0", "Up": "float-move 0 -1", "Down": "float-move 0 1",
            "H": "float-move -8 0", "L": "float-move 8 0", "K": "float-move 0 -4", "J": "float-move 0 4",
            "Esc": "mode normal", "q": "mode normal", "Enter": "mode normal",
        },
        "scroll": {
            "k": "scroll up", "j": "scroll down", "Up": "scroll up", "Down": "scroll down",
            "PageUp": "scroll page-up", "PageDown": "scroll page-down", "C-u": "scroll page-up",
            "C-d": "scroll page-down", "g": "scroll top", "G": "scroll bottom", "Home": "scroll top",
            "End": "scroll bottom", "h": "scroll left", "l": "scroll right",
            "/": "prompt search ", "Esc": "mode normal", "q": "mode normal", "v": "copy-mode",
        },
        "copy": {
            "h": "copy-move left", "j": "copy-move down", "k": "copy-move up", "l": "copy-move right",
            "Left": "copy-move left", "Down": "copy-move down", "Up": "copy-move up", "Right": "copy-move right",
            "PageUp": "copy-move page-up", "PageDown": "copy-move page-down", "C-u": "copy-move page-up",
            "C-d": "copy-move page-down", "g": "copy-move top", "G": "copy-move bottom", "Home": "copy-move home",
            "End": "copy-move end", "0": "copy-move home", "$": "copy-move end", "^": "copy-move first",
            "w": "copy-move word-next", "b": "copy-move word-prev",
            "v": "copy-select char", "Space": "copy-select char", "V": "copy-select line", "C-v": "copy-select rect",
            "y": "copy-yank", "Enter": "copy-yank", "Esc": "copy-cancel", "q": "copy-cancel",
        },
    },
}


class Keymap:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = {}
        self.mode = "normal"
        self.prefix_active = False
        self.prefix_time = 0.0
        self.set_config(cfg or {})

    def set_config(self, cfg: dict):
        merged = {
            "prefix": DEFAULT_KEYS["prefix"],
            "prefix_timeout": DEFAULT_KEYS["prefix_timeout"],
            "direct": dict(DEFAULT_KEYS["direct"]),
            "prefix_table": dict(DEFAULT_KEYS["prefix_table"]),
            "modes": {k: dict(v) for k, v in DEFAULT_KEYS["modes"].items()},
        }
        self.user_direct = dict((cfg or {}).get("direct") or {})
        if cfg:
            if "prefix" in cfg:
                merged["prefix"] = cfg["prefix"]
            if "prefix_timeout" in cfg:
                merged["prefix_timeout"] = float(cfg["prefix_timeout"])
            replace = cfg.get("replace_defaults", False)
            for sect in ("direct", "prefix_table"):
                if sect in cfg:
                    if replace:
                        merged[sect] = {}
                    merged[sect].update(cfg[sect] or {})
            for mname, tbl in (cfg.get("modes") or {}).items():
                merged["modes"].setdefault(mname, {}).update(tbl or {})
        for sect in ("direct", "prefix_table"):
            merged[sect] = {k: v for k, v in merged[sect].items() if v not in (None, "", "none")}
        self.cfg = merged
        self.mode = "normal" if self.mode not in merged["modes"] else self.mode

    @property
    def prefix(self) -> Optional[str]:
        return self.cfg["prefix"] or None

    def reset(self):
        self.mode = "normal"
        self.prefix_active = False

    def lookup(self, key: str, now: Optional[float] = None) -> Tuple[Optional[Union[str, list]], bool]:
        """Return (action, consumed).  consumed False => pass the key through to the window."""
        now = time.time() if now is None else now
        if self.mode != "normal":
            tbl = self.cfg["modes"].get(self.mode, {})
            act = tbl.get(key)
            if act is not None:
                return act, True
            return None, True    # sticky modes swallow unbound keys
        if self.prefix_active:
            if now - self.prefix_time > self.cfg["prefix_timeout"]:
                self.prefix_active = False
            else:
                self.prefix_active = False
                act = self.cfg["prefix_table"].get(key)
                if act is not None:
                    return act, True
                if key == self.prefix:
                    return None, False     # C-b C-b sends a literal C-b
                return None, True          # unknown key after prefix: swallowed
        if self.prefix and key == self.prefix:
            self.prefix_active = True
            self.prefix_time = now
            return "__prefix__", True
        act = self.cfg["direct"].get(key)
        if act is not None:
            return act, True
        return None, False

    def bindings(self):
        out = []
        for k, v in self.cfg["direct"].items():
            out.append(("direct", k, v))
        for k, v in self.cfg["prefix_table"].items():
            out.append(("prefix", self.prefix + " " + k, v))
        for m, t in self.cfg["modes"].items():
            for k, v in t.items():
                out.append(("mode:" + m, k, v))
        return out
