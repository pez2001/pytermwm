"""Command registry shared by hotkeys, the prompt, CLI, HTTP API, MCP, scripts and plugins."""
from __future__ import annotations

import shlex

from . import compat
from typing import Callable, Dict, Iterable, List, Optional, Sequence


class CommandError(Exception):
    """A command failed for a user-visible reason."""


class Command:
    def __init__(self, name: str, fn: Callable, usage: str = "", help: str = "", aliases: Sequence[str] = (),
                 completer: Optional[Callable] = None, category: str = "misc", hidden: bool = False):
        self.name = name
        self.fn = fn
        self.usage = usage or name
        self.help = help
        self.aliases = tuple(aliases)
        self.completer = completer
        self.category = category
        self.hidden = hidden


_BUILTIN: List[Command] = []


def command(name: str, usage: str = "", help: str = "", aliases: Sequence[str] = (),
            completer: Optional[Callable] = None, category: str = "misc", hidden: bool = False):
    """Decorator for builtin commands. ``fn(wm, args) -> result``."""
    def deco(fn):
        _BUILTIN.append(Command(name, fn, usage, help or (fn.__doc__ or "").strip().split("\n")[0], aliases,
                                completer, category, hidden))
        return fn
    return deco


def split_line(line: str) -> List[List[str]]:
    """Split a command line on ';' (outside quotes) into argv lists."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=";")
    lex.whitespace_split = True
    lex.commenters = ""
    if compat.IS_WINDOWS:
        lex.escape = ""             # backslashes are path separators on Windows, not escape characters
    cmds: List[List[str]] = [[]]
    for tok in lex:
        if tok == ";":
            cmds.append([])
        elif set(tok) == {";"}:     # ";;" and friends
            cmds.append([])
        else:
            cmds[-1].append(tok)
    return [c for c in cmds if c]


class CommandRegistry:
    def __init__(self, load_builtin: bool = True):
        self.commands: Dict[str, Command] = {}
        self.alias: Dict[str, str] = {}
        if load_builtin:
            from . import builtin_commands  # noqa: F401  (registers into _BUILTIN)
            for c in _BUILTIN:
                self.add(c)

    def add(self, cmd: Command):
        self.commands[cmd.name] = cmd
        for a in cmd.aliases:
            self.alias[a] = cmd.name

    def register(self, name: str, fn: Callable, usage: str = "", help: str = "", aliases: Sequence[str] = (),
                 completer: Optional[Callable] = None, category: str = "plugin"):
        self.add(Command(name, fn, usage, help, aliases, completer, category))

    def unregister(self, name: str):
        c = self.commands.pop(name, None)
        if c:
            for a in c.aliases:
                self.alias.pop(a, None)

    def get(self, name: str) -> Optional[Command]:
        name = self.alias.get(name, name)
        return self.commands.get(name)

    def names(self, include_hidden: bool = False) -> List[str]:
        n = [c.name for c in self.commands.values() if include_hidden or not c.hidden]
        n.extend(a for a in self.alias if not self.commands[self.alias[a]].hidden)
        return sorted(set(n))

    def complete_args(self, wm, name: str, prev: List[str], partial: str) -> List[str]:
        c = self.get(name)
        if not c or not c.completer:
            return []
        try:
            res = c.completer(wm, prev, partial) or []
        except Exception:
            return []
        return [str(r) for r in res]

    def run_argv(self, wm, argv: List[str]):
        if not argv:
            return None
        c = self.get(argv[0])
        if c is None:
            raise CommandError("unknown command: %s" % argv[0])
        return c.fn(wm, argv[1:])
