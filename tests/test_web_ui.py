"""Static checks for the web UI (no browser needed) plus an optional real-browser smoke test."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pytermwm", "web")


def read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


class StaticUi(unittest.TestCase):
    def test_files_exist(self):
        for n in ("index.html", "style.css", "app.js"):
            self.assertTrue(os.path.exists(os.path.join(WEB, n)), n)

    def test_every_id_used_by_js_exists_in_html(self):
        html, js = read("index.html"), read("app.js")
        ids = set(re.findall(r'id="([^"]+)"', html))
        used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"', js)) | set(re.findall(r'getElementById\("([^"+]+)"\)', js))
        missing = sorted(u for u in used if u not in ids)
        self.assertEqual(missing, [])

    def test_tabs_have_sections_and_loaders(self):
        html, js = read("index.html"), read("app.js")
        tabs = re.findall(r'data-tab="([a-z]+)"', html)
        self.assertGreaterEqual(len(tabs), 8)
        for t in tabs:
            self.assertIn('id="tab-%s"' % t, html)
            if t != "screen":
                self.assertIn("loaders.%s" % t, js)

    def test_csp_friendly(self):
        html = read("index.html")
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>\s*\S")      # no inline scripts
        self.assertNotRegex(html, r"\son[a-z]+=")                             # no inline handlers
        self.assertNotIn("innerHTML", read("app.js"))                          # everything via textContent

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_parses(self):
        r = subprocess.run(["node", "--check", os.path.join(WEB, "app.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_served_by_web_server(self):
        import http.client
        from tests.helpers import make_wm
        from pytermwm.api import WebServer
        os.environ["PYTERMWM_RUNTIME_DIR"] = os.environ["PYTERMWM_STATE_DIR"] = tempfile.mkdtemp()
        web = WebServer(make_wm(80, 24), "127.0.0.1", 0, "tok-tok-tok-tok-tok-1", "t-ui")
        web.start()
        try:
            for path, ctype in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")):
                c = http.client.HTTPConnection("127.0.0.1", web.port)
                c.request("GET", path + "?token=tok-tok-tok-tok-tok-1")
                r = c.getresponse()
                body = r.read()
                c.close()
                self.assertEqual(r.status, 200, path)
                self.assertIn(ctype, r.getheader("Content-Type"))
                self.assertGreater(len(body), 500)
        finally:
            web.stop()


def _have_browser():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.environ.get("PTW_CHROMIUM", "/opt/pw-browsers/chromium"))


@unittest.skipUnless(_have_browser(), "playwright + chromium not available")
class BrowserSmoke(unittest.TestCase):
    def test_screen_console_and_config_tabs(self):
        from playwright.sync_api import sync_playwright
        from tests.helpers import make_wm
        from tests.test_api import Pumper
        from pytermwm.api import WebServer
        os.environ["PYTERMWM_RUNTIME_DIR"] = os.environ["PYTERMWM_STATE_DIR"] = tempfile.mkdtemp()
        wm = make_wm(100, 30)
        wm.create_window({"cmd": "echo browser-visible; sleep 60", "title": "b"})
        web = WebServer(wm, "127.0.0.1", 0, "tok-tok-tok-tok-tok-2", "t-br")
        web.start()
        pump = Pumper(wm)
        pump.start()
        errors = []
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=os.environ.get("PTW_CHROMIUM", "/opt/pw-browsers/chromium"), args=["--no-sandbox"])
                pg = b.new_page(viewport={"width": 1200, "height": 800})
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto("http://127.0.0.1:%d/?token=tok-tok-tok-tok-tok-2" % web.port)
                pg.locator("#screen").filter(has_text="browser-visible").wait_for(timeout=8000)
                self.assertNotIn("token=", pg.url)                     # token removed from the address bar
                pg.click("#screen")
                pg.keyboard.type("echo typed-in-browser")
                pg.keyboard.press("Enter")
                pg.locator("#screen").filter(has_text="typed-in-browser").wait_for(timeout=8000)
                pg.click('#tabs button[data-tab="console"]')
                pg.fill("#console-in", "layout grid")
                pg.press("#console-in", "Enter")
                pg.locator("#console-out").filter(has_text="grid").wait_for(timeout=5000)
                pg.click('#tabs button[data-tab="config"]')
                pg.fill("#cfg-text", "theme: [unclosed")
                pg.locator("#cfg-msgs").filter(has_text="YAML").wait_for(timeout=5000)
                self.assertTrue(pg.is_disabled("#cfg-save"))
                b.close()
        finally:
            pump.stop = True
            web.stop()
        self.assertEqual(errors, [])

    def test_fixed_size_terminal_shrinks_font_instead_of_scrolling(self):
        """A window sized 240x67 (as if attached to a real console) must not need a scrollbar in a modest browser window;
        the page is expected to shrink the font instead (see the `fit` checkbox next to the font size input)."""
        from playwright.sync_api import sync_playwright
        from tests.helpers import make_wm
        from tests.test_api import Pumper
        from pytermwm.api import WebServer
        import types
        os.environ["PYTERMWM_RUNTIME_DIR"] = os.environ["PYTERMWM_STATE_DIR"] = tempfile.mkdtemp()
        wm = make_wm(240, 67)
        wm.create_window({"cmd": "echo wide-console; sleep 60", "title": "w"})
        wm.server = types.SimpleNamespace(clients=[types.SimpleNamespace(hello=True, writer=True)])   # simulate an attached tty
        web = WebServer(wm, "127.0.0.1", 0, "tok-tok-tok-tok-tok-3", "t-fit")
        web.start()
        pump = Pumper(wm)
        pump.start()
        errors = []
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=os.environ.get("PTW_CHROMIUM", "/opt/pw-browsers/chromium"), args=["--no-sandbox"])
                pg = b.new_page(viewport={"width": 1000, "height": 600})
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto("http://127.0.0.1:%d/?token=tok-tok-tok-tok-tok-3" % web.port)
                pg.locator("#screen").filter(has_text="wide-console").wait_for(timeout=8000)
                pg.wait_for_function(
                    "() => document.getElementById('font-size').value !== '14'", timeout=8000)   # it had to shrink
                overflow = pg.evaluate(
                    "() => { const w = document.getElementById('screen-wrap'), m = document.querySelector('main');"
                    " return {sw: w.scrollWidth, cw: w.clientWidth, mh: m.scrollHeight, mch: m.clientHeight}; }")
                self.assertLessEqual(overflow["sw"], overflow["cw"], overflow)          # no horizontal scrollbar on the terminal
                self.assertLessEqual(overflow["mh"], overflow["mch"], overflow)         # and no vertical scrollbar on the page (not even a sliver)
                self.assertTrue(pg.is_checked("#font-fit"))
                pg.uncheck("#font-fit")
                pg.fill("#font-size", "14")
                pg.dispatch_event("#font-size", "change")
                pg.wait_for_timeout(400)
                overflow2 = pg.evaluate(
                    "() => { const w = document.getElementById('screen-wrap');"
                    " return {sw: w.scrollWidth, cw: w.clientWidth}; }")
                self.assertGreater(overflow2["sw"], overflow2["cw"], "with fit off, 240 columns at 14px must overflow")
                b.close()
        finally:
            pump.stop = True
            web.stop()
        self.assertEqual(errors, [])

    def test_logs_tab_does_not_reset_scroll_when_nothing_new(self):
        """A person reading older log lines while `follow` stays checked (the default) must not get yanked back to
        the bottom on every 2s refresh tick when nothing new has actually arrived -- only a genuinely new log line
        should move the view."""
        from playwright.sync_api import sync_playwright
        from tests.helpers import make_wm
        from tests.test_api import Pumper
        from pytermwm.api import WebServer
        os.environ["PYTERMWM_RUNTIME_DIR"] = os.environ["PYTERMWM_STATE_DIR"] = tempfile.mkdtemp()
        wm = make_wm(100, 30)
        for i in range(200):
            wm.log.info("seed line %03d" % i)
        web = WebServer(wm, "127.0.0.1", 0, "tok-tok-tok-tok-tok-4", "t-logs")
        web.start()
        pump = Pumper(wm)
        pump.start()
        errors = []
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=os.environ.get("PTW_CHROMIUM", "/opt/pw-browsers/chromium"), args=["--no-sandbox"])
                pg = b.new_page(viewport={"width": 900, "height": 500})
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto("http://127.0.0.1:%d/?token=tok-tok-tok-tok-tok-4" % web.port)
                pg.click('#tabs button[data-tab="logs"]')
                pg.locator("#log-out").filter(has_text="seed line 199").wait_for(timeout=5000)
                self.assertTrue(pg.is_checked("#log-follow"))          # follow stays on by default
                pg.evaluate("() => { document.getElementById('log-out').scrollTop = 0; }")
                pg.wait_for_timeout(2500)                                # past at least one 2s refresh tick, no new logs
                scroll_top = pg.evaluate("() => document.getElementById('log-out').scrollTop")
                self.assertEqual(scroll_top, 0, "unchanged log content must not reset the user's scroll position")
                wm.log.info("seed line 200 (new)")
                pg.locator("#log-out").filter(has_text="seed line 200").wait_for(timeout=5000)
                scroll_top2 = pg.evaluate("() => document.getElementById('log-out').scrollTop")
                self.assertGreater(scroll_top2, 0, "a genuinely new log line with follow checked must still scroll to the end")
                b.close()
        finally:
            pump.stop = True
            web.stop()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
