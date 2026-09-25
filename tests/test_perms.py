import json
import unittest

from tests.helpers import *
from tests.test_api import ApiCase
from pytermwm import perms
from pytermwm.config import validate_config

READ = "read-only-token-0123456789"
AGENT = "agent-token-0123456789ab"


def mcp_call(case, token, name, args=None):
    st, body = case.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": name, "arguments": args or {}}}, token=token)
    return st, body


class ScopedTokenTests(ApiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.wm.cfg["web"] = {"tokens": [{"name": "dash", "token": READ, "scope": "read"},
                                        {"name": "bot", "token": AGENT, "scope": "agent"},
                                        {"name": "short", "token": "tiny", "scope": "read"},
                                        {"name": "bad", "token": "x" * 20, "scope": "root"}]}
        cls.wm.create_window({"cmd": "cat", "name": "tagged", "tag": "agent"})

    def test_invalid_entries_are_ignored(self):
        self.assertEqual(self.req("GET", "/api/state", token="tiny")[0], 401)
        self.assertEqual(self.req("GET", "/api/state", token="x" * 20)[0], 401)

    def test_read_token_can_read(self):
        for path in ("/api/state", "/api/windows", "/api/frame", "/api/window/first/text", "/api/logs", "/api/help/index"):
            st, body = self.req("GET", path, token=READ)
            self.assertEqual(st, 200, path)
            self.assertTrue(body["ok"], path)

    def test_read_token_cannot_write_or_see_config(self):
        for method, path, body in (("POST", "/api/command", {"line": "layout grid"}),
                                   ("POST", "/api/op", {"op": "quit"}),
                                   ("POST", "/api/op", {"op": "detach"}),
                                   ("POST", "/api/window/first/send", {"text": "x"}),
                                   ("POST", "/api/input", {"data": "x"}),
                                   ("GET", "/api/config", None),
                                   ("PUT", "/api/config", {"text": "layout: grid"}),
                                   ("GET", "/api/scripts", None),
                                   ("POST", "/api/op", {"op": "create", "spec": {"cmd": "sh"}}),
                                   ("DELETE", "/api/window/first", None)):
            st, res = self.req(method, path, body, token=READ)
            self.assertEqual(st, 403, (method, path))
            self.assertFalse(res["ok"])
        self.assertNotEqual(self.wm.layout_name if hasattr(self.wm, "layout_name") else "", "grid")

    def test_agent_token_scope(self):
        st, res = self.req("POST", "/api/window/first/send", {"text": "x"}, token=AGENT)
        self.assertEqual(st, 403)
        self.assertIn("agent", res["error"])
        st, res = self.req("POST", "/api/window/tagged/send", {"text": "hi", "enter": True}, token=AGENT)
        self.assertEqual(st, 200, res)
        self.wait_until(lambda: "hi" in self.req("GET", "/api/window/tagged/text", token=AGENT)[1]["text"])
        self.assertEqual(self.req("POST", "/api/command", {"line": "layout grid"}, token=AGENT)[0], 403)
        self.assertEqual(self.req("GET", "/api/config", token=AGENT)[0], 403)
        self.assertEqual(self.req("POST", "/api/op", {"op": "feed", "window": "tagged", "data": "x"}, token=AGENT)[0], 403)
        self.assertEqual(self.req("DELETE", "/api/window/first", token=AGENT)[0], 403)

    def test_agent_created_windows_are_tagged_and_controllable(self):
        st, res = self.req("POST", "/api/op", {"op": "create", "spec": {"cmd": "cat", "name": "mine", "tag": "other"}}, token=AGENT)
        self.assertEqual(st, 200, res)
        self.assertEqual(self.wm.resolve_window("mine").opts["tag"], "agent")
        self.assertEqual(self.req("POST", "/api/window/mine/send", {"text": "z"}, token=AGENT)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/window/mine", token=AGENT)[0], 200)

    def test_mcp_respects_scope(self):
        st, body = mcp_call(self, READ, "list_windows")
        self.assertEqual(st, 200)
        self.assertFalse(body["result"].get("isError"), body)
        st, body = mcp_call(self, READ, "send_keys", {"window": "first", "text": "x"})
        self.assertTrue(body["result"].get("isError"), body)
        st, body = mcp_call(self, READ, "get_config")
        self.assertTrue(body["result"].get("isError"), body)
        st, body = mcp_call(self, AGENT, "run_command", {"line": "layout grid"})
        self.assertTrue(body["result"].get("isError"), body)

    def test_scoped_cookie_does_not_escalate(self):
        r, _ = self.req("GET", "/api/state?token=%s" % READ, token=False, raw=True)
        self.assertEqual(r.status, 200)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn(READ, cookie)
        self.assertNotIn("secret-token", cookie)
        st, _ = self.req("POST", "/api/command", {"line": "layout grid"}, token=False, headers={"Cookie": cookie})
        self.assertEqual(st, 403)

    def test_revocation_is_live(self):
        tokens = self.wm.cfg["web"]["tokens"]
        try:
            self.wm.cfg["web"]["tokens"] = [t for t in tokens if t["token"] != READ]
            self.assertEqual(self.req("GET", "/api/state", token=READ)[0], 401)
        finally:
            self.wm.cfg["web"]["tokens"] = tokens
        self.assertEqual(self.req("GET", "/api/state", token=READ)[0], 200)

    def test_non_ascii_token_is_rejected_cleanly(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=10)
        c.request("GET", "/api/state", headers={"Authorization": "Bearer t\xe9st-token-0123456789"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 401)

    def test_full_token_unchanged(self):
        self.assertEqual(self.req("GET", "/api/config")[0], 200)
        self.assertEqual(self.req("POST", "/api/command", {"line": "message hello"})[0], 200)


class ValidationTests(unittest.TestCase):
    def test_config_validation(self):
        good = {"web": {"tokens": [{"name": "a", "token": "0123456789abcdef", "scope": "read"}]}}
        self.assertEqual(validate_config(good)[0], [])
        for bad in ({"tokens": "x"}, {"tokens": ["x"]}, {"tokens": [{"token": "short", "scope": "read"}]},
                    {"tokens": [{"token": "0123456789abcdef", "scope": "admin"}]},
                    {"tokens": [{"token": "0123456789abcdef", "scope": "read"}, {"token": "0123456789abcdef", "scope": "agent"}]}):
            self.assertTrue(validate_config({"web": bad})[0], bad)

    def test_check_function(self):
        wm = make_wm()
        try:
            wm.create_window({"cmd": "cat", "name": "t", "tag": "agent"})
            wm.create_window({"cmd": "cat", "name": "u"})
            self.assertIsNone(perms.check(None, wm, {"op": "quit"}))
            self.assertIsNone(perms.check("agent", wm, {"op": "send", "window": "t"}))
            self.assertIsNotNone(perms.check("agent", wm, {"op": "send", "window": "u"}))
            self.assertIsNotNone(perms.check("agent", wm, {"op": "send", "window": "nonexistent"}))
            self.assertIsNotNone(perms.check("bogus", wm, {"op": "state"}))
        finally:
            wm.shutdown()


if __name__ == "__main__":
    unittest.main()
