"""The plan as a first-class, session-scoped artifact.

Authoritative and apart from the planner's throwaway chat: one record per
session, under that session's own directory, holding the original task, the
plan, its completion state and the workflow phase. Keyed by session and stored
under the active workspace's session dir, so a plan never leaks between tasks,
tabs or workspaces and it survives a restart, so a reload shows the plan that
was actually produced rather than a fresh, empty one.
"""

import json

from . import paths
from . import session as session_mod

COMPLETE = "complete"
PARTIAL = "partial"
UNUSABLE = "unusable"
CANCELLED = "cancelled"


def _path(session_id):
    return paths.session_dir() / "_plans" / f"{session_id}.json"


def load(session_id):
    try:
        data = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(session_id, **fields):
    """Merge ``fields`` into the session's plan record and persist it.

    Merge, not replace: an incremental update (say, phase -> building) keeps the
    task and plan already stored.
    """
    record = load(session_id) or {}
    record["session_id"] = session_id
    record.update(fields)
    target = _path(session_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(target)
    except OSError:
        pass
    return record


def forget(session_id):
    try:
        _path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


def current_save(**fields):
    """Persist for the session attached to the current context, if any."""
    return save(session_mod.current_id(), **fields)
