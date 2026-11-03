"""The report: what a review produced, and where it is kept.

A report has to survive the turn that made it. The card, the agent's
context and the next session all read the same saved document, so there is
one shape for it and one place it lives.

The saved report belongs to one session of one workspace. It is never
reachable from another session: the path is scoped to both.
"""

import json
import time
from pathlib import Path

from .. import paths

REVIEW_DIR = "_reviews"


def _dir(session_id):
    return paths.session_dir() / REVIEW_DIR / str(session_id or "default")


def _index(session_id):
    return _dir(session_id) / "index.json"


def _path(session_id, review_id):
    return _dir(session_id) / f"{review_id}.json"


def path_for(session_id, review_id):
    """Where a report lives, so the agent can read it on demand."""
    return str(_path(session_id, review_id))


def new_id():
    import secrets

    return "rv-" + secrets.token_hex(6)


def save(session_id, report, *, newest=True):
    """Write the report, record it as the newest, and return it.

    Save order is recorded in index.json because file mtimes collide when
    two reports are written within one clock tick, and "latest" must
    still name the report that was saved last. Re-saves that only mutate
    an old report (dismiss, mark_stale) pass newest=False.
    """
    try:
        target = _path(session_id, report.get("review_id") or new_id())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        return report
    if newest and report.get("review_id"):
        try:
            _index(session_id).write_text(
                json.dumps({"latest": report["review_id"]}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
    return report


def load(session_id, review_id):
    """One saved report, or None."""
    try:
        data = json.loads(_path(session_id, review_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def latest(session_id):
    """The most recent report for this session, or None.

    The recorded save order wins because mtimes can collide within one
    clock tick; a missing index (or one pointing at a deleted report)
    falls back to mtime order for directories written by older builds.
    """
    try:
        data = json.loads(_index(session_id).read_text(encoding="utf-8"))
        review_id = data.get("latest") if isinstance(data, dict) else None
    except (OSError, ValueError):
        review_id = None
    if review_id:
        document = load(session_id, review_id)
        if document is not None:
            return document
    try:
        entries = [
            item
            for item in _dir(session_id).glob("rv-*.json")
            if item.is_file()
        ]
    except OSError:
        return None
    if not entries:
        return None
    newest = max(entries, key=lambda item: item.stat().st_mtime_ns)
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def all_reports(session_id):
    """Every report for this session, newest first."""
    try:
        entries = [item for item in _dir(session_id).glob("rv-*.json") if item.is_file()]
    except OSError:
        return []
    entries.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    reports = []
    for item in entries:
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            reports.append(data)
    return reports


def dismiss(session_id, review_id, finding_id):
    """Mark one finding as dismissed and save the report back."""
    report = load(session_id, review_id)
    if report is None:
        return None
    dismissed = set(report.get("dismissed") or [])
    dismissed.add(str(finding_id))
    report["dismissed"] = sorted(dismissed)
    save(session_id, report, newest=False)
    return report


def mark_stale(session_id, upto_index):
    """Mark every report made at or after a transcript position as stale.

    A rewind throws away the work that came after it, so a review of that
    work no longer describes anything. The report is kept - it may still be
    interesting - but it is marked, and the card says so.
    """
    changed = []
    for report in all_reports(session_id):
        index = int(report.get("message_index") or 0)
        if index >= int(upto_index) and not report.get("stale"):
            report["stale"] = True
            save(session_id, report, newest=False)
            changed.append(report)
    return changed


def cache_key(scope, models=()):
    """A key that changes when the reviewed content or the reviewers change.

    Two runs over the same unchanged files, by the same reviewers, produce
    the same key - which is how a repeated review returns the cached report
    instead of spending money on the same answer. A different set of
    reviewers is a different answer, so it gets a different key.
    """
    import hashlib

    digest = hashlib.sha256()
    digest.update(scope.mode.encode("utf-8"))
    digest.update(scope.base.encode("utf-8"))
    for model_id in sorted(models):
        digest.update(b"\0")
        digest.update(str(model_id).encode("utf-8"))
    for change in scope.files:
        digest.update(change.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(change.diff.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def cached(session_id, key):
    """The cached report for this exact content, if there is one."""
    if not key:
        return None
    for report in all_reports(session_id):
        if report.get("cache_key") == key and not report.get("stale"):
            return report
    return None


def build(
    review_id,
    scope,
    merged,
    *,
    redactions=0,
    seconds=0.0,
    message_index=0,
    cache_key="",
    triggered_by="manual",
    extra=None,
):
    """The report document, in the shape the renderer and the agent read."""
    report = {
        "review_id": review_id,
        "created_at": time.time(),
        "verdict": merged.get("verdict", "clean"),
        "verdict_label": merged.get("verdict_label", "clean"),
        "findings": [f.to_dict() for f in merged.get("findings", [])],
        "suppressed_count": len(merged.get("suppressed", [])),
        "by_severity": merged.get("by_severity", {}),
        "by_category": merged.get("by_category", {}),
        "models": merged.get("models", []),
        "threshold": merged.get("threshold", "low"),
        "redacted": int(redactions),
        "seconds": round(float(seconds), 2),
        "message_index": int(message_index),
        "cache_key": cache_key,
        "triggered_by": triggered_by,
        "stale": False,
        "dismissed": [],
        "scope": {
            "mode": scope.mode,
            "base": scope.base,
            "is_repo": scope.is_repo,
            "total_files": scope.total_files,
            "files": [
                {
                    "path": change.path,
                    "status": change.status,
                    "added": change.added,
                    "removed": change.removed,
                }
                for change in scope.files
            ],
            "skipped": list(scope.skipped),
        },
    }
    if extra:
        report.update(extra)
    return report


def summary(report, limit=6):
    """The short version of a report, for the agent's context."""
    findings = report.get("findings") or []
    lines = [
        "Multi-model review of the change you just made.",
        f"Verdict: {report.get('verdict_label', 'clean')}. "
        f"{len(findings)} finding(s) across "
        f"{len(report.get('scope', {}).get('files', []))} file(s).",
    ]
    if not findings:
        lines.append("No issues were found.")
        return "\n".join(lines)
    lines.append("")
    for finding in findings[:limit]:
        location = finding.get("file", "")
        if finding.get("line"):
            location = f"{location}:{finding['line']}"
        lines.append(
            f"[{finding.get('id')}] {finding.get('severity', 'medium')} "
            f"({finding.get('category', 'other')}) {location}\n"
            f"  {finding.get('summary', '')}\n"
            f"  why: {finding.get('why', '')}\n"
            f"  fix: {finding.get('fix', '')}"
        )
    if len(findings) > limit:
        lines.append("")
        lines.append(
            f"...and {len(findings) - limit} more finding(s). The full report is "
            "readable on demand."
        )
    return "\n".join(lines)


def as_json(report):
    """The report as a JSON string, for the non-interactive output mode."""
    return json.dumps(report, ensure_ascii=False, indent=2)
