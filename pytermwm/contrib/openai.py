"""Chat with an OpenAI-compatible API (OpenAI, Azure gateways, Ollama, llama.cpp, vLLM, ...) inside a window;
responses stream in token by token.

    plugins:
      - name: openai
        base_url: https://api.openai.com/v1      # e.g. http://localhost:11434/v1 for Ollama
        model: gpt-4o-mini
        api_key_env: OPENAI_API_KEY               # the key is read from this environment variable (or api_key: ...)
        system: "You are a concise terminal assistant."
        max_context: 8000                         # characters of window text sent with --from

Commands:  ai <question...>                      new/current chat window
           ai --from <window> [--last N] <question...>   include another window's output as context ("explain this error")
           ai-window   ai-cancel   ai-clear
Keys in the chat window: Enter send, Esc/C-c cancel generation, C-l clear, PageUp/PageDown scroll, C-y copy last answer to the focused terminal's input.
"""
from __future__ import annotations

import json
import os
import textwrap
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

from pytermwm.builtin_windows import bold, dim
from pytermwm.charts import ansi_color
from pytermwm.commands import CommandError
from pytermwm.prompt import LineEditor
from pytermwm.window import InternalWindow


class ChatClient:
    def __init__(self, base_url: str, model: str, api_key: Optional[str], system: str = "", temperature=None,
                 max_tokens=None, timeout: float = 120.0, extra_headers: Optional[dict] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.system = system
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    def request(self, messages: List[dict]) -> urllib.request.Request:
        body = {"model": self.model, "stream": True, "messages": ([{"role": "system", "content": self.system}] if self.system else []) + messages}
        if self.temperature is not None:
            body["temperature"] = float(self.temperature)
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "pytermwm"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        headers.update(self.extra_headers)
        return urllib.request.Request(self.base_url + "/chat/completions", json.dumps(body).encode(), headers, method="POST")

    def stream(self, messages: List[dict], cancel: threading.Event):
        """Yield content chunks.  Raises RuntimeError with a readable message on failures."""
        try:
            resp = urllib.request.urlopen(self.request(messages), timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
                detail = (j.get("error") or {}).get("message") or ""
            except Exception:
                pass
            raise RuntimeError("HTTP %d %s%s" % (e.code, e.reason, (": " + detail) if detail else ""))
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError("cannot reach %s: %s" % (self.base_url, getattr(e, "reason", e)))
        try:
            for raw in resp:
                if cancel.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    j = json.loads(data)
                except ValueError:
                    continue
                if j.get("error"):
                    raise RuntimeError(str((j["error"] or {}).get("message") or j["error"]))
                for ch in j.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        yield piece
        finally:
            try:
                resp.close()
            except Exception:
                pass


class ChatWindow(InternalWindow):
    kind = "chat"
    refresh_interval = 0.2

    def __init__(self, wid, title, rows, cols, wm=None, plugin=None, **opts):
        super().__init__(wid, title or "chat", rows, cols, **opts)
        self._wm = wm
        self.p = plugin
        self.messages: List[dict] = []           # what is sent to the API
        self.log: List[tuple] = []               # (role, text) shown
        self.editor = LineEditor("")
        self.history: List[str] = []
        self.editor.history = self.history
        self.busy = False
        self.cancel = threading.Event()
        self.scroll_off = 0
        self.error: Optional[str] = None

    # ---------------------------------------------------------------- conversation
    def ask(self, text: str, context: str = ""):
        if self.busy:
            raise CommandError("still answering (Esc cancels)")
        shown = text
        content = text if not context else "%s\n\n---\n%s" % (text, context)
        self.messages.append({"role": "user", "content": content})
        self.log.append(("user", shown + (dim("   [+%d chars of context]" % len(context)) if context else "")))
        self.log.append(("assistant", ""))
        self.busy = True
        self.error = None
        self.cancel = threading.Event()
        cancel = self.cancel
        self.scroll_off = 0
        api = self.p.api

        def work():
            answer = []
            try:
                for piece in self.p.client.stream(list(self.messages), cancel):
                    answer.append(piece)
                    api.call_soon(self._token, piece)
                api.call_soon(self._finish, "".join(answer), None, cancel.is_set())
            except RuntimeError as e:
                api.call_soon(self._finish, "".join(answer), str(e), False)
            except Exception as e:
                api.call_soon(self._finish, "".join(answer), "%s: %s" % (type(e).__name__, e), False)

        api.thread(work)
        self.dirty = True

    def _token(self, piece: str):
        if self.log and self.log[-1][0] == "assistant":
            self.log[-1] = ("assistant", self.log[-1][1] + piece)
        self.dirty = True
        self._wm.dirty = True

    def _finish(self, text: str, error: Optional[str], cancelled: bool):
        self.busy = False
        if text:
            self.messages.append({"role": "assistant", "content": text})
        elif self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()        # nothing came back: drop the unanswered question so retry works
        if error:
            self.error = error
            self.log.append(("error", error))
        elif cancelled:
            self.log.append(("note", "(cancelled)"))
        self.last_output = time.time()
        self.dirty = True
        self._wm.dirty = True
        self._wm.emit("chat_done", window=self, error=error or "")

    def clear(self):
        self.cancel.set()
        self.messages.clear()
        self.log.clear()
        self.busy = False
        self.dirty = True

    def last_answer(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                return m["content"]
        return ""

    def text(self, history=False):
        return "\n\n".join("%s: %s" % (r, t) for r, t in self.log if r in ("user", "assistant", "error"))

    # ---------------------------------------------------------------- rendering
    def render(self, cols, rows):
        width = max(10, cols - 2)
        body: List[str] = []
        if not self.log:
            body += [dim("Ask something below. Enter sends, Esc cancels, C-l clears."),
                     dim("model: %s   endpoint: %s" % (self.p.client.model, self.p.client.base_url))]
        for role, text in self.log:
            if role == "user":
                body.append(ansi_color("you ❯", 6, bold=True))
                for para in text.split("\n"):
                    body.extend("  " + l for l in (textwrap.wrap(para, width - 2) or [""]))
            elif role == "assistant":
                body.append(ansi_color("ai ❯", 2, bold=True) + (dim(" …") if self.busy and text == "" else ""))
                for para in text.split("\n"):
                    body.extend("  " + l for l in (textwrap.wrap(para, width - 2, replace_whitespace=False, drop_whitespace=False) or [""]))
            elif role == "error":
                body.append(ansi_color("error: " + text, 1))
            else:
                body.append(dim(text))
        room = rows - 2
        end = len(body) - self.scroll_off
        view = body[max(0, end - room):max(0, end)]
        out = view + [""] * (room - len(view))
        status = ansi_color("● thinking…", 3) if self.busy else dim("ready")
        out.append(status)
        prompt = "❯ " + self.editor.text
        out.append(prompt[-cols:])
        return out[:rows]

    def cursor_position(self, vw, vh):
        return (min(vw - 1, 2 + self.editor.pos), vh - 1)

    # ---------------------------------------------------------------- keys
    def handle_key(self, key, raw):
        if key == "Esc" or key == "C-c":
            if self.busy:
                self.cancel.set()
                return True
            return False
        if key == "C-l":
            self.clear()
            return True
        if key == "PageUp":
            self.scroll_off += max(1, self.screen.rows - 3)
        elif key == "PageDown":
            self.scroll_off = max(0, self.scroll_off - max(1, self.screen.rows - 3))
        elif key == "C-y":
            self._wm.execute("send-text -t last -- %s" % _quote(self.last_answer()))
        else:
            res = self.editor.handle(key)
            if res == "submit":
                t = self.editor.text.strip()
                self.editor.set("")
                if t:
                    if not self.history or self.history[-1] != t:
                        self.history.append(t)
                    try:
                        self.ask(t)
                    except CommandError as e:
                        self._wm.message(str(e), "warn")
            elif res is None:
                return False
        self.dirty = True
        return True

    def write_input(self, data: bytes):
        # text pushed with send-text / send-keys goes to the input line (Enter submits)
        from pytermwm.keys import KeyParser
        p = KeyParser()
        for ev in p.feed(data) + p.flush():
            if ev.type == "key":
                self.handle_key(ev.name, ev.raw)
            elif ev.type == "paste":
                self.editor.insert(ev.data.replace("\n", " "))
        self.dirty = True


def _quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


class OpenAIPlugin:
    def __init__(self, api):
        cfg = api.config
        self.api = api
        key = cfg.get("api_key") or os.environ.get(str(cfg.get("api_key_env", "OPENAI_API_KEY")), "")
        self.client = ChatClient(str(cfg.get("base_url", "https://api.openai.com/v1")), str(cfg.get("model", "gpt-4o-mini")), key or None,
                                 str(cfg.get("system", "You are a concise assistant living in a terminal window manager. Prefer short answers and plain text.")),
                                 cfg.get("temperature"), cfg.get("max_tokens"), float(cfg.get("timeout", 120)))
        self.max_context = int(cfg.get("max_context", 8000))
        self.window: Optional[ChatWindow] = None


def setup(api):
    p = OpenAIPlugin(api)
    wm = api.wm

    def factory(wm_, wid, spec, rows, cols, opts):
        w = ChatWindow(wid, spec.get("title") or "chat", rows, cols, wm=wm_, plugin=p, name=spec.get("name"), **opts)
        p.window = w
        return w

    api.window_kind("chat", factory)
    api.on_unload(lambda: [w.cancel.set() for w in wm.windows.values() if w.kind == "chat"])

    def get_window(create=True) -> Optional[ChatWindow]:
        w = p.window
        if w is not None and w.id in wm.windows:
            return w
        if not create:
            return None
        return wm.create_window({"kind": "chat", "title": "ai chat"})

    def c_ai(wm_, args):
        src, last, rest, i = None, None, [], 0
        while i < len(args):
            a = args[i]
            if a == "--from" and i + 1 < len(args):
                src = args[i + 1]
                i += 1
            elif a == "--last" and i + 1 < len(args):
                last = int(args[i + 1])
                i += 1
            else:
                rest.append(a)
            i += 1
        w = get_window()
        wm_.focus_window(w.id)
        question = " ".join(rest).strip()
        if not question:
            return {"id": w.id}
        if not p.client.api_key and "localhost" not in p.client.base_url and "127.0.0.1" not in p.client.base_url:
            raise CommandError("no API key: set $%s (or api_key: in the plugin options)" % api.config.get("api_key_env", "OPENAI_API_KEY"))
        context = ""
        if src is not None:
            sw = wm_.resolve_window(src)
            txt = sw.text(history=True)
            if last:
                txt = "\n".join(txt.split("\n")[-last:])
            context = txt[-p.max_context:]
        w.ask(question, context)
        return {"id": w.id}

    def c_cancel(wm_, args):
        w = get_window(False)
        if w is None or not w.busy:
            return "nothing to cancel"
        w.cancel.set()
        return "cancelling"

    def c_clear(wm_, args):
        w = get_window(False)
        if w:
            w.clear()

    api.command("ai", c_ai, usage="ai [--from <window> [--last N]] <question...>", help="Ask the AI (streams into a chat window)",
                completer=lambda wm_, prev, partial: ["--from", "--last"] if not prev else [])
    api.command("ai-window", lambda wm_, a: {"id": get_window().id}, usage="ai-window", help="Open the chat window")
    api.command("ai-cancel", c_cancel, usage="ai-cancel", help="Stop generating")
    api.command("ai-clear", c_clear, usage="ai-clear", help="Forget the conversation")
