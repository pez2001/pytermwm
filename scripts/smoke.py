#!/usr/bin/env python3
"""End-to-end smoke test of the real CLI, daemon, HTTP API and MCP server.  Works on Linux, macOS and Windows and
needs no installation (it runs ``python -m pytermwm`` from this checkout).

    python scripts/smoke.py            exit code 0 = everything works
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="ptw-smoke-")
SESSION = "smoke%d" % os.getpid()
ENV = dict(os.environ)
ENV.update({"PYTERMWM_RUNTIME_DIR": os.path.join(TMP, "run"), "PYTERMWM_STATE_DIR": os.path.join(TMP, "state"),
            "HOME": os.path.join(TMP, "home"), "USERPROFILE": os.path.join(TMP, "home"),
            "APPDATA": os.path.join(TMP, "appdata"), "LOCALAPPDATA": os.path.join(TMP, "localappdata"),
            "PYTHONPATH": ROOT + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
for d in ("run", "state", "home", "appdata", "localappdata"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
if os.name != "nt":
    os.chmod(os.path.join(TMP, "run"), 0o700)
FAILS = []


def pt(*args, session=True, timeout=30, input=None):
    cmd = [sys.executable, "-m", "pytermwm"] + (["-s", SESSION] if session else []) + list(args)
    try:
        p = subprocess.run(cmd, env=ENV, cwd=ROOT, capture_output=True, text=True, timeout=timeout, input=input, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        return 124, "", "timeout: %s" % e
    return p.returncode, p.stdout, p.stderr


def check(name, ok, detail=""):
    print("  %-5s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "   (%s)" % str(detail).strip()[:300]))
    if not ok:
        FAILS.append(name)
    return ok


def wait_for(pattern, target, timeout=15):
    rc, out, err = pt("wait", "-t", target, "--timeout", str(timeout), pattern, timeout=timeout + 15)
    return rc == 0, out + err


def listed():
    """True while the session shows up as *running* (saved sessions are listed on a separate line)."""
    return SESSION in pt("sessions", session=False)[1].splitlines()


def py(code):
    return [sys.executable, "-u", "-c", code]


def main():
    rc, out, _ = pt("version", session=False)
    print("pytermwm smoke test (%s, %s, python %s)" % (out.strip(), sys.platform, sys.version.split()[0]))
    check("doctor finds no problems", pt("doctor", session=False)[0] == 0, pt("doctor", session=False)[1][-400:])

    cfg = os.path.join(TMP, "c.yaml")
    check("init-config writes a starter file", pt("init-config", cfg, session=False)[0] == 0)
    check("check-config accepts the starter config", pt("check-config", cfg, session=False)[0] == 0)
    bad = os.path.join(TMP, "bad.yaml")
    with open(bad, "w") as f:
        f.write("theme: [oops\n")
    check("check-config rejects a broken file", pt("check-config", bad, session=False)[0] != 0)

    rc, out, err = pt("--web", "0", "start", timeout=60)
    check("session starts", rc == 0, out + err)
    end = time.time() + 20
    while time.time() < end and not listed():
        time.sleep(0.2)
    check("session is listed", listed())

    pt("run", "--name", "smoke", "--", *py("print('smoke-marker-%d' % (6*7)); import time; time.sleep(60)"))
    ok, info = wait_for("smoke-marker-42", "smoke")
    check("wait sees process output", ok, info)
    rc, out, _ = pt("capture", "-t", "smoke")
    check("capture returns the text", "smoke-marker-42" in out, out)

    pt("run", "--name", "typer", "--", *py("import sys, time; l = sys.stdin.readline(); print('got:' + l.strip()); sys.stdout.flush(); time.sleep(60)"))
    time.sleep(0.5)
    pt("send", "-t", "typer", "-l", "-e", "hello")
    ok, info = wait_for("got:hello", "typer")
    check("send types into a window", ok, info)

    pt("ctl", "layout grid")
    check("ctl changes the layout", '"grid"' in pt("state")[1])
    check("frame renders the screen", "smoke" in pt("frame")[1])
    pt("status", "build", "ok 42")
    check("status item appears in the status line", "ok 42" in pt("frame")[1])
    check("save works", pt("ctl", "session-save")[0] == 0)

    rc, out, err = pt("web")
    url = out.strip().splitlines()[-1] if out.strip() else ""
    if check("web URL is printed", "token=" in url, out + err):
        base, token = url.split("/?token=")
        hdr = {"Authorization": "Bearer " + token}

        def get(path, headers=hdr):
            return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=10)

        try:
            windows = json.load(get("/api/state"))["state"]["windows"]
            check("HTTP /api/state with token", len(windows) >= 2, windows)
        except Exception as e:
            check("HTTP /api/state with token", False, e)
        try:
            get("/api/state", {})
            check("HTTP rejects a request without token", False, "accepted")
        except urllib.error.HTTPError as e:
            check("HTTP rejects a request without token", e.code == 401, e.code)
        try:
            check("web UI is served", b"pytermwm" in get("/").read())
        except Exception as e:
            check("web UI is served", False, e)
        try:
            req = urllib.request.Request(base + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                                         headers=dict(hdr, **{"Content-Type": "application/json"}))
            tools = json.load(urllib.request.urlopen(req, timeout=10))["result"]["tools"]
            check("MCP tools/list over HTTP", len(tools) >= 10, len(tools))
        except Exception as e:
            check("MCP tools/list over HTTP", False, e)

    msgs = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"screenshot","arguments":{}}}\n')
    rc, out, err = pt("mcp", input=msgs, timeout=40)
    check("MCP stdio initialize", '"serverInfo"' in out, out + err)
    check("MCP stdio screenshot tool", "smoke" in out, out + err)

    pt("ctl", "notify -t smoke hello-notify")
    check("notify shows in the status line", "hello-notify" in pt("frame")[1], pt("frame")[1][-300:])
    cast = os.path.join(TMP, "smoke.cast")
    check("record starts", pt("ctl", "record -t smoke " + json.dumps(cast))[0] == 0)
    pt("send", "-t", "typer", "-l", "-e", "more")
    time.sleep(0.5)
    check("record stops", pt("ctl", "record-stop -t smoke")[0] == 0)
    try:
        with open(cast, encoding="utf-8") as f:
            check("recording is asciicast v2", json.loads(f.readline()).get("version") == 2)
    except (OSError, ValueError) as e:
        check("recording is asciicast v2", False, e)
    check("replay opens a window", pt("ctl", "replay " + json.dumps(cast))[0] == 0)

    pt("kill")
    end = time.time() + 15
    while time.time() < end and listed():
        time.sleep(0.2)
    check("session ends", not listed())

    # project workspace: `pytermwm up`
    proj = os.path.join(TMP, "proj")
    os.makedirs(proj, exist_ok=True)
    pname = "smokeup%d" % os.getpid()
    pfile = os.path.join(proj, ".pytermwm.yaml")
    with open(pfile, "w", encoding="utf-8") as f:
        f.write("name: %s\ndesktops:\n  - name: one\n    windows:\n      - {kind: text, text: project-marker, name: t}\n" % pname)
    rc, out, err = pt("up", "--no-attach", pfile, session=False, timeout=60)
    check("up builds the project workspace", rc == 0 and "2 created" in out, out + err)
    check("up is repeatable", "0 created" in pt("up", "--no-attach", pfile, session=False)[1])
    p = subprocess.run([sys.executable, "-m", "pytermwm", "-s", pname, "frame"], env=ENV, cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    check("project window is on screen", "project-marker" in p.stdout, p.stdout + p.stderr)
    subprocess.run([sys.executable, "-m", "pytermwm", "-s", pname, "kill"], env=ENV, cwd=ROOT, capture_output=True)
    print("  ---")
    print("smoke test passed" if not FAILS else "%d check(s) FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        pt("kill")
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
