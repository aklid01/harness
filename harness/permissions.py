"""Which tool calls need a human.

The sandbox decides what is *possible*. These rules only decide what is worth
interrupting you for - so read-only commands run silently, and the risky ones
still stop and ask.
"""

import contextlib
import contextvars
from fnmatch import fnmatch
from pathlib import Path

from . import paths
from .shellrisk import (
    assignment_only,
    classify_bash,
    has_substitution,
    hides_write_primitive,
    redirect_writes_file,
    shell_words,
    split_command,
)
from .toolsets import SAFE_TOOLS


def project_root():
    return paths.project_root()


MODES = ("default", "strict", "allow-all", "autopilot")
MODE = "default"
_ACTIVE_MODE = contextvars.ContextVar("harness_perm_mode", default=None)


class ModeRef:
    """A live permission-mode holder for one session.

    Session bindings set the active mode by reference, so changing it while a
    turn is already running takes effect on the next permission check - the
    running worker reads ``value`` at call time instead of a mode captured when
    the turn started.
    """

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value if value in MODES else MODE


def mode_ref(initial=MODE):
    return ModeRef(initial)


def set_mode(mode):
    """Execution policy for the default (global) owner. The deny list wins in
    every mode, no exceptions."""
    global MODE
    if mode in MODES:
        MODE = mode


@contextlib.contextmanager
def moded(target):
    """Scope the policy for the current context (one session).

    ``target`` may be a mode string (captured once) or a ``ModeRef`` (read live,
    so a mid-turn change applies).
    """
    ref = target if isinstance(target, ModeRef) else ModeRef(target)
    token = _ACTIVE_MODE.set(ref)
    try:
        yield
    finally:
        try:
            _ACTIVE_MODE.reset(token)
        except ValueError:
            pass


def current_mode():
    ref = _ACTIVE_MODE.get()
    if isinstance(ref, ModeRef):
        return ref.value if ref.value in MODES else MODE
    if ref in MODES:
        return ref
    return MODE


BASH_RULES = {
    "*": "ask",
    "ls*": "allow",
    "pwd": "allow",
    "cd *": "allow",
    "echo *": "allow",
    "type": "allow",
    "type *": "allow",
    "dir": "allow",
    "dir *": "allow",
    "findstr": "allow",
    "findstr *": "allow",
    "select-string *": "allow",
    "get-childitem": "allow",
    "get-childitem *": "allow",
    "gci *": "allow",
    "where *": "allow",
    "more *": "allow",
    "sed -n*": "allow",
    "mkdir": "allow",
    "mkdir *": "allow",
    "sort*": "allow",
    "uniq*": "allow",
    "cut *": "allow",
    "basename *": "allow",
    "dirname *": "allow",
    "date*": "allow",
    "env": "allow",
    "cat *": "allow",
    "head *": "allow",
    "tail *": "allow",
    "wc *": "allow",
    "file *": "allow",
    "which *": "allow",
    "grep *": "allow",
    "rg *": "allow",
    "find *": "allow",
    "tree*": "allow",
    "git status*": "allow",
    "git diff*": "allow",
    "git log*": "allow",
    "git show*": "allow",
    "git ls-files*": "allow",
    "pytest*": "allow",
    "python -m pytest*": "allow",
    "rm *": "deny",
    "sudo *": "deny",
    "chmod *": "deny",
    "chown *": "deny",
    "curl *": "deny",
    "wget *": "deny",
    "git push*": "deny",
    "git reset*": "deny",
    "git clean*": "deny",
}


def decide(command):
    """Rate every part of a compound command; the strictest verdict wins."""
    verdicts = []
    for part in split_command(command):
        action = "ask"
        for pattern, rule in BASH_RULES.items():
            if fnmatch(part, pattern):
                action = rule

        if action != "deny" and assignment_only(shell_words(part)):
            action = "allow"

        if action == "allow" and redirect_writes_file(part):
            action = "ask"

        if action == "allow" and (
            has_substitution(part) or hides_write_primitive(part)
        ):
            action = "ask"
        verdicts.append(action)

    for strictest in ("deny", "ask"):
        if strictest in verdicts:
            return strictest
    return "allow"


def inside_project(path):
    root = project_root()
    try:
        return root in Path(path).resolve().parents or Path(path).resolve() == root
    except OSError:
        return False


def check(name, args):
    """Return (action, reason). Action is allow, ask or deny."""
    if name == "bash":
        action, reason = decide(args["command"]), f"run: {args['command']}"
        if action == "ask" and current_mode() in ("allow-all", "autopilot"):
            return "allow", None
        return action, reason

    if name in ("write_file", "str_replace"):
        root = project_root()
        if not inside_project(args["path"]):
            if current_mode() == "autopilot":
                return "allow", None
            return "ask", f"{name} outside {root}: {args['path']}"
        if current_mode() == "strict":
            return "ask", f"{name} inside {root}: {args['path']}"
        return "allow", None

    if name in SAFE_TOOLS:
        return "allow", None
    return "ask", f"{name} has no permission rule"


RISK_LEVELS = ("low", "medium", "high", "critical")


def assess_risk(name, args):
    """Return (level, one-line reason). Deterministic; conservative."""
    if name == "bash":
        return classify_bash(str(args.get("command", "")))
    if name in ("write_file", "str_replace"):
        return "medium", "Edits a file in the project."
    if name == "task":
        return "medium", "Spawns a sub-agent to explore."
    return "low", "Read-only or low-impact tool."


def approval_info(name, args, reason, note=""):
    """Rich payload for the approval dialog: validated risk + short context."""
    level, risk_reason = assess_risk(name, args)
    command = str(args.get("command", "")) if name == "bash" else ""
    target = (
        str(args.get("path", ""))
        if name in ("write_file", "str_replace", "read_file")
        else ""
    )
    why = (note or "").strip()
    if len(why) > 280:
        why = why[:280].rstrip() + "…"
    if not why:
        why = reason or "The model wants to run this."
    return {
        "reason": reason,
        "tool": name,
        "command": command,
        "target": target,
        "why": why,
        "risk": level,
        "risk_reason": risk_reason,
    }
