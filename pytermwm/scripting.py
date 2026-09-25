"""Automation: declarative rules (config ``rules:``) and python scripts.

Rule example::

    rules:
      - name: fullscreen-on-error
        when: {window: build, output_matches: "ERROR|FAILED (\\w+)"}
        do: ["focus $window", "zoom on", "message build failed: $line", {shell: "notify-send $1"}]
        undo_after: 20          # seconds; restores focus / zoom / layout / desktop as before the rule fired
        cooldown: 5

Triggers (exactly one per rule):  ``output_matches`` (regex on each output line), ``output_changed``,
``idle`` (seconds without output), ``exit`` (true or an exit code / list), ``interval`` (seconds),
``event`` (any WM event name, optionally with ``match: {field: regex}``), ``status`` (status item
name plus ``equals`` / ``matches`` / ``above`` / ``below``).  Filters: ``window`` (name, id, title glob or
``*``), ``desktop``, ``tag``.

Actions: WM command strings (``"layout grid"``), ``"!shell command"`` / ``{shell: ...}``,
``{command: ...}``, ``{python: "code"}`` (variables ``wm`` and ``ctx``), any action may have ``delay: seconds``.
Variables in actions: ``$window $title $name $line $1..$9 $code $event $rule $key $value $time``;
substituted values are shell-quoted so output can never inject commands.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
import shlex

from .compat import new_session_kwargs, quote_shell_arg
import subprocess
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from .commands import CommandError

TRIGGERS = ("output_matches", "output_changed", "idle", "exit", "interval", "event", "status")
RULE_KEYS = {"name", "when", "do", "undo", "undo_after", "cooldown", "enabled", "once", "description"}
WHEN_KEYS = set(TRIGGERS) | {"window", "desktop", "tag", "match", "equals", "matches", "above", "below", "exit_code"}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
_VAR = re.compile(r"\$(\w+)")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def validate_rules(rules) -> List[str]:
    errs: List[str] = []
    if not isinstance(rules, list):
        return ["rules: expected a list"]
    seen = set()
    for i, r in enumerate(rules):
        tag = "rules[%d]" % i
        if not isinstance(r, dict):
            errs.append("%s: expected a mapping" % tag)
            continue
        name = r.get("name")
        if name:
            tag = "rule %r" % name
            if name in seen:
                errs.append("%s: duplicate name" % tag)
            seen.add(name)
        for k in r:
            if k not in RULE_KEYS:
                errs.append("%s: unknown key %r" % (tag, k))
        when = r.get("when")
        if not isinstance(when, dict):
            errs.append("%s: 'when' must be a mapping" % tag)
            continue
        trig = [k for k in when if k in TRIGGERS]
        if len(trig) != 1:
            errs.append("%s: 'when' needs exactly one trigger of %s" % (tag, ", ".join(TRIGGERS)))
        for k in when:
            if k not in WHEN_KEYS:
                errs.append("%s: unknown 'when' key %r" % (tag, k))
        try:
            if "output_matches" in when:
                re.compile(str(when["output_matches"]))
            if "matches" in when:
                re.compile(str(when["matches"]))
            for k, v in (when.get("match") or {}).items():
                re.compile(str(v))
            for k in ("idle", "interval", "above", "below"):
                if k in when:
                    float(when[k])
        except (re.error, ValueError, TypeError, AttributeError) as e:
            errs.append("%s: bad value in 'when': %s" % (tag, e))
        if "status" in when and not any(k in when for k in ("equals", "matches", "above", "below")):
            errs.append("%s: 'status' needs equals, matches, above or below" % tag)
        do = r.get("do")
        if not isinstance(do, list) or not do:
            errs.append("%s: 'do' must be a non-empty list" % tag)
        else:
            for a in do:
                if isinstance(a, str):
                    continue
                if isinstance(a, dict) and any(k in a for k in ("command", "shell", "python")):
                    if "python" in a:
                        try:
                            compile(str(a["python"]), "<rule %s>" % tag, "exec")
                        except SyntaxError as e:
                            errs.append("%s: python action: %s" % (tag, e.msg))
                    continue
                errs.append("%s: bad action %r" % (tag, a))
        for k in ("undo_after", "cooldown"):
            if k in r:
                try:
                    float(r[k])
                except (ValueError, TypeError):
                    errs.append("%s: %s must be a number" % (tag, k))
    return errs


class Rule:
    def __init__(self, spec: dict, index: int):
        self.spec = spec
        self.name = str(spec.get("name") or "rule%d" % (index + 1))
        self.when: dict = spec["when"]
        self.do: list = spec["do"]
        self.undo: list = spec.get("undo") or []
        self.undo_after: Optional[float] = float(spec["undo_after"]) if spec.get("undo_after") else None
        self.cooldown = float(spec.get("cooldown", 0))
        self.enabled = bool(spec.get("enabled", True))
        self.once = bool(spec.get("once", False))
        self.trigger = next(k for k in TRIGGERS if k in self.when)
        self.rx = re.compile(str(self.when["output_matches"])) if "output_matches" in self.when else None
        self.status_rx = re.compile(str(self.when["matches"])) if "matches" in self.when else None
        self.event_match = {k: re.compile(str(v)) for k, v in (self.when.get("match") or {}).items()}
        self.fired = 0
        self.last_fired = 0.0
        self.last_error: Optional[str] = None
        self.last_run = 0.0          # interval bookkeeping
        self.status_state = False
        self.recent: List[float] = []

    def describe(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "trigger": self.trigger, "when": self.when, "do": self.do,
                "fired": self.fired, "last_fired": self.last_fired or None, "error": self.last_error,
                "undo_after": self.undo_after, "cooldown": self.cooldown}


class RuleEngine:
    def __init__(self, wm):
        self.wm = wm
        self.rules: List[Rule] = []
        self.by_name: Dict[str, Rule] = {}
        self.bufs: Dict[int, str] = {}                # partial output line per window
        self.idle_seen: Dict[tuple, float] = {}       # (rule, window id) -> fired for output at this time
        self.undo_jobs: List[dict] = []
        self.delayed: List[tuple] = []                # (due, rule name, action, ctx)
        self._depth = 0
        self._subscribed = False
        self.scripts: Dict[str, dict] = {}
        self.script_paths: List[str] = []
        self._script_scan = 0.0

    # ------------------------------------------------------------ loading
    def load(self, rules: list, scripts: list = ()):
        errs = validate_rules(rules)
        if errs:
            raise CommandError("; ".join(errs))
        old = {r.name: r for r in self.rules}
        new = []
        for i, spec in enumerate(rules):
            r = Rule(spec, i)
            o = old.get(r.name)
            if o is not None:
                r.fired, r.last_fired = o.fired, o.last_fired
                if "enabled" not in spec:
                    r.enabled = o.enabled
            new.append(r)
        self.rules = new
        self.by_name = {r.name: r for r in new}
        for k in [k for k in self.idle_seen if k[0] not in self.by_name]:
            del self.idle_seen[k]
        self._subscribe()
        self.script_paths = [str(s) for s in (scripts or [])]
        self.reload_scripts()

    def _subscribe(self):
        if self._subscribed:
            return
        self._subscribed = True
        self.wm.on("window_output", self._on_output)
        self.wm.on("window_exit", self._on_exit)
        self.wm.on("*", self._on_any)

    def names(self) -> List[str]:
        return list(self.by_name)

    def describe(self) -> List[dict]:
        out = [r.describe() for r in self.rules]
        for path, s in sorted(self.scripts.items()):
            out.append({"name": "script:" + os.path.basename(path), "script": True, "enabled": s.get("error") is None,
                        "error": s.get("error"), "path": path})
        return out

    def set_enabled(self, name: str, on: bool):
        r = self.by_name.get(name)
        if r is None:
            raise CommandError("no such rule: %s" % name)
        r.enabled = on

    def fire(self, name: str, extra: Optional[dict] = None):
        r = self.by_name.get(name)
        if r is None:
            raise CommandError("no such rule: %s" % name)
        ctx = self._base_ctx(r)
        ctx.update(extra or {})
        self._run(r, ctx, force=True)

    # ------------------------------------------------------------ matching helpers
    def _base_ctx(self, r: Rule) -> dict:
        return {"rule": r.name, "time": time.strftime("%H:%M:%S"), "event": "", "window": "", "title": "", "name": "",
                "line": "", "code": "", "key": "", "value": ""}

    def _window_ctx(self, w) -> dict:
        return {"window": w.id, "title": w.title, "name": w.name or ""}

    def _window_ok(self, r: Rule, w) -> bool:
        want = r.when.get("window")
        if want not in (None, "", "*"):
            want = str(want)
            if not (str(w.id) == want or (w.name or "") == want or fnmatch.fnmatchcase(w.title or "", want)
                    or fnmatch.fnmatchcase(w.name or "", want)):
                return False
        if r.when.get("desktop") not in (None, ""):
            d = w.desktop
            dw = str(r.when["desktop"])
            idx = self.wm.desktops.index(d) + 1 if d in self.wm.desktops else 0
            if not (d is not None and (d.name == dw or str(idx) == dw)):
                return False
        if r.when.get("tag") not in (None, "") and (w.opts.get("tag") or "") != str(r.when["tag"]):
            return False
        return True

    # ------------------------------------------------------------ event handlers
    def _on_output(self, window=None, text="", stream="out", **kw):
        if not self.rules or window is None:
            return
        wid = window.id
        interested = [r for r in self.rules if r.enabled and r.trigger in ("output_matches", "output_changed", "idle")]
        if not interested:
            return
        for r in interested:
            if r.trigger == "idle":
                self.idle_seen.pop((r.name, wid), None)      # output again: re-arm the idle rule
        for r in interested:
            if r.trigger == "output_changed" and self._window_ok(r, window):
                ctx = self._base_ctx(r)
                ctx.update(self._window_ctx(window))
                ctx["event"] = "output"
                self._run(r, ctx)
        if not any(r.trigger == "output_matches" for r in interested):
            return
        buf = self.bufs.get(wid, "") + strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
        *lines, rest = buf.split("\n")
        self.bufs[wid] = rest[-4000:]
        for line in lines[-200:]:
            if not line.strip():
                continue
            for r in interested:
                if not r.enabled or r.trigger != "output_matches" or not self._window_ok(r, window):
                    continue
                m = r.rx.search(line)
                if m:
                    ctx = self._base_ctx(r)
                    ctx.update(self._window_ctx(window))
                    ctx["line"] = line
                    ctx["event"] = "output"
                    for i in range(1, 10):
                        try:
                            ctx[str(i)] = m.group(i) or ""
                        except IndexError:
                            ctx[str(i)] = ""
                    ctx["0"] = m.group(0)
                    self._run(r, ctx)

    def _on_exit(self, window=None, code=None, **kw):
        self.bufs.pop(getattr(window, "id", None), None)
        for r in self.rules:
            if not r.enabled or r.trigger != "exit" or window is None or not self._window_ok(r, window):
                continue
            want = r.when["exit"]
            if want in (True, "any", None):
                ok = True
            elif want == "error":
                ok = bool(code)
            elif want == "ok":
                ok = code == 0
            else:
                wl = want if isinstance(want, list) else [want]
                ok = code in [int(x) for x in wl]
            if ok and "exit_code" in r.when:
                ok = code == int(r.when["exit_code"])
            if ok:
                ctx = self._base_ctx(r)
                ctx.update(self._window_ctx(window))
                ctx["code"] = "" if code is None else code
                ctx["event"] = "exit"
                self._run(r, ctx)

    def _on_any(self, event, **kw):
        if event in ("window_output", "tick", "command", "key") or not self.rules:
            return
        for r in self.rules:
            if not r.enabled:
                continue
            if r.trigger == "event" and str(r.when["event"]) == event:
                ctx = self._base_ctx(r)
                ctx["event"] = event
                w = kw.get("window")
                if w is not None and hasattr(w, "id"):
                    if not self._window_ok(r, w):
                        continue
                    ctx.update(self._window_ctx(w))
                for k, v in kw.items():
                    if isinstance(v, (str, int, float, bool)):
                        ctx[k] = v
                    elif hasattr(v, "name") and isinstance(getattr(v, "name"), str) and k not in ctx:
                        ctx[k] = v.name
                if any(not rx.search(str(ctx.get(k, ""))) for k, rx in r.event_match.items()):
                    continue
                self._run(r, ctx)
            elif r.trigger == "status" and event == "status":
                self._check_status(r)

    def _check_status(self, r: Rule):
        key = str(r.when["status"])
        item = self.wm.status_items.get(key)
        cond = False
        val = ""
        if item is not None:
            val = item.get("value")
            sval = str(val)
            w = r.when
            if "equals" in w:
                cond = sval == str(w["equals"])
            elif "matches" in w:
                cond = bool(r.status_rx.search(sval))
            else:
                try:
                    f = float(str(val).rstrip("%"))
                    cond = (("above" not in w or f > float(w["above"])) and ("below" not in w or f < float(w["below"])))
                except ValueError:
                    cond = False
        if cond and not r.status_state:
            ctx = self._base_ctx(r)
            ctx.update({"key": key, "value": val, "event": "status"})
            self._run(r, ctx)
        r.status_state = cond

    # ------------------------------------------------------------ periodic
    def tick(self, now: float):
        for r in self.rules:
            if not r.enabled:
                continue
            if r.trigger == "interval":
                every = max(0.05, float(r.when["interval"]))
                if r.last_run == 0.0:
                    r.last_run = now
                elif now - r.last_run >= every:
                    r.last_run = now
                    ctx = self._base_ctx(r)
                    ctx["event"] = "interval"
                    self._run(r, ctx)
            elif r.trigger == "idle":
                secs = float(r.when["idle"])
                for w in list(self.wm.windows.values()):
                    if not self._window_ok(r, w) or w.exited:
                        continue
                    if (r.name, w.id) in self.idle_seen or not w.last_output:
                        continue
                    if now - w.last_output >= secs:
                        self.idle_seen[(r.name, w.id)] = w.last_output
                        ctx = self._base_ctx(r)
                        ctx.update(self._window_ctx(w))
                        ctx["event"] = "idle"
                        self._run(r, ctx)
            elif r.trigger == "status":
                self._check_status(r)
        if self.delayed:
            due = [d for d in self.delayed if d[0] <= now]
            self.delayed = [d for d in self.delayed if d[0] > now]
            for _, rname, action, ctx in due:
                r = self.by_name.get(rname)
                if r is not None:
                    self._do_action(r, action, ctx)
        if self.undo_jobs:
            due = [j for j in self.undo_jobs if j["due"] <= now]
            self.undo_jobs = [j for j in self.undo_jobs if j["due"] > now]
            for j in due:
                self._undo(j)
        if now - self._script_scan >= 1.0:
            self._script_scan = now
            self._scan_scripts()

    # ------------------------------------------------------------ running
    def _run(self, r: Rule, ctx: dict, force: bool = False):
        now = time.time()
        if not force:
            if r.cooldown and now - r.last_fired < r.cooldown:
                return
            r.recent = [t for t in r.recent if now - t < 1.0]
            if len(r.recent) >= 20:            # runaway protection (a rule reacting to its own output)
                return
            r.recent.append(now)
        if self._depth >= 3:
            return
        r.fired += 1
        r.last_fired = now
        r.last_error = None
        snap = self._snapshot() if (r.undo_after and not force) or (r.undo_after and force) else None
        self._depth += 1
        try:
            self.wm.log.info("rule %s fired", r.name)
            self.wm.emit("rule_fired", rule=r.name)
            for a in r.do:
                delay = float(a.get("delay", 0)) if isinstance(a, dict) else 0
                if delay > 0:
                    self.delayed.append((now + delay, r.name, a, dict(ctx)))
                else:
                    self._do_action(r, a, ctx)
        finally:
            self._depth -= 1
        if r.undo_after:
            self.undo_jobs = [j for j in self.undo_jobs if j["rule"] != r.name]
            self.undo_jobs.append({"rule": r.name, "due": now + r.undo_after, "snap": snap, "ctx": dict(ctx)})
        if r.once:
            r.enabled = False

    def _snapshot(self) -> dict:
        wm = self.wm
        d = wm.desk
        return {"desktop": wm.cur, "focus": d.focus, "zoom": d.zoom, "layout": d.layout, "desk": d}

    def _undo(self, job: dict):
        r = self.by_name.get(job["rule"])
        wm = self.wm
        try:
            if r is not None and r.undo:
                for a in r.undo:
                    self._do_action(r, a, job["ctx"])
                return
            s = job["snap"]
            if not s:
                return
            d = s["desk"]
            if d not in wm.desktops:
                return
            if wm.desk is not d:
                wm.switch_desktop(wm.desktops.index(d) + 1)
            d.zoom = s["zoom"] if s["zoom"] in wm.windows else None
            if d.layout != s["layout"]:
                wm.set_layout(s["layout"]) if hasattr(wm, "set_layout") else wm.run_command_line("layout " + s["layout"])
            wm.relayout()
            if s["focus"] in wm.windows:
                wm.focus_window(s["focus"])
            wm.dirty = True
        except Exception:
            wm.log.error("rule undo failed:\n%s", traceback.format_exc())

    @staticmethod
    def substitute(text: str, ctx: dict, quote: bool = True, shell: bool = False) -> str:
        def rep(m):
            k = m.group(1)
            if k in ctx:
                v = str(ctx[k])
                if not quote:
                    return v
                return quote_shell_arg(v) if shell else shlex.quote(v)
            return m.group(0)
        return _VAR.sub(rep, text)

    def _do_action(self, r: Rule, action, ctx: dict):
        wm = self.wm
        try:
            if isinstance(action, str):
                if action.startswith("!"):
                    self._shell(self.substitute(action[1:], ctx, shell=True))
                else:
                    wm.run_command_line(self.substitute(action, ctx), source="rule")
            elif "command" in action:
                wm.run_command_line(self.substitute(str(action["command"]), ctx), source="rule")
            elif "shell" in action:
                self._shell(self.substitute(str(action["shell"]), ctx, shell=True), window=action.get("window"))
            elif "python" in action:
                ns = {"wm": wm, "ctx": dict(ctx), "log": wm.log}
                exec(compile(str(action["python"]), "<rule %s>" % r.name, "exec"), ns)
        except Exception as e:
            r.last_error = "%s: %s" % (type(e).__name__, e)
            wm.log.error("rule %s action failed:\n%s", r.name, traceback.format_exc())
            wm.message("rule %s: %s" % (r.name, e), "err", 5.0)

    def _shell(self, cmd: str, window=None):
        if window:
            self.wm.create_window({"cmd": cmd, "title": cmd[:40], "keep": True, "focus": False})
            return
        subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=os.getcwd(), **new_session_kwargs())

    # ------------------------------------------------------------ scripts
    def _script_files(self) -> List[str]:
        files: List[str] = []
        for pat in self.script_paths:
            files.extend(sorted(glob.glob(os.path.expanduser(pat))))
        try:
            from .control import scripts_dir
            d = scripts_dir(self.wm)
            if os.path.isdir(d):
                files.extend(os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".py") and not f.startswith("_"))
        except Exception:
            pass
        seen, out = set(), []
        for f in files:
            f = os.path.abspath(f)
            if f not in seen and os.path.isfile(f):
                seen.add(f)
                out.append(f)
        return out

    def _scan_scripts(self):
        files = self._script_files()
        changed = set(files) != set(self.scripts)
        for f in files:
            s = self.scripts.get(f)
            try:
                if s is None or s["mtime"] != os.stat(f).st_mtime:
                    changed = True
            except OSError:
                changed = True
        if changed:
            self.reload_scripts()

    def reload_scripts(self):
        from .plugins import PluginAPI, PluginManager
        wm = self.wm
        for s in self.scripts.values():
            s["api"]._teardown()
        self.scripts = {}
        if wm.plugins is None:
            wm.plugins = PluginManager(wm)
        for f in self._script_files():
            api = PluginAPI(wm.plugins, "script:" + os.path.basename(f), {})
            rec = {"api": api, "mtime": 0.0, "error": None}
            self.scripts[f] = rec
            try:
                rec["mtime"] = os.stat(f).st_mtime
                with open(f, encoding="utf-8") as fh:
                    code = compile(fh.read(), f, "exec")
                ns = {"__name__": "pytermwm_script", "__file__": f, "wm": wm, "api": api}
                exec(code, ns)
                if callable(ns.get("setup")):
                    ns["setup"](api)
            except Exception as e:
                rec["error"] = "%s: %s" % (type(e).__name__, e)
                wm.log.error("script %s failed:\n%s", f, traceback.format_exc())
                wm.message("script %s: %s" % (os.path.basename(f), e), "err", 6.0)
                api._teardown()
        wm.emit("scripts_loaded")
