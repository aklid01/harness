"""Per-session journal of AI file edits, so a rewind can restore code.

write_file/str_replace snapshot a file's content before and after the edit and
record it against the transcript position (session.WRITTEN at write time, which
is the assistant-message index). rewind uses the pre-edit content to restore the
files a rewound portion of the conversation changed.

Safety:
- Only files this journal recorded are ever touched; shell edits are not tracked.
- restore_since reverts to the *pre-change* content captured for the earliest
  change at/after the rewind point, so it undoes exactly what the AI did.
- preview() flags files whose current content differs from what the agent last
  wrote ("dirty") so the UI can warn about later manual edits before restoring.
- Captured content is stored as base64 of the raw BYTES: a text-mode round
  trip translated newlines and persisted U+FFFD for undecodable bytes, so a
  rewind of a cp1252 or LF-only file restored corrupted content.
- Every function is best-effort and never raises into the caller.
"""

import base64
import json
from pathlib import Path

from . import paths


def _dir(session_id) -> Path:
    # Stored inside the owning workspace's session dir, so identical session
    # ids in different workspaces never collide (callers run within a bound
    # workspace context).
    return paths.session_dir() / "_checkpoints" / str(session_id)


def _index(session_id) -> Path:
    return _dir(session_id) / "index.json"


def read_or_none(path):
    """The file's raw bytes, base64-encoded; None when it cannot be captured."""
    try:
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError:
        return None


def _load(session_id):
    try:
        data = json.loads(_index(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"changes": []}
    except (OSError, ValueError):
        return {"changes": []}


def _save(session_id, data):
    try:
        d = _dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        _index(session_id).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def record(session_id, index, path, before, after):
    if not path:
        return
    try:
        data = _load(session_id)
        data.setdefault("changes", []).append(
            {"index": int(index), "path": str(path), "before": before, "after": after}
        )
        _save(session_id, data)
    except (OSError, ValueError, TypeError):
        pass


def _at_or_after(session_id, count):
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    return [
        c
        for c in _load(session_id).get("changes", [])
        if int(c.get("index", 0)) >= count
    ]


def changes(session_id):
    """Every recorded change for this session, oldest first.

    The review flow uses this to reconstruct what a run changed in a
    workspace that is not a git repository: the journal holds the exact
    bytes before and after each edit.
    """
    try:
        return list(_load(session_id).get("changes", []))
    except (TypeError, ValueError):
        return []


def all_changes(session_id=""):
    """The changes of one session, or of every session in the workspace.

    A session-scoped review asks for one session. A review that is not
    attached to a session - the headless command line - asks for
    everything the engine has changed in this workspace.
    """
    if session_id:
        return changes(session_id)
    base = paths.session_dir() / "_checkpoints"
    found = []
    try:
        entries = [item for item in base.iterdir() if item.is_dir()]
    except OSError:
        return []
    for entry in entries:
        found.extend(changes(entry.name))
    return found


def preview(session_id, count):
    """What a rewind to `count` would restore, without changing anything."""
    changes = _at_or_after(session_id, count)
    by_path = {}
    for c in changes:
        p = c.get("path")
        if not p:
            continue
        if p not in by_path:
            by_path[p] = {"earliest": c, "latest": c}
        else:
            if int(c.get("index", 0)) < int(by_path[p]["earliest"].get("index", 0)):
                by_path[p]["earliest"] = c
            if int(c.get("index", 0)) >= int(by_path[p]["latest"].get("index", 0)):
                by_path[p]["latest"] = c
    rows = []
    for p, info in by_path.items():
        current = read_or_none(p)
        before = info["earliest"].get("before")
        after = info["latest"].get("after")
        rows.append(
            {
                "path": p,
                "action": "delete" if before is None else "restore",
                "dirty": current != after,
            }
        )
    return rows


def restore_since(session_id, count):
    """Restore files changed at/after `count` to their pre-change content."""
    earliest = {}
    for c in _at_or_after(session_id, count):
        p = c.get("path")
        if not p:
            continue
        if p not in earliest or int(c.get("index", 0)) < int(
            earliest[p].get("index", 0)
        ):
            earliest[p] = c
    restored = []
    for p, c in earliest.items():
        before = c.get("before")
        try:
            if before is None:
                Path(p).unlink(missing_ok=True)
            else:
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                Path(p).write_bytes(base64.b64decode(before))
            restored.append(p)
        except (OSError, ValueError):
            pass
    return restored


def delete(session_id):
    import shutil

    try:
        shutil.rmtree(_dir(session_id), ignore_errors=True)
    except OSError:
        pass
