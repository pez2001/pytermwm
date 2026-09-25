"""CP437 (IBM PC / BBS era) to unicode conversion, usable on a byte stream."""
from __future__ import annotations

import codecs

# Python's cp437 codec maps 0x00-0x1f to control chars.  BBS art expects the
# graphical glyphs (smileys, card suits, ...) for the ones that are not real
# terminal controls.  We keep the real controls (BEL BS HT LF CR ESC) intact.
_KEEP = {0x00, 0x07, 0x08, 0x09, 0x0A, 0x0D, 0x1B}
_LOW_GLYPHS = "\u0000☺☻♥♦♣♠•◘○◙♂♀♪♫☼" \
              "►◄↕‼¶§▬↨↑↓→←∟↔▲▼"

_TABLE = {}
for _i in range(256):
    if _i < 32:
        _TABLE[_i] = chr(_i) if _i in _KEEP else _LOW_GLYPHS[_i]
    elif _i == 127:
        _TABLE[_i] = "⌂"
    else:
        _TABLE[_i] = bytes([_i]).decode("cp437")

_DECODE_TABLE = {i: c for i, c in _TABLE.items()}


class CP437Decoder:
    """Incremental decoder with the same interface as the codecs decoders.

    ESC sequences (0x1b ...) are passed through unchanged so ANSI art works.
    """

    def __init__(self, errors: str = "replace"):
        self.errors = errors

    def decode(self, data: bytes, final: bool = False) -> str:
        return "".join(_TABLE[b] for b in data)

    def reset(self):
        pass

    def getstate(self):
        return (b"", 0)

    def setstate(self, state):
        pass


def make_decoder(cp437: bool):
    if cp437:
        return CP437Decoder()
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


def cp437_to_utf8(data: bytes) -> str:
    return CP437Decoder().decode(data)
