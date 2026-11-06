"""How risky is one shell command, on its own words.

Everything here is pure string analysis: no policy, no mode, no workspace -
the same command classifies the same way anywhere. permissions.py builds its
allow/ask/deny policy on top of it, and oversight reads the raw risk level.
"""

import os as _os
import re as _re


def _ends_with_redirect(text):
    """Does the text so far end on a redirect operator ('>')?"""
    for char in reversed(text):
        if not char.isspace():
            return char == ">"
    return False


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
        elif char == "&" and _ends_with_redirect(current):
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


def _is_assignment(word):
    """True for `NAME=value`: a shell variable assignment, not a command."""
    name, sep, _ = word.partition("=")
    if not sep or not name:
        return False
    head = name[0]
    if not (head.isascii() and (head.isalpha() or head == "_")):
        return False
    return all(char.isascii() and (char.isalnum() or char == "_") for char in name)


def _assignment_only(words):
    """True when the words are nothing but shell variable assignments.

    `P1=/tmp/x` changes nothing on disk, but judged as an unknown executable
    it fell through to "ask" - so a read-only probe that set a variable first
    was refused in Ask mode.
    """
    return bool(words) and all(_is_assignment(word) for word in words)


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
                if any(_re.search(r"(?<![\w\\])w\s+\S", arg) for arg in args):
                    return True
            elif any(arg == primitive or arg.startswith(primitive) for arg in args):
                return True
    return False


def _has_substitution(segment):
    """True if the segment can run hidden text: $(), backticks, or <().

    Conservative by design - single quotes would make these literal, but a
    false "ask" costs one dialog while a false "allow" runs unseen commands.
    """
    return "$(" in segment or "`" in segment or "<(" in segment


_READONLY_TOOLS = {
    "ls",
    "dir",
    "cat",
    "type",
    "head",
    "tail",
    "more",
    "less",
    "grep",
    "egrep",
    "fgrep",
    "findstr",
    "select-string",
    "rg",
    "ag",
    "wc",
    "sort",
    "uniq",
    "cut",
    "stat",
    "file",
    "du",
    "df",
    "echo",
    "printf",
    "pwd",
    "whoami",
    "hostname",
    "date",
    "env",
    "printenv",
    "which",
    "where",
    "where.exe",
    "tree",
    "basename",
    "dirname",
    "realpath",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "diff",
    "cmp",
    "nl",
    "tac",
    "rev",
    "true",
    "false",
    "test",
    "comm",
    "find",
    "find.exe",
    "gci",
    "get-childitem",
    "cd",
    "ls.exe",
    "cat.exe",
    "grep.exe",
}


_ACTION_TOOLS = {
    "pip",
    "pip3",
    "python",
    "python3",
    "py",
    "node",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "apt",
    "apt-get",
    "brew",
    "cargo",
    "gem",
    "composer",
    "gradle",
    "mvn",
    "go",
    "dotnet",
    "make",
    "cmake",
    "tsc",
    "pytest",
    "py.test",
    "uv",
    "poetry",
    "virtualenv",
    "conda",
    "ruff",
    "flake8",
    "mypy",
    "docker",
}

_DESTRUCTIVE_TOOLS = {
    "rm",
    "rmdir",
    "del",
    "erase",
    "shred",
    "mv",
    "move",
    "cp",
    "copy",
    "tee",
    "chmod",
    "chown",
    "chflags",
    "ln",
    "install",
    "curl",
    "wget",
    "scp",
    "sftp",
    "dd",
    "nc",
    "ncat",
    "kill",
    "killall",
    "mkfs",
    "fsck",
    "format",
    "renice",
}
_SHELL_CONSUMERS = (
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "csh",
    "tcsh",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "powershell",
    "pwsh",
    "cmd",
)


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
            j = i + 1
            if j < n and seg[j] == ">":
                j += 1
            while j < n and seg[j] in " \t":
                j += 1
            if j < n and seg[j] == "&":
                if j + 1 < n and seg[j + 1].isdigit():
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

    if _assignment_only(words):
        return "low"

    if base == "sed":
        return "high" if any(a.startswith("-i") for a in args) else "low"
    if base == "find":
        return (
            "high"
            if (
                "-delete" in args
                or "-exec" in args
                or "-execdir" in args
                or "-ok" in args
            )
            else "low"
        )
    if base in ("black", "isort", "autopep8", "gofmt", "rustfmt", "prettier"):
        return "medium"
    if base in ("mkdir", "touch"):
        return "low"
    if base == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in ("reset", "clean", "push", "rebase", "filter-branch"):
            return "high"
        if sub == "checkout" and any(
            a == "--" or a == "." or a.startswith("HEAD") for a in args
        ):
            return "high"
        if sub == "restore" and args:
            return "high"
        if sub in (
            "log",
            "status",
            "diff",
            "show",
            "ls-files",
            "branch",
            "rev-parse",
            "remote",
            "config",
            "describe",
            "shortlog",
        ):
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
    return "medium"


def _outside_quotes(text):
    """The command with quoted spans blanked: their content is data, not code."""
    out = []
    quote = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
                out.append(" ")
            continue
        if char in "\"'":
            quote = char
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)


def _has_shell_consumer(low):
    """True when some command in the line feeds its arguments to a shell."""
    return (
        _re.search(r"\b(sh|bash|zsh|dash|ksh|eval|exec|xargs|source)\b", low)
        is not None
    )


_CATASTROPHIC_COMMANDS = {"mkfs", "shutdown", "reboot", "poweroff", "halt", "format"}

_CATASTROPHIC_PATTERNS = (
    ":(){",
    "--no-preserve-root",
    "> /dev/sd",
    "rm -rf /",
    "rm -fr /",
)


def _is_catastrophic(command):
    for segment in split_command(command):
        words = _shell_words(segment)
        base = _base_command(words)
        if base in _CATASTROPHIC_COMMANDS:
            return True
        if base == "dd" and any(arg.startswith("if=") for arg in words[1:]):
            return True
    low = command.lower()
    return any(pattern in low for pattern in _CATASTROPHIC_PATTERNS)


def _strip_redirects(command):
    """Remove redirection clauses so split_command can find the real commands.

    split_command treats the `&` in `2>&1` and the `>` in `2>nul` as separators,
    which would leave a phantom `2>` fragment. Pulling redirects out first keeps
    the base-command classification honest; the redirect risk is judged
    separately on the original text by redirect_writes_file.
    """
    s = str(command)
    s = _re.sub(r"\d*>&\d", " ", s)
    s = _re.sub(r"\d*>>?\s*[\"']?[^\"'\s;&|<>]+[\"']?", " ", s)
    s = _re.sub(r"\d*>>?", " ", s)
    s = _re.sub(r"<\s*\S+", " ", s)
    return s


def classify_bash(command):
    """(level, reason) for a shell command. Shared, quote-aware, conservative.

    Splits a compound command on its real separators, classifies each part by
    what it does, adds file-redirection as high, and treats download-into-shell
    as critical. The whole operation gets the strictest verdict of its parts.
    """
    text = str(command or "")
    low = text.lower()

    outside = _outside_quotes(low)
    scanned = low if _has_shell_consumer(outside) else outside
    if _re.search(r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh)\b", scanned) and _re.search(
        r"\b(curl|wget|nc|ncat)\b", scanned
    ):
        return "critical", "downloads a remote script and pipes it into a shell"
    if _is_catastrophic(scanned):
        return "critical", "can wipe data or take the machine down"

    levels = []
    if redirect_writes_file(text):
        levels.append("high")
    for part in split_command(_strip_redirects(text)):
        levels.append(_classify_segment(part))
    if _re.search(
        r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|python[\d.]*|perl|ruby|node|powershell|pwsh)\b",
        scanned,
    ):
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
