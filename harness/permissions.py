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

def split_command(command):
    """Split a compound command on the separators that actually separate.

    A naive split on | and ; also cuts inside quotes, so `rg "cap|max"` breaks
    into fragments that match no rule and fall through to "ask". Anything
    quoted or backslash-escaped is an argument, not a separator. Newlines are
    separators too: `bash -c` executes every line, so a newline inside one
    "echo" segment must not hide the second line from the rules.
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
        elif char in "&|;\n\r":
            parts.append("".join(current))
            current = []
            while index + 1 < len(command) and command[index + 1] in "&|":
                index += 1
        else:
            current.append(char)
        index += 1

    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


# Allow-listed commands that can hide a write or a second command. Checked on
# the parsed words of a segment, not prefixes, because the dangerous argument
# can sit anywhere (`find . -name x -delete`, `sort -o out in`, sed's `w`).
_WRITE_PRIMITIVES = (
    ("find", ("-delete", "-exec", "-execdir", "-ok", "-okdir")),
    ("sort", ("-o", "--output")),
    ("sed", ("w",)),
)


def _hides_write_primitive(segment):
    """True if an allow-listed "read-only" command carries a write primitive."""
    words = _shell_words(segment)
    base = _base_command(words)
    args = words[1:]
    for name, primitives in _WRITE_PRIMITIVES:
        if base != name:
            continue
        for primitive in primitives:
            if primitive == "w":
                # sed's w command lives inside the script text, after a
                # delimiter (`s/x/y/w file`) or after space (`x;w file`)
                if any(_re.search(r"(?<![\w\\])w\s+\S", arg) for arg in args):
                    return True
            elif any(arg == primitive or arg.startswith(primitive)
                     for arg in args):
                return True
    return False


def _has_substitution(segment):
    """True if the segment can run hidden text: $(), backticks, or <().

    Conservative by design - single quotes would make these literal, but a
    false "ask" costs one dialog while a false "allow" runs unseen commands.
    """
    return "$(" in segment or "`" in segment or "<(" in segment


def decide(command):
    """Rate every part of a compound command; the strictest verdict wins."""
    verdicts = []
    for part in split_command(command):
        action = "ask"
        for pattern, rule in BASH_RULES.items():
            if fnmatch(part, pattern):
                action = rule
        # A read-only command that redirects into a real file overwrites data,
        # so an "allow" it earned disappears the moment it writes one.
        if action == "allow" and redirect_writes_file(part):
            action = "ask"
        # Same for commands that smuggle a second command or a write primitive
        # past a prefix rule: the allow was earned by the prefix, not by what
        # the segment actually does.
        if action == "allow" and (_has_substitution(part)
                                  or _hides_write_primitive(part)):
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


# --- shell analysis (shared by permission risk and Decision Oversight) --------
# Risk is derived from what a command will actually do, not a list of command
# names: the same executable can be harmless or destructive depending on its
# arguments, redirections and pipelines. Parsing is quote-aware so a filename or
# a quoted argument never changes the verdict.

import os as _os
import re as _re

# Commands that only read, when their arguments do not redirect or edit.
_READONLY_TOOLS = {
    "ls", "dir", "cat", "type", "head", "tail", "more", "less", "grep", "egrep",
    "fgrep", "findstr", "select-string", "rg", "ag", "wc", "sort", "uniq", "cut",
    "stat", "file", "du", "df", "echo", "printf", "pwd", "whoami", "hostname",
    "date", "env", "printenv", "which", "where", "where.exe", "tree", "basename",
    "dirname", "realpath", "md5sum", "sha1sum", "sha256sum", "diff", "cmp",
    "nl", "tac", "rev", "true", "false", "test", "comm", "find", "find.exe",
    "gci", "get-childitem", "cd", "pwd", "ls.exe", "cat.exe", "grep.exe",
}
# Commands that install, fetch, build or execute code: real side effects, but
# not inherently destructive on their own.
_ACTION_TOOLS = {
    "pip", "pip3", "python", "python3", "py", "node", "npm", "npx", "yarn",
    "pnpm", "apt", "apt-get", "brew", "cargo", "gem", "composer", "gradle",
    "mvn", "go", "dotnet", "make", "cmake", "tsc", "pytest", "py.test",
    "uv", "poetry", "virtualenv", "conda", "ruff", "flake8", "mypy", "docker",
}
# Commands that remove, overwrite, move or change permissions / config.
_DESTRUCTIVE_TOOLS = {
    "rm", "rmdir", "del", "erase", "shred", "mv", "move", "cp", "copy", "tee",
    "chmod", "chown", "chflags", "ln", "install", "curl", "wget", "scp", "sftp",
    "dd", "nc", "ncat", "kill", "killall", "mkfs", "fsck", "format", "renice",
}
_SHELL_CONSUMERS = ("sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh",
                    "python", "python3", "perl", "ruby", "node", "powershell",
                    "pwsh", "cmd")


def _shell_words(segment):
    """Split one command segment into words, honouring quotes and escapes."""
    words, cur, quote, i = [], [], None, 0
    seg = segment
    while i < len(seg):
        char = seg[i]
        if quote:
            if char == quote:
                quote = None
            else:
                cur.append(char)
        elif char == "\\" and i + 1 < len(seg):
            cur.append(seg[i + 1])
            i += 1
        elif char in "\"'":
            quote = char
        elif char.isspace():
            if cur:
                words.append("".join(cur))
                cur = []
        else:
            cur.append(char)
        i += 1
    if cur:
        words.append("".join(cur))
    return words


def _base_command(words):
    if not words:
        return ""
    name = words[0].replace("\\", "/").rsplit("/", 1)[-1]
    name = name.strip("'\"")
    name = _os.path.splitext(name.lower())[0]
    return name or name.lower()


def redirect_writes_file(command):
    """True if the command redirects output to a real file (truncating it).

    A stderr/dev-null sink (`2>nul`, `2>/dev/null`, `>/dev/null`) and fd dup
    (`2>&1`) are not writes and must never raise the risk.
    """
    seg = command
    i, n, quote = 0, len(seg), None
    while i < n:
        char = seg[i]
        if quote:
            quote = None if char == quote else quote
            i += 1
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        if char == ">":
            # A fd number in front and the & of 2>&1 are not file sinks.
            j = i + 1
            if j < n and seg[j] == ">":
                j += 1
            while j < n and seg[j] in " \t":
                j += 1
            if j < n and seg[j] == "&":        # 2>&1  /  >&2 : duplicate fd
                i = j + 1
                continue
            target = []
            while j < n and not seg[j].isspace() and seg[j] not in ";|&<>":
                target.append(seg[j])
                j += 1
            sink = "".join(target).strip().lower()
            if sink in ("nul", "/dev/null", "\\\\.\\nul", "null"):
                i = j
                continue
            if sink.startswith("&"):
                i = j
                continue
            return True
        i += 1
    return False


def _classify_segment(segment):
    words = _shell_words(segment)
    base = _base_command(words)
    args = [w for w in words[1:]]

    if base == "sed":
        return "high" if any(a.startswith("-i") for a in args) else "low"
    if base == "find":
        return "high" if ("-delete" in args or "-exec" in args or "-execdir" in args or "-ok" in args) else "low"
    if base in ("black", "isort", "autopep8", "gofmt", "rustfmt", "prettier"):
        return "medium"   # rewrites source in place
    if base in ("mkdir", "touch"):
        return "low"      # creating a dir/bumping mtime cannot destroy data
    if base == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in ("reset", "clean", "push", "rebase", "filter-branch"):
            return "high"
        if sub == "checkout" and any(a == "--" or a == "." or a.startswith("HEAD") for a in args):
            return "high"
        if sub == "restore" and args:
            return "high"
        if sub in ("log", "status", "diff", "show", "ls-files", "branch",
                   "rev-parse", "remote", "config", "describe", "shortlog"):
            return "low"
        return "medium"
    if base in _DESTRUCTIVE_TOOLS:
        return "critical" if _is_catastrophic(segment) else "high"
    if base in _READONLY_TOOLS:
        return "low"
    if base in _ACTION_TOOLS:
        return "medium"
    if base in _SHELL_CONSUMERS or base in ("eval", "exec", "xargs", "source", "."):
        return "high"
    return "medium"   # unknown executable: conservative


def _is_catastrophic(command):
    low = command.lower()
    return any(m in low for m in ("rm -rf /", "rm -fr /", "mkfs", ":(){", "dd if=",
                                  "--no-preserve-root", "shutdown", "reboot",
                                  "poweroff", "> /dev/sd", "format c:", "halt"))


def _strip_redirects(command):
    """Remove redirection clauses so split_command can find the real commands.

    split_command treats the `&` in `2>&1` and the `>` in `2>nul` as separators,
    which would leave a phantom `2>` fragment. Pulling redirects out first keeps
    the base-command classification honest; the redirect risk is judged
    separately on the original text by redirect_writes_file.
    """
    s = str(command)
    s = _re.sub(r"\d*>&\d", " ", s)                        # 2>&1, 1>&2 (fd dup)
    s = _re.sub(r"\d*>>?\s*[\"']?[^\"'\s;&|<>]+[\"']?", " ", s)  # > file, 2>nul
    s = _re.sub(r"\d*>>?", " ", s)                         # any leftover > / >>
    s = _re.sub(r"<\s*\S+", " ", s)                        # stdin redirects
    return s


def classify_bash(command):
    """(level, reason) for a shell command. Shared, quote-aware, conservative.

    Splits a compound command on its real separators, classifies each part by
    what it does, adds file-redirection as high, and treats download-into-shell
    as critical. The whole operation gets the strictest verdict of its parts.
    """
    text = str(command or "")
    low = text.lower()
    if _re.search(r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh)\b", low) and _re.search(r"\b(curl|wget|nc|ncat)\b", low):
        return "critical", "downloads a remote script and pipes it into a shell"
    if _is_catastrophic(text):
        return "critical", "can wipe data or take the machine down"

    levels = []
    if redirect_writes_file(text):
        levels.append("high")
    for part in split_command(_strip_redirects(text)):
        levels.append(_classify_segment(part))
    if _re.search(r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|python[\d.]*|perl|ruby|node|powershell|pwsh)\b", low):
        levels.append("high")

    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if not levels:
        return "low", "read-only or no side effects"
    worst = max(levels, key=lambda l: order[l])
    return worst, _RISK_REASON.get(worst, worst)


_RISK_REASON = {
    "low": "read-only inspection with no side effects.",
    "medium": "installs, builds, tests, or runs code.",
    "high": "changes, moves, deletes files or fetches remote content.",
    "critical": "can destroy data or run arbitrary remote code.",
}


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
