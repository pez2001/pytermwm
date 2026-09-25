"""Terminal setup/teardown and helpers shared by the attach client and standalone mode.

The platform specific parts (termios raw mode on POSIX, console modes on Windows) live in :mod:`pytermwm.compat`.
"""
from __future__ import annotations

import os

from .colors import BASIC16
from .compat import RawTerminal, StdinReader, term_size, write_all  # noqa: F401  (re-exported)

ENTER = RawTerminal.ENTER
MOUSE_ON = RawTerminal.MOUSE_ON
MOUSE_OFF = RawTerminal.MOUSE_OFF
LEAVE = RawTerminal.LEAVE


def detect_glyphs() -> str:
    """A guess, from this process's own environment, at how safe fractional block/shade characters are to draw.

    ``blocks``: stick to the four classic code page 437 shades (`` `` `░` `▒` `▓` `█` ``), present in
    essentially every terminal font ever made, including the Linux virtual console's built-in font, in place of the
    finer 1/8-cell Unicode block elements (``▁``-``▇``, ``▏``-``▉``) that font is missing -- they show as
    tofu squares there instead. This is a real signal (`TERM=linux` is set only by the kernel's own virtual console) but
    a narrow one: it says nothing about a remote terminal such as PuTTY, which usually reports ``xterm`` regardless of
    what its font actually covers, so that case needs `charts: {glyphs: blocks}` in the config instead."""
    if os.environ.get("TERM") == "linux":
        return "blocks"
    return "unicode"


def console_palette():
    """The Linux virtual console's built-in 16-colour palette is its own (often quite dim/muddy)
    choice of RGB values, unrelated to the ``colors.BASIC16`` values pytermwm assumes when it
    downgrades truecolor theme colours down to 16 colours for a client that can show no more
    (see ``colors.downgrade``). The SGR code is only an index (e.g. "34" for blue): what it
    actually looks like is entirely up to the console's own palette, so two themes whose accents
    both downgrade to "blue" render identically, and a colour picked to look like one thing can
    come out looking like another -- both reported as "colours look wrong / themes look the same".

    The Linux console (and only the Linux console: this is not a standard xterm escape, and other
    terminals may not handle it gracefully) can be told to reprogram its palette with the private
    ``ESC ] P nrrggbb`` sequence, and reset with ``ESC ] R``. Programming it to match
    ``colors.BASIC16`` -- the values pytermwm already assumes -- makes the existing downgrade
    accurate instead of just a reasonable guess. This only makes sense for a real local console
    (``TERM=linux`` is set by the kernel itself, never by a remote client such as PuTTY), and only
    changes something the attach client already restores on exit (see ``RawTerminal``); it can be
    turned off with ``PYTERMWM_CONSOLE_PALETTE=0`` if it ever causes trouble.

    Returns a ``{index: (r, g, b)}`` mapping for :class:`~pytermwm.compat.RawTerminal`, or ``None``."""
    if os.environ.get("TERM") != "linux":
        return None
    if os.environ.get("PYTERMWM_CONSOLE_PALETTE", "1").lower() in ("0", "false", "off", "no"):
        return None
    return dict(enumerate(BASIC16))


_DEPTH_ALIASES = {
    "1": 1, "mono": 1, "monochrome": 1, "bw": 1,
    "4": 4, "16": 4, "16color": 4, "16colors": 4,
    "8": 8, "256": 8, "256color": 8, "256colors": 8,
    "24": 24, "24bit": 24, "true": 24, "truecolor": 24,
}


def detect_depth() -> int:
    # Auto-detection is a guess from environment variables, and it is a poor one for a remote
    # terminal emulator (PuTTY, and others) whose own capability isn't reflected in what it
    # reports over the connection: PuTTY, for instance, usually reports plain "xterm" (no
    # COLORTERM, no "256color" suffix) even though its own 256-colour and often truecolour
    # rendering is perfectly capable, so it gets guessed down to 16 colours here -- the same
    # false-negative problem noted on ``detect_glyphs`` for the same terminal. There is no
    # reliable signal for that case, so ``PYTERMWM_DEPTH`` (1/4/8/24, or mono/16/256/truecolor)
    # lets a person say what their terminal can actually do instead of guessing wrong.
    override = os.environ.get("PYTERMWM_DEPTH", "").strip().lower()
    if override:
        try:
            return _DEPTH_ALIASES[override] if override in _DEPTH_ALIASES else int(override)
        except ValueError:
            pass
    ct = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "")
    if "truecolor" in ct or "24bit" in ct:
        return 24
    if os.name == "nt" and (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") or os.environ.get("ConEmuANSI")):
        return 24                                   # Windows Terminal, VS Code, ConEmu
    if "256" in term or term in ("xterm-kitty", "alacritty", "wezterm", "foot"):
        return 8
    if os.name == "nt" and not term:
        return 8                                    # conhost with VT processing supports 256 colors (truecolor since Win10 1703)
    if term in ("dumb", ""):
        return 1
    return 4
