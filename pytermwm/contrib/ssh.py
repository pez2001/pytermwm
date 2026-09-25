"""SSH sessions as windows (uses the system ``ssh``).

    plugins:
      - name: ssh
        use_ssh_config: true          # offer the Host entries of ~/.ssh/config (default true)
        reconnect: false              # restart a session's window when it drops
        hosts:
          prod: {host: prod.example.com, user: deploy, port: 2222, identity: ~/.ssh/prod, cmd: "tmux attach"}
          db:   {host: 10.0.0.5, user: admin}

Commands: ssh <name|[user@]host> [-- remote command...]    ssh-list    ssh-run <name> <command...>    ssh-forward <name> <L:port:host:port>
Segment: ``ssh`` (number of open sessions).
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Dict, List

from pytermwm.commands import CommandError
from pytermwm.statusline import Segment

TARGET_RE = re.compile(r"^(?:[A-Za-z0-9._%+-]+@)?[A-Za-z0-9][A-Za-z0-9._:-]*$")
FORWARD_RE = re.compile(r"^(?:[A-Za-z0-9.*_-]+:)?\d{1,5}:[A-Za-z0-9._-]+:\d{1,5}$")


def parse_ssh_config(path: str) -> Dict[str, dict]:
    hosts: Dict[str, dict] = {}
    cur: List[str] = []
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return hosts
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\w+)\s*[=\s]\s*(.+)$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "host":
            cur = [h for h in val.split() if not any(c in h for c in "*?!")]
            for h in cur:
                hosts.setdefault(h, {})
        elif cur:
            for h in cur:
                if key == "hostname":
                    hosts[h]["host"] = val
                elif key == "user":
                    hosts[h]["user"] = val
                elif key == "port":
                    hosts[h]["port"] = val
                elif key == "identityfile":
                    hosts[h].setdefault("identity", val)
    return hosts


def setup(api):
    cfg = api.config
    wm = api.wm
    hosts: Dict[str, dict] = {}
    if cfg.get("use_ssh_config", True):
        hosts.update(parse_ssh_config(cfg.get("ssh_config", "~/.ssh/config")))
    cf = cfg.get("hosts") or {}
    if isinstance(cf, list):
        cf = {str(h.get("name") or h.get("host")): h for h in cf if isinstance(h, dict)}
    for name, spec in cf.items():
        hosts[str(name)] = spec if isinstance(spec, dict) else {"host": str(spec)}
    binary = cfg.get("bin") or "ssh"

    def build(target: str, remote: List[str], extra: List[str] = ()) -> (List[str], str):
        spec = hosts.get(target)
        argv = [binary, "-o", "ServerAliveInterval=30"]
        if spec is not None:
            host = spec.get("host") or target
            if spec.get("user"):
                host = "%s@%s" % (spec["user"], host)
            if spec.get("port"):
                argv += ["-p", str(int(spec["port"]))]
            if spec.get("identity"):
                argv += ["-i", os.path.expanduser(str(spec["identity"]))]
            for o in spec.get("options") or []:
                argv += ["-o", str(o)]
            if not remote and spec.get("cmd"):
                remote = [str(spec["cmd"])]        # ssh hands it to the remote login shell
                argv.append("-t")
        else:
            host = target
        if not TARGET_RE.match(host):
            raise CommandError("invalid ssh target: %r" % host)
        argv += list(extra)
        argv += ["--", host] + list(remote)
        return argv, host

    def check_bin():
        if shutil.which(binary) is None:
            raise CommandError("ssh not found")

    def c_ssh(wm_, args):
        if not args:
            raise CommandError("usage: ssh <name|[user@]host> [-- remote command...]")
        check_bin()
        target = args[0]
        remote = args[2:] if len(args) > 1 and args[1] == "--" else args[1:]
        argv, host = build(target, remote)
        spec = {"cmd": argv, "title": "ssh %s" % host, "kind": "term", "tag": "ssh"}
        if cfg.get("reconnect") or (hosts.get(target) or {}).get("reconnect"):
            spec["on_exit"] = "restart"
        else:
            spec["keep"] = True
        return {"id": wm_.create_window(spec).id}

    def c_run(wm_, args):
        if len(args) < 2:
            raise CommandError("usage: ssh-run <name|host> <command...>")
        check_bin()
        argv, host = build(args[0], args[1:], ["-T", "-o", "BatchMode=yes"])
        return {"id": wm_.create_window({"cmd": argv, "title": "ssh %s: %s" % (host, " ".join(args[1:])[:30]), "pipe": True,
                                         "keep": True, "tag": "ssh"}).id}

    def c_forward(wm_, args):
        if len(args) != 2 or not FORWARD_RE.match(args[1]):
            raise CommandError("usage: ssh-forward <name|host> [bind:]port:host:port")
        check_bin()
        argv, host = build(args[0], ["-N"], ["-L", args[1], "-o", "ExitOnForwardFailure=yes"])
        return {"id": wm_.create_window({"cmd": argv, "title": "tunnel %s %s" % (host, args[1]), "pipe": True, "keep": True, "tag": "ssh"}).id}

    def c_list(wm_, args):
        return [{"name": n, "host": s.get("host", n), "user": s.get("user"), "port": s.get("port")} for n, s in sorted(hosts.items())]

    names = lambda wm_, prev, partial: sorted(hosts) if not prev else []
    api.command("ssh", c_ssh, usage="ssh <name|[user@]host> [-- cmd...]", help="Open an SSH session in a window", completer=names)
    api.command("ssh-run", c_run, usage="ssh-run <name|host> <command...>", help="Run a remote command (output in a window)", completer=names)
    api.command("ssh-forward", c_forward, usage="ssh-forward <name|host> <port:host:port>", help="Open a local port forward", completer=names)
    api.command("ssh-list", c_list, usage="ssh-list", help="Known SSH hosts")

    def seg(wm_, o):
        n = sum(1 for w in wm_.windows.values() if w.opts.get("tag") == "ssh")
        return Segment("ssh:%d" % n, "accent", prio=2) if n else None

    api.segment("ssh", seg)
