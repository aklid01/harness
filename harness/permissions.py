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
    _assignment_only,
    _has_substitution,
    _hides_write_primitive,
    _shell_words,
    classify_bash,
    redirect_writes_file,
    split_command,
)


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


# Last matching rule wins, so put the catch-all first.
BASH_RULES = {
    "*": "ask",
    # read-only: let them through
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
    # risky: never, even if the user says yes
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
        # Setting a shell variable is not a command and touches nothing on
        # disk, so a segment that only assigns is read-only. A deny rule
        # still wins.
        if action != "deny" and _assignment_only(_shell_words(part)):
            action = "allow"
        # A read-only command that redirects into a real file overwrites data,
        # so an "allow" it earned disappears the moment it writes one.
        if action == "allow" and redirect_writes_file(part):
            action = "ask"
        # Same for commands that smuggle a second command or a write primitive
        # past a prefix rule: the allow was earned by the prefix, not by what
        # the segment actually does.
        if action == "allow" and (
            _has_substitution(part) or _hides_write_primitive(part)
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


# Tools with no explicit branch in check() that are safe to run unattended.
# Anything else that is not bash/write/str_replace falls through to "ask", so a
# future mutating tool can never silently inherit the default-allow.
_KNOWN_SAFE = ("read_file", "read_skill", "write_todos", "task")


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

    if name in _KNOWN_SAFE:
        return "allow", None
    return "ask", f"{name} has no permission rule"


# Levels are conservative and derived from the command/tool itself. The model's
# own suggestion is never trusted for risk - only the command text is.
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
