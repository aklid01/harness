"""What gets reviewed: the scope of a review, in a repo or out of one.

Four scopes, all of them cheap to compute:

``run``        the files this agent run changed (the default)
``uncommitted``everything the working tree has changed against HEAD
``staged``     what is in the index, ready to commit
``base:<ref>`` everything that differs from a chosen branch or commit

In a git repository the diff comes from git itself, so a reviewer sees the
same change the user would see in ``git diff``. Outside a repository there is
no diff engine, so the change is reconstructed from the checkpoint journal -
the before/after bytes the engine already keeps for every edit it makes.

Nothing here writes to the workspace. A review reads, and only reads.
"""

import base64
import dataclasses
import difflib
import os
from pathlib import Path

from . import findings as findings_mod

# Files that are never worth a reviewer's time: they are generated, vendored,
# or they are a lockfile whose churn is noise.
DEFAULT_EXCLUSIONS = (
    "*.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "pdm.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.min.map",
    "*.generated.*",
    "*.gen.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.snap",
    "node_modules/**",
    "vendor/**",
    "third_party/**",
    "dist/**",
    "build/**",
    "out/**",
    "target/**",
    ".venv/**",
    "venv/**",
    "__pycache__/**",
    "*.egg-info/**",
    ".git/**",
)

MODES = ("run", "uncommitted", "staged", "base")

_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".bz2",
    ".xz",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".pyc",
    ".pyo",
    ".wasm",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    ".sqlite",
    ".db",
)


@dataclasses.dataclass
class FileChange:
    path: str
    status: str = "M"
    diff: str = ""
    added: int = 0
    removed: int = 0
    binary: bool = False


@dataclasses.dataclass
class Scope:
    mode: str = "run"
    base: str = ""
    files: list = dataclasses.field(default_factory=list)
    skipped: list = dataclasses.field(default_factory=list)
    is_repo: bool = True
    total_files: int = 0
    truncated: bool = False
    head: str = ""

    @property
    def empty(self):
        return not self.files

    def paths(self):
        return [f.path for f in self.files]


# --------------------------------------------------------------------- git


def _git(root, args, timeout=20):
    """Run git in ``root`` and return (returncode, stdout).

    Thin over gitcmd.run; ``core.quotePath=false`` matters: without it git
    quotes a path that holds a non-ascii character, and the quoted name
    matches no file - the change would silently drop out of the review.
    """
    from .. import gitcmd

    return gitcmd.run(
        root, *args, timeout=timeout, config=("-c", "core.quotePath=false")
    )


def is_repo(root):
    code, out = _git(root, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def repo_root(root):
    """The repository's top level, or the workspace when it is not in one.

    Git reports paths relative to the repository root, not to the directory
    the user opened, so the two have to be reconciled before a path from git
    can be compared with a path from the engine.
    """
    code, out = _git(root, ["rev-parse", "--show-toplevel"])
    if code == 0 and out.strip():
        return Path(out.strip())
    return Path(root)


def _head(root):
    code, out = _git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if code == 0:
        return out.strip()
    return ""


def _porcelain(root):
    """(status, path) for every file git sees as changed."""
    code, out = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if code != 0:
        return []
    rows = []
    fields = out.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status = entry[:2].strip() or "?"
        path = entry[3:]
        if status[0] in ("R", "C"):
            # A rename carries the old path in the next field.
            index += 1
        rows.append((status, path))
    return rows


def _relative(root, path):
    """A path relative to the workspace, with forward slashes."""
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            relative = candidate.resolve().relative_to(Path(root).resolve())
        else:
            relative = candidate
    except (OSError, ValueError):
        return str(path).replace("\\", "/")
    return relative.as_posix()


# -------------------------------------------------------------- exclusions


def _matches(path, pattern):
    """Glob match, with ``**`` meaning "any depth".

    ``node_modules/**`` is everything under ``node_modules`` and nothing
    else; ``**/*.snap`` is any snapshot at any depth.
    """
    import fnmatch

    if "**" not in pattern:
        if "/" in pattern:
            return fnmatch.fnmatch(path, pattern)
        return fnmatch.fnmatch(os.path.basename(path), pattern) or fnmatch.fnmatch(
            path, pattern
        )

    head, _, tail = pattern.partition("**")
    head = head.rstrip("/")
    tail = tail.lstrip("/")
    if head:
        if not (path == head or path.startswith(head + "/")):
            return False
        rest = path[len(head) :].lstrip("/")
    else:
        rest = path
    if not tail:
        return True
    if fnmatch.fnmatch(rest, tail):
        return True
    return any(fnmatch.fnmatch(part, tail) for part in rest.split("/") if part)


def is_excluded(path, patterns):
    """True when a path is on the exclusion list."""
    normalised = path.replace("\\", "/")
    for pattern in patterns:
        if not pattern:
            continue
        if _matches(normalised, pattern):
            return True
    return False


def is_binary(path):
    if Path(path).suffix.lower() in _BINARY_SUFFIXES:
        return True
    try:
        with open(path, "rb") as handle:
            return b"\0" in handle.read(4096)
    except OSError:
        return True


# ------------------------------------------------------------ diff building


def _synthetic_new_file(path, text):
    """A unified diff for a file git does not know about yet."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def _text_diff(path, before, after):
    """A unified diff between two blobs of text."""
    old = (before or "").split("\n")
    new = (after or "").split("\n")
    body = list(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=3,
        )
    )
    return "\n".join(body)


def _decode(blob):
    if blob is None:
        return None
    try:
        return base64.b64decode(blob).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ scopes


def _diff_commands(root, mode, base):
    """The git diff invocations that make up this scope.

    A repository with no commits has no HEAD to diff against, so the index
    and the working tree are compared separately and the untracked files are
    picked up from the status.
    """
    head = _head(root)
    if mode == "staged":
        return [["diff", "--cached"]] if head else [["diff", "--cached", _EMPTY_TREE]]
    if mode == "base":
        return [["diff", base or "HEAD"]]
    if head:
        return [["diff", "HEAD"]]
    return [["diff"], ["diff", "--cached", _EMPTY_TREE]]


def _admit(scope, path, absolute, max_files, exclusions):
    """The shared skip ladder: file cap, settings exclusions, binary.

    True when the path may go on to produce a diff; otherwise the reason
    is already recorded in scope.skipped.
    """
    if len(scope.files) >= max_files:
        scope.truncated = True
        scope.skipped.append({"path": path, "reason": "over the file limit"})
        return False
    if is_excluded(path, exclusions):
        scope.skipped.append({"path": path, "reason": "excluded by the review settings"})
        return False
    if absolute.exists() and is_binary(absolute):
        scope.skipped.append({"path": path, "reason": "binary file"})
        return False
    return True


def _git_scope(root, mode, base, only_paths, exclusions, max_files):
    """Build a scope from git itself."""
    top = repo_root(root)
    scope = Scope(mode=mode, base=base, is_repo=True)
    scope.head = _head(top)
    untracked = set()
    statuses = {}
    for status, path in _porcelain(top):
        statuses[path] = status
        if status == "??":
            untracked.add(path)

    wanted = None
    if only_paths is not None:
        # An empty list means "this run changed nothing", which is different
        # from "no run context", which is None.
        wanted = {_relative(top, p) for p in only_paths}

    commands = _diff_commands(top, mode, base)
    if mode == "run" and wanted is not None:
        candidates = sorted(wanted)
    else:
        candidates = []
        for command in commands:
            _, out = _git(top, [*command, "--name-only"])
            for line in out.split("\n"):
                line = line.strip()
                if line and line not in candidates:
                    candidates.append(line)
        if mode in ("run", "uncommitted"):
            for path in sorted(untracked):
                if path not in candidates:
                    candidates.append(path)

    scope.total_files = len(candidates)
    for path in candidates:
        if not _admit(scope, path, top / path, max_files, exclusions):
            continue
        change = _git_file_change(top, path, commands, untracked, statuses)
        if change is None:
            scope.skipped.append({"path": path, "reason": "no readable diff"})
            continue
        scope.files.append(change)
    return scope


def _git_file_change(top, path, commands, untracked, statuses):
    """The diff for one file, from the commands that make up this scope."""
    status = statuses.get(path, "M")
    if path in untracked:
        try:
            text = (top / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        diff = _synthetic_new_file(path, text)
        return FileChange(path=path, status="A", diff=diff, added=text.count("\n") + 1)

    chunks, stats = [], []
    for command in commands:
        code, diff = _git(top, [*command, "--", path])
        if code == 0 and diff.strip():
            chunks.append(diff)
        code, stat = _git(top, [*command, "--numstat", "--", path])
        if code == 0 and stat.strip():
            stats.append(stat)
    if not chunks:
        return None
    diff = "\n".join(chunks)
    added = removed = 0
    binary = False
    for block in stats:
        for line in block.split("\n"):
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            if parts[0] == "-" or parts[1] == "-":
                binary = True
                continue
            try:
                added += int(parts[0])
                removed += int(parts[1])
            except ValueError:
                continue
    if binary:
        return None
    return FileChange(
        path=path, status=status or "M", diff=diff, added=added, removed=removed
    )


def _journal_scope(root, session_id, mode, only_paths, exclusions, max_files):
    """Build a scope without git, from the checkpoint journal.

    The journal is what the engine already writes for every edit it makes, so
    outside a repository this is the only honest record of what changed.
    """
    from .. import checkpoints

    scope = Scope(mode=mode, is_repo=False)
    entries = checkpoints.all_changes(session_id or "")
    if only_paths is not None:
        # An empty list means "this run changed nothing", which is different
        # from "no run context", which is None.
        wanted = {_relative(root, p) for p in only_paths}
        entries = [
            e
            for e in entries
            if _relative(root, e.get("path", "")) in wanted
        ]

    seen = {}
    for entry in entries:
        path = _relative(root, entry.get("path", ""))
        if not path:
            continue
        seen[path] = entry

    scope.total_files = len(seen)
    for path in sorted(seen):
        if not _admit(scope, path, Path(root) / path, max_files, exclusions):
            continue
        entry = seen[path]
        before = _decode(entry.get("before"))
        after = _decode(entry.get("after"))
        if before is None and after is None:
            scope.skipped.append({"path": path, "reason": "no readable diff"})
            continue
        diff = _text_diff(path, before, after)
        if not diff.strip():
            scope.skipped.append({"path": path, "reason": "no readable diff"})
            continue
        status = "A" if before is None else ("D" if after is None else "M")
        added = sum(1 for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff.split("\n") if line.startswith("-") and not line.startswith("---"))
        scope.files.append(
            FileChange(path=path, status=status, diff=diff, added=added, removed=removed)
        )
    return scope


def detect(
    root,
    mode="run",
    base="",
    only_paths=None,
    session_id="",
    exclusions=DEFAULT_EXCLUSIONS,
    max_files=40,
):
    """Work out what a review should look at.

    ``root`` is the workspace, ``mode`` one of MODES, ``base`` the ref for
    ``base:<ref>``, ``only_paths`` the files this run touched (used by the
    ``run`` scope) and ``session_id`` the session whose journal to read when
    there is no git.
    """
    root = Path(root)
    if not root.exists():
        return Scope(mode=mode, base=base, is_repo=False)
    if is_repo(root):
        return _git_scope(root, mode, base, only_paths, exclusions, max_files)
    return _journal_scope(root, session_id, mode, only_paths, exclusions, max_files)


def detect_for_settings(
    root, settings, *, mode="run", base="", only_paths=None, session_id=""
):
    """detect() with the exclusions and file cap a review's settings name.

    One home for that juggling, so the engine and both of its front doors
    (the sidecar and the CLI) parse the scope identically.
    """
    from . import config as config_mod

    return detect(
        root,
        mode=mode,
        base=base,
        only_paths=only_paths,
        session_id=session_id,
        exclusions=tuple(settings.get("exclusions") or DEFAULT_EXCLUSIONS),
        max_files=int(settings.get("max_files") or config_mod.DEFAULT_MAX_FILES),
    )


def valid_scope(text):
    """True when the text names a scope the engine understands."""
    raw = (text or "").strip().lower()
    if not raw:
        return True
    if raw in MODES:
        return True
    if raw.startswith("base:") and raw[5:].strip():
        return True
    if raw.startswith("against:") and raw[8:].strip():
        return True
    return False


def parse_scope(text):
    """Parse a scope argument: "run", "uncommitted", "staged", "base:main".

    Returns (mode, base). Anything unrecognised falls back to "run", because
    a typo should not silently widen what gets sent to a provider.
    """
    raw = (text or "").strip()
    if not raw:
        return "run", ""
    lowered = raw.lower()
    if lowered in MODES:
        return lowered, ""
    if lowered.startswith("base:"):
        ref = raw[5:].strip()
        if ref:
            return "base", ref
    if lowered.startswith("against:"):
        ref = raw[8:].strip()
        if ref:
            return "base", ref
    return "run", ""


def known_lines(scope):
    """The lines each reviewed file's diff shows, for finding validation."""
    lines = {}
    for change in scope.files:
        lines.setdefault(change.path, set()).update(
            findings_mod.diff_lines(change.diff).get(change.path, set())
        )
    return lines
