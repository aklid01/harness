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

PROJECT = Path.cwd().resolve()


def project_root():
    if paths.is_initialized():
        return paths.project_root()
    return PROJECT

MODES = ("default", "strict", "allow-all", "autopilot")
MODE = "default"
_ACTIVE_MODE = contextvars.ContextVar("harness_perm_mode", default=None)


def set_mode(mode):
    """Execution policy for the default (global) owner. The deny list wins in
    every mode, no exceptions."""
    global MODE
    if mode in MODES:
        MODE = mode


@contextlib.contextmanager
def moded(mode):
    """Scope the policy to ``mode`` for the current context (one session)."""
    token = _ACTIVE_MODE.set(mode if mode in MODES else None)
    try:
        yield
    finally:
        try:
            _ACTIVE_MODE.reset(token)
        except ValueError:
            pass


def current_mode():
    m = _ACTIVE_MODE.get()
    return m if m is not None else MODE

# Last matching rule wins, so put the catch-all first.
BASH_RULES = {
    "*": "ask",
    # read-only: let them through
    "ls*": "allow",
    "pwd": "allow",
    "cd *": "allow",
    "echo *": "allow",
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

def split_command(command):
    """Split a compound command on the separators that actually separate.

    A naive split on | and ; also cuts inside quotes, so `rg "cap|max"` breaks
    into fragments that match no rule and fall through to "ask". Anything
    quoted or backslash-escaped is an argument, not a separator.
    """
    parts, current, quote = [], [], None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            quote = None if char == quote else quote
        elif char == "\\":
            current.append(char)
            index += 1
            if index < len(command):
                current.append(command[index])
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in "&|;":
            parts.append("".join(current))
            current = []
            while index + 1 < len(command) and command[index + 1] in "&|":
                index += 1
        else:
            current.append(char)
        index += 1

    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def decide(command):
    """Rate every part of a compound command; the strictest verdict wins."""
    verdicts = []
    for part in split_command(command):
        action = "ask"
        for pattern, rule in BASH_RULES.items():
            if fnmatch(part, pattern):
                action = rule
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


# Levels are conservative and derived from the command/tool itself. The model's
# own suggestion is never trusted for risk - only the command text is.
RISK_LEVELS = ("low", "medium", "high", "critical")


def assess_risk(name, args):
    """Return (level, one-line reason). Deterministic; conservative."""
    if name == "bash":
        return _bash_risk(str(args.get("command", "")))
    if name in ("write_file", "str_replace"):
        return "medium", "Edits a file in the project."
    if name == "task":
        return "medium", "Spawns a sub-agent to explore."
    return "low", "Read-only or low-impact tool."


def _bash_risk(cmd):
    low = cmd.lower()
    if any(m in low for m in ("rm -rf", "rm -fr", "mkfs", "shutdown", "reboot", ":(){", "dd if=",
                              "--no-preserve-root", "| sh", "| bash", "curl", "wget", "chmod -r 777")):
        return "critical", "Can delete data or fetch and run remote code."
    if any(m in low for m in ("rm ", "rmdir", "truncate", "git clean", "git reset", "git checkout --",
                              "git restore", "mv ", "del ", "shred")):
        return "high", "Removes, moves, or overwrites existing files."
    if ">" in cmd:
        return "high", "Redirects output into a file, overwriting it."
    if any(m in low for m in ("install", "pip ", "npm ", "yarn ", "pnpm ", "apt", "apt-get", "brew",
                              "cargo", "go get", "gem ", "composer", "gradle", "mvn ")):
        return "medium", "Installs or fetches dependencies."
    if any(m in low for m in ("make", " tsc", "build", "pytest", "npm run", "cargo build",
                              "docker build", "docker run")):
        return "medium", "Runs a build, test, or container step."
    if any(m in low for m in ("sed -i", "tee ", "sed ", "export ", "setx", "kubectl", "helm")):
        return "high", "Modifies configuration or system state."
    return "medium", "Runs a shell command the model requested."


def approval_info(name, args, reason, note=""):
    """Rich payload for the approval dialog: validated risk + short context."""
    level, risk_reason = assess_risk(name, args)
    command = str(args.get("command", "")) if name == "bash" else ""
    target = str(args.get("path", "")) if name in ("write_file", "str_replace", "read_file") else ""
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
