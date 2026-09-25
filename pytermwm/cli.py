"""Command line interface: ``pytermwm``."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import List, Optional

from . import __version__
from . import compat
from . import protocol as P


def _session_name(args) -> str:
    return getattr(args, "session", None) or os.environ.get("PYTERMWM_SESSION") or "default"


def _control(args) -> P.ControlClient:
    explicit = getattr(args, "session", None)
    try:
        if explicit or not os.environ.get("PYTERMWM_SOCK"):
            return P.ControlClient(session=_session_name(args))
        return P.ControlClient(path=os.environ["PYTERMWM_SOCK"])
    except (FileNotFoundError, ConnectionRefusedError):
        sys.stderr.write("pytermwm: no running session %r (start one with `pytermwm start`)\n" % _session_name(args))
        raise SystemExit(1)


def _request(args, req: dict, timeout: float = 30.0) -> dict:
    c = _control(args)
    try:
        res = c.request(req, timeout)
    finally:
        c.close()
    return res


def _print_result(res: dict) -> int:
    if not res.get("ok"):
        sys.stderr.write("error: %s\n" % res.get("error"))
        return 1
    r = res.get("result")
    if r is None:
        return 0
    if isinstance(r, str):
        print(r)
    else:
        print(json.dumps(r, indent=2, default=str))
    return 0


# ------------------------------------------------------------------ daemon start
def start_daemon(session: str, config: Optional[str], web: Optional[str] = None, restore: Optional[str] = None,
                 debug: bool = False, wait: float = 8.0, cwd: Optional[str] = None) -> bool:
    if session in P.list_sessions():
        return True
    logdir = os.path.join(P.state_dir(), "logs")
    os.makedirs(logdir, exist_ok=True)
    logf = open(os.path.join(logdir, "%s.daemon.log" % session), "ab")
    cmd = [sys.executable, "-m", "pytermwm", "-s", session]
    if config:
        cmd += ["-c", config]
    if web is not None:
        cmd += ["--web=%s" % web]
    if debug:
        cmd += ["--debug"]
    cmd += ["server"]
    if restore:
        cmd += ["--restore", restore]
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, env=env, cwd=cwd or os.getcwd(), **compat.detached_kwargs())
    end = time.time() + wait
    while time.time() < end:
        if session in P.list_sessions(cleanup=False):
            return True
        time.sleep(0.05)
    sys.stderr.write("pytermwm: the server did not start; see %s\n" % logf.name)
    try:
        with open(logf.name, encoding="utf-8") as f:
            sys.stderr.write("".join(f.readlines()[-15:]))
    except OSError:
        pass
    return False


def _web_arg(v) -> Optional[dict]:
    if v is None:
        return None
    if v is True or v == "":
        return {"enabled": True}
    m = re.match(r"^(?:(.*):)?(\d+)$", str(v))
    if not m:
        raise SystemExit("--web expects [HOST:]PORT")
    d = {"enabled": True, "port": int(m.group(2))}
    if m.group(1):
        d["host"] = m.group(1)
    return d


def _find_config(args) -> Optional[str]:
    if getattr(args, "config", None):
        return os.path.abspath(os.path.expanduser(args.config))
    from .config import find_config
    return find_config()


# ------------------------------------------------------------------ subcommands
def cmd_server(args) -> int:
    from .server import Server
    from .terminal import term_size
    web = None
    if args.web is not None:
        web = _web_arg(args.web)
    srv = Server(_session_name(args), _find_config(args), 80, 24, standalone=False, restore_path=args.restore, web=web,
                 debug=args.debug, log_stderr=args.log_stderr)
    try:
        srv.setup()
    except Exception as e:
        sys.stderr.write("pytermwm server: %s\n" % e)
        return 2
    srv.run()
    return 0


def cmd_attach(args) -> int:
    from .client import attach, nesting_refused
    session = _session_name(args)
    why = nesting_refused(None if args.standalone else session, args.nested)
    if why:                                   # before a daemon is started for nothing
        sys.stderr.write(why + "\n")
        return 1
    if args.standalone:
        from .server import run_standalone
        return run_standalone(session, _find_config(args), args.debug, _web_arg(args.web))
    if session not in P.list_sessions():
        if getattr(args, "no_create", False):
            sys.stderr.write("pytermwm: no session %r\n" % session)
            return 1
        if not start_daemon(session, _find_config(args), args.web if args.web is not None else None, debug=args.debug):
            return 1
    return attach(session, mouse=not args.no_mouse, nested=args.nested)


def cmd_start(args) -> int:
    session = _session_name(args)
    if session in P.list_sessions():
        print("session %r is already running" % session)
        return 0
    ok = start_daemon(session, _find_config(args), args.web, args.restore, args.debug)
    if ok:
        print("session %r started (attach with: pytermwm attach -s %s)" % (session, session))
    return 0 if ok else 1


def cmd_up(args) -> int:
    """Build a workspace from .pytermwm.yaml in a session named after the project, then attach."""
    from . import project
    from .config import ConfigError
    path = os.path.abspath(os.path.expanduser(args.file)) if args.file else project.find_project_file()
    if not path or not os.path.isfile(path):
        sys.stderr.write("pytermwm up: no project file (.pytermwm.yaml) found in %s or its parents\n" % os.getcwd())
        return 1
    try:
        doc = project.load(path)
    except ConfigError as e:
        sys.stderr.write("pytermwm up: %s\n" % e)
        return 1
    session = getattr(args, "session", None) or os.environ.get("PYTERMWM_SESSION") or project.session_name(doc, path)
    args.session = session
    was_running = session in P.list_sessions()
    if not start_daemon(session, _find_config(args), args.web, debug=args.debug, cwd=os.path.dirname(path)):
        return 1
    res = _request(args, {"op": "command", "line": "up %s%s" % ("" if was_running else "--fresh ", shlex_quote(path)),
                          "source": "cli"})
    if not res.get("ok"):
        sys.stderr.write("pytermwm up: %s\n" % res.get("error"))
        return 1
    print("%s (session %r)" % ((res.get("result") or "up").split("\n")[0], session))
    if args.no_attach or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0
    from .client import attach, inside_session
    if inside_session() == session:
        return 0                                # built into the session this shell runs in: it is already on screen
    return attach(session, mouse=not args.no_mouse, nested=args.nested)


def shlex_quote(text: str) -> str:
    import shlex
    return shlex.quote(text) if not compat.IS_WINDOWS else '"%s"' % text.replace('"', "")


def cmd_sessions(args) -> int:
    names = P.list_sessions()
    if not names:
        print("(no running sessions)")
    for n in names:
        print(n)
    from .session import list_saved
    saved = [s for s in list_saved() if s not in names]
    if saved:
        print("saved (restore with `pytermwm restore NAME`): " + ", ".join(saved))
    return 0


def cmd_kill(args) -> int:
    return _print_result(_request(args, {"op": "quit"}))


def cmd_detach(args) -> int:
    return _print_result(_request(args, {"op": "detach"}))


def cmd_ctl(args) -> int:
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        sys.stderr.write("usage: pytermwm ctl COMMAND [ARGS...]   (see: pytermwm ctl commands)\n")
        return 2
    line = " ".join(args.command) if len(args.command) != 1 else args.command[0]
    if len(args.command) > 1:
        import shlex
        line = shlex.join(args.command)
    return _print_result(_request(args, {"op": "command", "line": line, "source": "cli"}))


def cmd_run(args) -> int:
    import shlex
    cmd = ["new-window"]
    if args.title:
        cmd += ["-t", args.title]
    if args.name:
        cmd += ["-n", args.name]
    if args.float:
        cmd += ["--float"]
    if args.desktop:
        cmd += ["-d", args.desktop]
    if args.cwd or True:
        cmd += ["-c", os.path.abspath(args.cwd or os.getcwd())]
    if args.close:
        cmd += ["--close"]
    if args.no_focus:
        cmd += ["--no-focus"]
    if args.pipe:
        cmd += ["--pipe"]
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    cmd += ["--"] + command
    res = _request(args, {"op": "command", "line": shlex.join(cmd), "source": "cli"})
    return _print_result(res)


def cmd_ls(args) -> int:
    return _print_result(_request(args, {"op": "command", "line": "list-windows", "source": "cli"}))


def cmd_send(args) -> int:
    req = {"op": "send", "window": args.target}
    if args.literal:
        req["text"] = " ".join(args.keys)
        req["enter"] = args.enter
    else:
        req["keys"] = args.keys
    res = _request(args, req)
    if not res.get("ok"):
        sys.stderr.write("error: %s\n" % res.get("error"))
        return 1
    return 0


def cmd_capture(args) -> int:
    res = _request(args, {"op": "capture", "window": args.target, "history": args.history})
    if not res.get("ok"):
        sys.stderr.write("error: %s\n" % res.get("error"))
        return 1
    print(res["text"])
    return 0


def cmd_state(args) -> int:
    res = _request(args, {"op": "state"})
    print(json.dumps(res.get("state", res), indent=2, default=str))
    return 0 if res.get("ok") else 1


def cmd_logs(args) -> int:
    res = _request(args, {"op": "logs", "n": args.n, "level": args.level})
    from .logs import format_record
    for r in res.get("logs", []):
        print(format_record(r, color=sys.stdout.isatty()))
    return 0


def cmd_status(args) -> int:
    req = {"op": "status_set", "key": args.key, "value": " ".join(args.value), "style": args.style}
    if args.ttl:
        req["ttl"] = args.ttl
    return _print_result(_request(args, req))


def cmd_frame(args) -> int:
    res = _request(args, {"op": "frame"})
    print(res.get("text", ""))
    return 0 if res.get("ok") else 1


def cmd_wait(args) -> int:
    pat = re.compile(args.pattern)
    end = time.time() + args.timeout
    c = _control(args)
    try:
        while time.time() < end:
            res = c.request({"op": "capture", "window": args.target, "history": True})
            if not res.get("ok"):
                sys.stderr.write("error: %s\n" % res.get("error"))
                return 1
            m = pat.search(res["text"])
            if m:
                print(m.group(0))
                return 0
            time.sleep(0.2)
    finally:
        c.close()
    sys.stderr.write("timeout waiting for %r\n" % args.pattern)
    return 124


def cmd_pipe(args) -> int:
    """cmd | pytermwm pipe : show a stream in a viewer window."""
    c = _control(args)
    try:
        spec = {"kind": "viewer", "title": args.title or "pipe"}
        if args.name:
            spec["name"] = args.name
        if args.float:
            spec["floating"] = True
        if args.no_focus:
            spec["focus"] = False
        res = c.request({"op": "create", "spec": spec})
        if not res.get("ok"):
            sys.stderr.write("error: %s\n" % res.get("error"))
            return 1
        wid = res["id"]
        fd = sys.stdin.fileno()
        out = sys.stdout.buffer if args.tee else None
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            if out:
                out.write(data)
                out.flush()
            r = c.request({"op": "feed", "window": wid, "data": data.decode("utf-8", "replace")})
            if not r.get("ok"):
                sys.stderr.write("error: %s\n" % r.get("error"))
                return 1
        c.request({"op": "command", "line": "rename %s" % json.dumps((args.title or "pipe") + " (done)")}) if False else None
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        c.close()
    return 0


def cmd_restore(args) -> int:
    from .session import default_path
    target = args.name or _session_name(args)
    path = target if os.path.isfile(os.path.expanduser(target)) else default_path(target)
    if not os.path.isfile(path):
        sys.stderr.write("pytermwm: no saved session %r (%s)\n" % (target, path))
        return 1
    session = args.session or os.path.splitext(os.path.basename(path))[0]
    if session in P.list_sessions():
        sys.stderr.write("pytermwm: session %r is already running\n" % session)
        return 1
    if not start_daemon(session, _find_config(args), None, path):
        return 1
    if args.attach:
        from .client import attach
        return attach(session, nested=args.nested)
    print("restored session %r" % session)
    return 0


def cmd_save(args) -> int:
    line = "session-save" + (" " + args.path if args.path else "")
    return _print_result(_request(args, {"op": "command", "line": line, "source": "cli"}))


def cmd_init_config(args) -> int:
    from .config import DEFAULT_CONFIG_YAML
    path = os.path.expanduser(args.path or os.path.join(compat.config_home(), "pytermwm", "config.yaml"))
    path = os.path.expanduser(path)
    if os.path.exists(path) and not args.force:
        sys.stderr.write("%s exists (use --force to overwrite)\n" % path)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(DEFAULT_CONFIG_YAML)
    print("wrote %s" % path)
    return 0


def cmd_check_config(args) -> int:
    from .config import load_config_file, validate_config, ConfigError, find_config
    path = args.path or find_config()
    if not path:
        sys.stderr.write("no configuration file found\n")
        return 1
    try:
        cfg, files = load_config_file(path)
    except ConfigError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    errors, warnings = validate_config(cfg)
    for w in warnings:
        print("warning: %s" % w)
    for e in errors:
        print("error: %s" % e)
    if not errors:
        print("%s: OK (%d file(s))" % (path, len(files)))
    return 1 if errors else 0


def cmd_help(args) -> int:
    from .helpdata import get_topic, all_help_text, topic_names
    from .wm import WindowManager
    wm = WindowManager(80, 24)
    try:
        if args.topic == "all":
            print(all_help_text(wm))
        else:
            t, body = get_topic(wm, args.topic or "index")
            print(t + "\n" + "=" * len(t) + "\n" + body)
    finally:
        wm.shutdown()
    return 0


def cmd_themes(args) -> int:
    from .theme import all_theme_names
    print("\n".join(all_theme_names()))
    return 0


def cmd_pv(args) -> int:
    from .pv import main as pv_main
    return pv_main(args.rest)


def cmd_mcp(args) -> int:
    from .mcp import run_stdio
    return run_stdio(_session_name(args), args.http)


def cmd_doctor(args) -> int:
    from .doctor import run
    return run()


def cmd_web(args) -> int:
    res = _request(args, {"op": "command", "line": "web-info", "source": "cli"})
    if res.get("ok") and isinstance(res.get("result"), dict):
        print(res["result"]["url"])
        return 0
    return _print_result(res)


def cmd_version(args) -> int:
    print("pytermwm %s" % __version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pytermwm", description="pytermwm - a modern terminal window manager")
    ap.add_argument("-s", "--session", help="session name (default: $PYTERMWM_SESSION or 'default')")
    ap.add_argument("-c", "--config", help="configuration file")
    ap.add_argument("--standalone", action="store_true", help="run without a background server (no detach)")
    ap.add_argument("--web", nargs="?", const="", default=None, metavar="[HOST:]PORT", help="enable the web interface")
    ap.add_argument("--debug", action="store_true", help="verbose logging")
    ap.add_argument("--no-mouse", action="store_true")
    ap.add_argument("--nested", action="store_true",
                    help="allow attaching to another session (or --standalone) from inside a pytermwm window")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    def sp(name, fn, help, aliases=()):
        p = sub.add_parser(name, help=help, aliases=list(aliases))
        p.set_defaults(fn=fn)
        return p

    p = sp("attach", cmd_attach, "attach to (or create) a session", ["a"])
    p.add_argument("--no-create", action="store_true")
    p = sp("start", cmd_start, "start a session in the background")
    p.add_argument("--restore", help="restore from a session file")
    p = sp("server", cmd_server, "run the session server in the foreground (internal)")
    p.add_argument("--restore")
    p.add_argument("--log-stderr", action="store_true")
    p = sp("up", cmd_up, "build a workspace from .pytermwm.yaml (session named after the project) and attach")
    p.add_argument("file", nargs="?", help="project file (default: .pytermwm.yaml in this or a parent directory)")
    p.add_argument("--no-attach", action="store_true", help="only create the workspace")
    sp("sessions", cmd_sessions, "list sessions", ["list"])
    sp("kill", cmd_kill, "end a session")
    sp("detach", cmd_detach, "detach all clients of a session")
    p = sp("ctl", cmd_ctl, "run a pytermwm command on a session")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sp("run", cmd_run, "open a window running a command")
    for a, kw in (("--title", {}), ("--name", {}), ("--desktop", {}), ("--cwd", {})):
        p.add_argument(a, **kw)
    p.add_argument("--float", action="store_true")
    p.add_argument("--close", action="store_true", help="close the window when the command ends")
    p.add_argument("--no-focus", action="store_true")
    p.add_argument("--pipe", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER)
    sp("ls", cmd_ls, "list windows")
    p = sp("send", cmd_send, "send keys to a window (Enter, C-c, text...)")
    p.add_argument("-t", "--target")
    p.add_argument("-l", "--literal", action="store_true")
    p.add_argument("-e", "--enter", action="store_true")
    p.add_argument("keys", nargs="+")
    p = sp("capture", cmd_capture, "print the text of a window")
    p.add_argument("-t", "--target")
    p.add_argument("-H", "--history", action="store_true")
    sp("state", cmd_state, "print the session state (JSON)")
    p = sp("logs", cmd_logs, "show the session log")
    p.add_argument("-n", type=int, default=50)
    p.add_argument("--level")
    p = sp("status", cmd_status, "set a status line item")
    p.add_argument("key")
    p.add_argument("value", nargs="+")
    p.add_argument("--style", default="normal")
    p.add_argument("--ttl", type=float)
    sp("frame", cmd_frame, "print the current screen as text")
    p = sp("wait", cmd_wait, "wait until a window's output matches a regex")
    p.add_argument("pattern")
    p.add_argument("-t", "--target")
    p.add_argument("--timeout", type=float, default=30)
    p = sp("pipe", cmd_pipe, "show stdin in a viewer window:  cmd | pytermwm pipe")
    p.add_argument("--title", "-t")
    p.add_argument("--name", "-n")
    p.add_argument("--float", action="store_true")
    p.add_argument("--no-focus", action="store_true")
    p.add_argument("--tee", action="store_true", help="also copy stdin to stdout")
    p = sp("restore", cmd_restore, "restore a saved session")
    p.add_argument("name", nargs="?")
    p.add_argument("--attach", action="store_true")
    p = sp("save", cmd_save, "save the running session now")
    p.add_argument("path", nargs="?")
    p = sp("init-config", cmd_init_config, "write a commented starter configuration")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    p = sp("check-config", cmd_check_config, "validate a configuration file")
    p.add_argument("path", nargs="?")
    p = sp("help", cmd_help, "print help topics")
    p.add_argument("topic", nargs="?")
    sp("themes", cmd_themes, "list themes")
    p = sp("pv", cmd_pv, "progress viewer wired to the status line")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sp("mcp", cmd_mcp, "run an MCP server (stdio) for a session")
    p.add_argument("--http", metavar="PORT", help="serve MCP over HTTP instead")
    sp("doctor", cmd_doctor, "check that this machine can run pytermwm (pty/ConPTY, sockets, terminal)")
    sp("web", cmd_web, "print the URL (with token) of the web interface")
    sp("version", cmd_version, "print the version")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `--web` has an optional value: make sure it does not swallow a sub command name
    for i, a in enumerate(argv):
        if a == "--web" and (i + 1 >= len(argv) or not re.match(r"^(\S*:)?\d+$", argv[i + 1])):
            argv[i] = "--web="
        if a in ("server", "attach", "start") and i > 0:
            break
    # `pytermwm pv ...` / `pipe` take raw arguments without top-level parsing
    if argv and argv[0] == "pv":
        from .pv import main as pv_main
        return pv_main(argv[1:])
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if not getattr(args, "fn", None):
        args.fn = cmd_attach
        args.no_create = False
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except (ConnectionError, BrokenPipeError) as e:
        sys.stderr.write("pytermwm: connection lost: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
