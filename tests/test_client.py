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


class NestedAttachTests(unittest.TestCase):
    """Running `pytermwm` (or `python ptw.py`) in a shell inside a pytermwm window attached that shell to the very
    session it runs in: the session drew itself into one of its own windows, over and over, until the window manager
    died. Taking over the terminal from inside a window is now refused (the same session always, another one or
    --standalone unless --nested); commands that only talk to the session (run, send, ctl, ...) still work."""

    INSIDE = {"PYTERMWM_SESSION": "work", "PYTERMWM_WINDOW": "3"}

    def env(self, **extra):
        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("PYTERMWM_")}
        env.update(extra)
        return mock.patch.dict("os.environ", env, clear=True)

    def test_rules(self):
        from pytermwm.client import inside_session, nesting_refused
        with self.env():
            self.assertIsNone(inside_session())
            self.assertIsNone(nesting_refused("work"))
            self.assertIsNone(nesting_refused(None))
        with self.env(PYTERMWM_SESSION="work"):            # a chosen default session, not a window
            self.assertIsNone(inside_session())
            self.assertIsNone(nesting_refused("work"))
        with self.env(**self.INSIDE):
            self.assertEqual(inside_session(), "work")
            self.assertIn("inside session 'work'", nesting_refused("work"))
            self.assertIn("inside session 'work'", nesting_refused("work", nested=True))   # never: it cannot work
            self.assertIn("--nested", nesting_refused("other"))
            self.assertIn("--nested", nesting_refused(None))
            self.assertIsNone(nesting_refused("other", nested=True))
            self.assertIsNone(nesting_refused(None, nested=True))

    def test_attach_refuses_before_connecting(self):
        from pytermwm import client
        with self.env(**self.INSIDE), mock.patch.object(P, "connect") as connect, \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(client.attach("work"), 1)
        connect.assert_not_called()
        self.assertIn("pytermwm run/send/ctl", err.getvalue())

    def test_cli_default_command_is_refused_without_starting_anything(self):
        from pytermwm import cli
        for argv in ([], ["attach"], ["--standalone"], ["-s", "other", "attach"]):
            with self.env(**self.INSIDE), mock.patch.object(cli, "start_daemon") as start, \
                    mock.patch("pytermwm.server.run_standalone") as standalone, \
                    mock.patch("sys.stderr", new_callable=__import__("io").StringIO):
                self.assertEqual(cli.main(argv), 1, argv)
            start.assert_not_called()
            standalone.assert_not_called()

    def test_cli_nested_allows_another_session_but_never_this_one(self):
        from pytermwm import cli, client
        with self.env(**self.INSIDE), mock.patch.object(P, "list_sessions", return_value=["work", "other"]), \
                mock.patch.object(client, "attach", return_value=0) as attach, \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO):
            self.assertEqual(cli.main(["--nested", "-s", "other", "attach"]), 0)
            attach.assert_called_once()
            self.assertEqual(cli.main(["--nested", "attach"]), 1)          # "work": the session we are in

    def test_hello_says_where_the_client_runs(self):
        import json
        from pytermwm import client
        sent = []
        sock = mock.MagicMock()
        sock.sendall.side_effect = sent.append
        sock.recv.return_value = b""
        with self.env(**{"PYTERMWM_SESSION": "work", "PYTERMWM_WINDOW": "3"}), \
                mock.patch.object(P, "connect", return_value=sock), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO):
            try:
                client.attach("other", nested=True)
            except Exception:
                pass                                    # RawTerminal needs a real tty; HELLO is sent before that
        hello = [json.loads(p) for k, p in _decode_all(sent) if k == P.HELLO]
        self.assertEqual(hello[0]["inside"], "work")
