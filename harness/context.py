"""Late injection: a small block appended just before we send.

It goes at the END of the message list so the stable prefix in front of it
stays cached.
"""

import hashlib
import subprocess
from datetime import datetime

from . import paths
from .todos import todos_prompt
from pathlib import Path

LABELS = {"M": "modified", "D": "deleted", "A": "added", "??": "new"}


def git(command):
    result = subprocess.run(
        f"git {command}", shell=True, capture_output=True, text=True,
        cwd=str(paths.project_root()), timeout=10,
    )
    return result.stdout


def file_hash(path):
    file = Path(path)
    return hashlib.md5(file.read_bytes()).hexdigest() if file.is_file() else None


def git_state():
    """path -> (status, content hash) for every file git sees as changed."""
    state = {}
    for line in git("status --porcelain").splitlines():
        path = line[3:]
        state[path] = (line[:2].strip(), file_hash(path))
    return state


LAST_STATE = {}


def file_changes():
    """Files whose status or contents moved since the previous turn."""
    global LAST_STATE
    key = str(paths.project_root())
    now = git_state()
    prior = LAST_STATE.get(key, {})
    changed = {p: v[0] for p, v in now.items() if prior.get(p) != v}
    LAST_STATE[key] = now
    return changed


def changes_note():
    changed = file_changes()
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


def reminder():
    """The block we append to the messages on every turn."""
    return {
        "role": "user",
        "content": (
            "<env>\n"
            f"time: {datetime.now():%Y-%m-%d %H:%M}\n"
            f"git branch: {git('branch --show-current').strip() or '(detached)'}\n"
            "</env>" + todos_note() + changes_note()
        ),
    }
