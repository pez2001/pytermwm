"""The attach client (pytermwm/client.py): the terminal-facing side of ``pytermwm attach``.

See also ``tests/test_chart_glyphs.py::AttachHintTests`` for the HELLO-payload glyph hint test,
which predates this file and uses the same "mock the socket, tolerate RawTerminal failing on a
non-tty stdin" pattern.
"""
import unittest
from unittest import mock

from pytermwm import protocol as P


def _decode_all(chunks):
    mb = P.MessageBuffer()
    out = []
    for c in chunks:
        out.extend(mb.feed(c))
    return out


class ResizeAfterRawModeTests(unittest.TestCase):
    """PTW-08x: a user reported the screen looking garbled right after attaching from a terminal
    that is bigger than the one the session was last sized to, needing one manual OS-level resize
    of the terminal application to fix it -- consistent with the terminal's size, as reported by
    ``term_size()`` at the moment HELLO is sent, being stale (some consoles only fully settle their
    reported geometry once something -- entering raw/VT mode, or a real interactive resize -- makes
    them recompute it; see also ``_WinConsole.sync_buffer_to_window`` in ``compat.py`` for the
    matching Windows screen-buffer-vs-window fix). ``attach()`` now re-checks the size immediately
    after entering raw mode and sends a corrective RESIZE right away if it changed, rather than
    waiting for the next SIGWINCH/poll tick."""

    def _run_attach(self, term_sizes, recv=b""):
        """Runs `client.attach()` with the socket and RawTerminal mocked out; term_sizes is the
        sequence `term_size()` returns on successive calls (HELLO time, then right after entering
        raw mode). Returns the decoded (kind, payload) messages passed to `sock.sendall`."""
        sizes = iter(term_sizes)
        with mock.patch("pytermwm.client.detect_glyphs", return_value="unicode"), \
             mock.patch("pytermwm.client.detect_depth", return_value=8), \
             mock.patch("pytermwm.client.term_size", side_effect=lambda fd=1: next(sizes)), \
             mock.patch("pytermwm.protocol.connect") as connect, \
             mock.patch("pytermwm.client.RawTerminal") as raw_terminal_cls, \
             mock.patch("pytermwm.compat.Waker"):
            sock = mock.MagicMock()
            connect.return_value = sock
            sock.recv.return_value = recv
            term = mock.MagicMock()
            term.stdin_reader.return_value.fileno.return_value = -1
            raw_terminal_cls.return_value.__enter__.return_value = term
            from pytermwm import client as client_mod
            try:
                client_mod.attach("t")
            except Exception:
                pass          # the select loop can't run against mocked fds; only sendall matters here
            return _decode_all(c.args[0] for c in sock.sendall.call_args_list)

    def test_sends_a_corrective_resize_when_the_size_changed_after_entering_raw_mode(self):
        msgs = self._run_attach([(80, 24), (120, 30)])
        self.assertEqual(msgs[0][0], P.HELLO)
        self.assertIn(b'"cols": 80, "rows": 24', msgs[0][1])
        self.assertIn((P.RESIZE, b'{"cols": 120, "rows": 30}'), msgs)

    def test_no_extra_resize_when_the_size_did_not_change(self):
        msgs = self._run_attach([(80, 24), (80, 24)])
        self.assertEqual([m for m in msgs if m[0] == P.RESIZE], [])


if __name__ == "__main__":
    unittest.main()
