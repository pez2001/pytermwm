"""Auto completion for the command prompt / palette."""
from __future__ import annotations

import glob
import os
import shlex

from .compat import is_executable_file, split_command_line
import time
from typing import List, Tuple

_path_cache = {"t": 0.0, "names": []}


def executables() -> List[str]:
    now = time.time()
    if now - _path_cache["t"] > 30:
        names = set()
        for d in os.environ.get("PATH", "").split(os.pathsep):
            try:
                for n in os.listdir(d):
                    p = os.path.join(d, n)
                    if is_executable_file(p):
                        names.add(n)
            except OSError:
                pass
        names.update(("cd", "exit", "export", "alias", "history", "source", "echo", "type", "ulimit"))
        _path_cache["names"] = sorted(names)
        _path_cache["t"] = now
    return _path_cache["names"]


def common_prefix(items: List[str]) -> str:
    if not items:
        return ""
    return os.path.commonprefix(items)


def complete_path(partial: str) -> List[str]:
    p = os.path.expanduser(partial) if partial.startswith("~") else partial
    d = p if p.endswith("/") else os.path.dirname(p)
    base = "" if p.endswith("/") else os.path.basename(p)
    try:
        names = os.listdir(d or ".")
    except OSError:
        return []
    out = []
    for n in sorted(names):
        if n.startswith(base) and (not n.startswith(".") or base.startswith(".")):
            full = os.path.join(d, n) if d else n
            if os.path.isdir(os.path.join(d or ".", n)):
                full += "/"
            if partial.startswith("~"):
                home = os.path.expanduser("~")
                if full.startswith(home):
                    full = "~" + full[len(home):]
            out.append(full)
    return out


def _split_last(line: str) -> Tuple[int, str, List[str]]:
    """Return (start index of the word being completed, that word, previous words)."""
    i = len(line)
    while i > 0 and not line[i - 1].isspace():
        i -= 1
    word = line[i:]
    try:
        prev = split_command_line(line[:i])
    except ValueError:
        prev = line[:i].split()
    return i, word, prev


def complete(wm, line: str, mode: str) -> Tuple[int, List[str], str]:
    """Complete ``line`` (text before the cursor).  Returns (start, candidates, common prefix)."""
    if mode == "command":
        offset = 1 if line.startswith(":") else 0
        start, word, prev = _split_last(line[offset:])
        start += offset
        if not prev:
            cands = [n for n in wm.commands.names() if n.startswith(word)]
        else:
            cands = wm.commands.complete_args(wm, prev[0], prev[1:], word)
            cands = [c for c in cands if c.startswith(word)]
        cands = sorted(dict.fromkeys(cands))
        return start, cands, common_prefix(cands)
    # shell mode
    start, word, prev = _split_last(line)
    if word.startswith("$"):
        cands = ["$" + k for k in sorted(os.environ) if ("$" + k).startswith(word)]
    elif not prev and not (word.startswith(("/", "./", "../", "~"))):
        cands = [n for n in executables() if n.startswith(word)]
        cands = sorted(cands, key=lambda s: (len(s), s))[:200]
    else:
        cands = complete_path(word)
        if not cands and prev and prev[0] == "docker" and hasattr(wm, "shell_completers"):
            cands = []
    return start, cands, common_prefix(cands)


def complete_command_args(wm, line: str) -> List[str]:
    """Full command lines for the palette: 'layout g' -> ['layout grid']"""
    try:
        parts = split_command_line(line)
    except ValueError:
        return []
    if not parts:
        return []
    if line.endswith(" "):
        parts.append("")
    name, args = parts[0], parts[1:]
    if not args:
        return []
    cands = wm.commands.complete_args(wm, name, args[:-1], args[-1])
    from .draw import fuzzy_score
    res = []
    for c in cands:
        if not args[-1] or fuzzy_score(args[-1], c) is not None:
            res.append(" ".join([name] + args[:-1] + [c]))
    return res[:30]
