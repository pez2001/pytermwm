"""Scoped API tokens.

The session token (``pytermwm web``) controls everything.  Extra tokens can be listed in the config so an agent, a
dashboard or a teammate gets less::

    web:
      tokens:
        - {name: dashboard, token: "<random string, 16+ chars>", scope: read}
        - {name: claude,    token: "<random string, 16+ chars>", scope: agent}

Scopes
    ``read``    look but do not touch: state, window list, window text, frames, events, logs, help.
    ``agent``   ``read`` plus: create windows (always tagged ``agent``), type into and close windows tagged ``agent``.
                No WM commands, config, scripts, ``feed``, detach or quit.  NOTE: a window can run any program, so an
                agent can act as you *through its own windows*; the scope protects the rest of the session, not the
                account.  Use ``read`` for anything that should only watch.

A scoped token never sees the configuration (it may contain credentials).  Tokens are read from the live config, so
adding or revoking one takes effect immediately.
"""
from __future__ import annotations

import hmac
from typing import Any, Dict, List, Optional, Tuple

SCOPES = ("read", "agent")
AGENT_TAG = "agent"

READ_OPS = frozenset({"state", "windows", "capture", "frame", "frame_json", "events", "commands", "rules", "plugins",
                      "themes", "dialogs", "help", "logs", "ping"})
TAGGED_OPS = frozenset({"send", "close"})            # allowed in scope "agent" on windows tagged "agent"
AGENT_OPS = READ_OPS | TAGGED_OPS | {"create"}

MIN_TOKEN_LENGTH = 16


def token_list(cfg: dict) -> List[dict]:
    """The valid entries of ``web.tokens``."""
    web = (cfg or {}).get("web") or {}
    out = []
    for e in web.get("tokens") or []:
        if isinstance(e, dict) and isinstance(e.get("token"), str) and len(e["token"]) >= MIN_TOKEN_LENGTH \
                and e.get("scope") in SCOPES:
            out.append({"name": str(e.get("name") or e["scope"]), "token": e["token"], "scope": e["scope"]})
    return out


def find_token(cfg: dict, presented: str) -> Optional[dict]:
    """Constant-time lookup; every entry is compared so timing does not reveal which one matched."""
    found = None
    for e in token_list(cfg):
        if hmac.compare_digest(e["token"].encode(), presented.encode()):
            found = e
    return found


def validate_tokens(entries: Any) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if entries is None:
        return errors, warnings
    if not isinstance(entries, list):
        return ["web.tokens: expected a list"], []
    seen = set()
    for i, e in enumerate(entries):
        where = "web.tokens[%d]" % i
        if not isinstance(e, dict):
            errors.append("%s: expected a mapping with name, token and scope" % where)
            continue
        tok = e.get("token")
        if not isinstance(tok, str) or len(tok) < MIN_TOKEN_LENGTH:
            errors.append("%s: token must be a string of at least %d characters" % (where, MIN_TOKEN_LENGTH))
        elif tok in seen:
            errors.append("%s: duplicate token" % where)
        else:
            seen.add(tok)
        if e.get("scope") not in SCOPES:
            errors.append("%s: scope must be one of %s" % (where, ", ".join(SCOPES)))
    return errors, warnings


def _tag(wm, ref) -> Optional[str]:
    try:
        return wm.resolve_window(ref).opts.get("tag")
    except Exception:
        return None


def check(scope: Optional[str], wm, req: Dict[str, Any]) -> Optional[str]:
    """None when the request is allowed, else the reason.  ``scope`` None means the full session token."""
    if scope is None:
        return None
    op = req.get("op", "command")
    if scope == "read":
        return None if op in READ_OPS else "this token is read-only (op %r not allowed)" % op
    if scope == "agent":
        if op in READ_OPS or op == "create":
            return None
        if op in TAGGED_OPS:
            if _tag(wm, req.get("window")) != AGENT_TAG:
                return "this token may only %s windows tagged %r" % (op, AGENT_TAG)
            return None
        return "this token may not use op %r" % op
    return "unknown token scope %r" % scope


def restrict(scope: Optional[str], req: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite an allowed request for the scope (agent windows are always tagged)."""
    if scope == "agent" and req.get("op") == "create":
        spec = dict(req.get("spec") or {})
        spec["tag"] = AGENT_TAG
        req = dict(req, spec=spec)
    return req
