"""Late injection: a small block appended just before we send.

It goes at the END of the message list so the stable prefix in front of it
stays cached.
"""

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

from . import paths
from .todos import todos_prompt

LABELS = {"M": "modified", "D": "deleted", "A": "added", "??": "new"}


def git(command):
    """Run a git command in the workspace; utf-8 output, failures are empty.

    Never raises: the injection is best-effort context, and a locked index or
    a disappearing git binary must not kill a turn before the model call.
    """
    try:
        result = subprocess.run(
            f"git {command}",
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(paths.project_root()),
            timeout=10,
        )
        return result.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def is_git_worktree():
    """True only when the workspace is a git repository git can actually serve.

    A plain folder is not an error: it just means git commands will fail, and
    the model deserves to be told that plainly instead of being handed an
    empty branch name that reads like a detached HEAD.
    """
    try:
        probe = subprocess.run(
            "git rev-parse --is-inside-work-tree",
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(paths.project_root()),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def file_hash(path):
    try:
        file = Path(path)
        return hashlib.md5(file.read_bytes()).hexdigest() if file.is_file() else None
    except OSError:
        # A file locked by another process (log, sync client) must not kill
        # the turn: "hash unknown" just means this file cannot be diffed.
        return None


def _porcelain_path(raw):
    """Decode one `git status --porcelain` path field.

    git quotes paths with special characters ("\\346\\..." octal escapes) and
    writes renames as `old -> new`; both need decoding before the path can be
    hashed or shown to the model.
    """
    raw = raw.strip()
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
        try:
            raw = (
                raw.encode("latin-1")
                .decode("unicode_escape")
                .encode("latin-1")
                .decode("utf-8")
            )
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
            pass
    return raw


def git_state():
    """path -> (status, content hash) for every file git sees as changed."""
    state = {}
    for line in git("status --porcelain").splitlines():
        if len(line) < 4:
            continue
        path = _porcelain_path(line[3:])
        if not path:
            continue
        state[path] = (line[:2].strip(), file_hash(Path(paths.project_root()) / path))
    return state


LAST_STATE = {}


def file_changes(baseline=None):
    """Files whose status or contents moved since ``baseline``.

    ``baseline`` is owned by the caller (one snapshot per turn, held by the
    Runner), so the note means "changed since your last turn" - not "since
    your last model request", and two sessions on one workspace never race a
    shared snapshot. Called without a baseline, falls back to the legacy
    module-level state.
    """
    key = str(paths.project_root())
    now = git_state()
    if baseline is None:
        prior = LAST_STATE.get(key, {})
        LAST_STATE[key] = now
    else:
        prior = baseline
    return {p: v[0] for p, v in now.items() if prior.get(p) != v}


def changes_note(baseline=None):
    changed = file_changes(baseline)
    if not changed:
        return ""
    lines = [f"{LABELS.get(code, code)}: {path}" for path, code in changed.items()]
    return (
        "\n<system-reminder>\n"
        "These files changed since your last turn. Read them again before "
        "editing:\n" + "\n".join(lines) + "\n</system-reminder>"
    )


def todos_note():
    plan = todos_prompt()
    return f"\n<todos>\n{plan}\n</todos>" if plan else ""


def reminder(baseline=None):
    """The block we append to the messages on every turn."""
    if is_git_worktree():
        where = f"git branch: {git('branch --show-current').strip() or '(detached)'}"
        changes = changes_note(baseline)
    else:
        where = "git: this workspace is not a git repository; git commands will fail"
        changes = ""
    return {
        "role": "user",
        "content": (
            "<env>\n"
            f"time: {datetime.now():%Y-%m-%d %H:%M}\n"
            f"{where}\n"
            "</env>" + todos_note() + changes
        ),
    }
