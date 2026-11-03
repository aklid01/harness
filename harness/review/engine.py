"""Running the reviewers.

Each reviewer is a model with no tools and no shell. It is handed a diff and
some context, and it answers with findings. The reviewers run independently -
none of them sees another's answer - and one of them failing, timing out or
being rate limited has no effect on the others.

The flow is the same every time:

    scope -> redact -> build -> ask -> validate -> merge -> report

Everything in it is read-only. The reviewer holds no tools, the diff is
untrusted text, and the only thing this module writes is the report.
"""

import concurrent.futures
import time
from pathlib import Path

from .. import paths as paths_mod
from .. import providers
from ..llm import call_llm
from . import config as config_mod
from . import findings as findings_mod
from . import prompt as prompt_mod
from . import redact as redact_mod
from . import report as report_mod
from . import scope as scope_mod
from .aggregate import ModelOutcome, merge

STAGES = (
    "offer",
    "started",
    "progress",
    "finding",
    "completed",
    "failed",
    "nothing",
    "cancelled",
    "stale",
)


def _noop(stage, payload):
    return None


def _label_for(model_id):
    """A name to show a user: the model's own name when it has one."""
    try:
        entry = providers.get_model(model_id) or {}
    except Exception:  # noqa: BLE001
        entry = {}
    name = entry.get("name") or ""
    model = entry.get("model") or ""
    if name and model and name != model:
        return f"{name} ({model})"
    return name or model or model_id


def _contexts(root, scope, full_context, max_file_bytes):
    """The file bodies to show alongside the diff, when asked for."""
    if not full_context:
        return {}
    contexts = {}
    for change in scope.files:
        path = Path(root) / change.path
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > max_file_bytes:
            text = text[:max_file_bytes] + "\n... (file truncated)"
        contexts[change.path] = text
    return contexts


def _redact_scope(scope):
    """Redact every diff in the scope. Line counts never change."""
    hits = []
    for change in scope.files:
        clean, found = redact_mod.redact_diff(change.diff)
        change.diff = clean
        hits.extend(found)
    return len(hits)


def _fit_scope(scope, max_diff_bytes, max_file_bytes):
    """Drop the files that are too big, and say which ones were dropped."""
    skipped = list(scope.skipped)
    kept = []
    used = 0
    for change in scope.files:
        size = len(change.diff.encode("utf-8", errors="replace"))
        if size > max_file_bytes:
            skipped.append({"path": change.path, "reason": "the diff is too large"})
            continue
        if used + size > max_diff_bytes:
            skipped.append({"path": change.path, "reason": "the review is over its size limit"})
            continue
        used += size
        kept.append(change)
    scope.files = kept
    scope.skipped = skipped
    return used


def _run_reviewer(reviewer, scope, contexts, redactions, settings, cancel, emit, root):
    """Ask one reviewer for its findings. Never raises."""
    model_id = reviewer.get("model_id") or ""
    label = reviewer.get("label") or _label_for(model_id)
    outcome = ModelOutcome(model_id=model_id, label=label)
    started = time.monotonic()

    def note(stage, payload):
        if emit:
            emit(stage, payload)

    messages = prompt_mod.build_messages(
        scope,
        focus=reviewer.get("focus") or "",
        contexts=contexts,
        extra=str(settings.get("prompt") or ""),
        redactions=redactions,
    )

    try:
        # A reviewer runs on its own thread, and a thread does not inherit
        # the workspace root the session bound. Without this, a reviewer
        # would look for files in the wrong place.
        with paths_mod.rooted(root):
            with providers.use(model_id):
                note(
                    "progress",
                    {"model_id": model_id, "label": label, "state": "running"},
                )
                raw = None
                last_error = None
                for attempt in range(2):
                    if cancel is not None and cancel.is_set():
                        outcome.status = "cancelled"
                        outcome.error = "cancelled"
                        return outcome
                    try:
                        message, usage = call_llm(messages, tools=[], cancel=cancel)
                    except Exception as failure:  # noqa: BLE001 - one model's problem
                        # The transport already retried inside call_llm; a
                        # failure here is that model being unreachable, not
                        # a bad reply.
                        outcome.status = "failed"
                        outcome.error = f"{type(failure).__name__}: {failure}"
                        outcome.seconds = time.monotonic() - started
                        note(
                            "progress",
                            {
                                "model_id": model_id,
                                "label": label,
                                "state": "failed",
                                "error": outcome.error,
                            },
                        )
                        return outcome
                    content = getattr(message, "content", "") or ""
                    outcome.prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    outcome.completion_tokens = int(usage.get("completion_tokens") or 0)
                    try:
                        raw = findings_mod.parse(content)
                        break
                    except findings_mod.ReviewFormatError as failure:
                        # A malformed reply is worth one more try: models
                        # often wrap their JSON in a sentence on the first
                        # attempt.
                        last_error = failure
                        if attempt == 0:
                            continue
                        break
                if raw is None:
                    raise findings_mod.ReviewFormatError(str(last_error))
    except findings_mod.ReviewFormatError as failure:
        outcome.status = "failed"
        outcome.error = f"the reply was not a usable review ({failure})"
        outcome.seconds = time.monotonic() - started
        note(
            "progress",
            {
                "model_id": model_id,
                "label": label,
                "state": "failed",
                "error": outcome.error,
            },
        )
        return outcome
    except Exception as failure:  # noqa: BLE001 - one model's problem
        outcome.status = "failed"
        outcome.error = f"{type(failure).__name__}: {failure}"
        outcome.seconds = time.monotonic() - started
        note(
            "progress",
            {
                "model_id": model_id,
                "label": label,
                "state": "failed",
                "error": outcome.error,
            },
        )
        return outcome

    allowed = [change.path for change in scope.files]
    known = scope_mod.known_lines(scope)
    kept, rejected = findings_mod.validate(raw, allowed, known)
    outcome.findings = kept
    outcome.rejected = rejected
    outcome.status = "ran"
    outcome.seconds = time.monotonic() - started
    note(
        "progress",
        {
            "model_id": model_id,
            "label": label,
            "state": "done",
            "findings": len(kept),
            "rejected": len(rejected),
            "seconds": outcome.seconds,
        },
    )
    for finding in kept:
        note("finding", {"model_id": model_id, "label": label, "finding": finding.to_dict()})
    return outcome


def run(
    root,
    session_id="",
    *,
    mode="run",
    base="",
    only_paths=None,
    reviewers=None,
    settings=None,
    emit=None,
    cancel=None,
    force=False,
    message_index=0,
    triggered_by="manual",
):
    """Run a review and return the report.

    ``emit(stage, payload)`` is called as the review progresses; the stages
    are the ones in STAGES. ``cancel`` is a threading.Event: setting it stops
    outstanding model calls as soon as they notice.
    """
    emit = emit or _noop
    settings = settings or config_mod.load()
    entries = list(reviewers) if reviewers is not None else config_mod.enabled_reviewers(settings)

    if not entries:
        emit(
            "failed",
            {
                "error": "no reviewer model is configured",
                "action": "settings",
                "message": (
                    "No reviewer model is configured. Choose one in Settings, "
                    "Review, to run a multi-model review."
                ),
            },
        )
        return {
            "review_id": "",
            "verdict": "failed",
            "verdict_label": "failed",
            "findings": [],
            "models": [],
            "by_severity": {},
            "by_category": {},
            "scope": {"mode": mode, "base": base, "files": [], "skipped": []},
            "error": "no reviewer model is configured",
            "action": "settings",
        }

    unusable = []
    usable = []
    for entry in entries:
        state = config_mod.model_state(entry.get("model_id") or "")
        if state == config_mod.OK:
            usable.append(entry)
        else:
            unusable.append(
                {**entry, "state": state, "reason": config_mod.reason(state)}
            )

    scope = scope_mod.detect(
        root,
        mode=mode,
        base=base,
        only_paths=only_paths,
        session_id=session_id,
        exclusions=tuple(settings.get("exclusions") or scope_mod.DEFAULT_EXCLUSIONS),
        max_files=int(settings.get("max_files") or config_mod.DEFAULT_MAX_FILES),
    )
    if scope.empty:
        emit(
            "nothing",
            {
                "message": "there is nothing to review - no files changed",
                "scope": {"mode": scope.mode, "base": scope.base},
            },
        )
        return {
            "review_id": "",
            "verdict": "nothing",
            "verdict_label": "nothing to review",
            "findings": [],
            "models": [],
            "by_severity": {},
            "by_category": {},
            "scope": {
                "mode": scope.mode,
                "base": scope.base,
                "files": [],
                "skipped": list(scope.skipped),
            },
        }

    if not usable:
        # Every reviewer the user picked cannot be reached. Say which one and
        # why, and point at the settings rather than failing quietly.
        names = ", ".join(
            f"{item.get('label') or item.get('model_id')} ({item['reason']})"
            for item in unusable
        )
        error = f"no reviewer model can be reached: {names}"
        emit(
            "failed",
            {
                "error": error,
                "action": "settings",
                "message": (
                    "None of the configured reviewers can be reached. Fix them in "
                    "Settings, Review."
                ),
            },
        )
        return {
            "review_id": "",
            "verdict": "failed",
            "verdict_label": "failed",
            "findings": [],
            "models": [
                ModelOutcome(
                    model_id=item.get("model_id") or "",
                    label=item.get("label") or "",
                    status="skipped",
                    error=item.get("reason", ""),
                ).to_dict()
                for item in unusable
            ],
            "by_severity": {},
            "by_category": {},
            "scope": {
                "mode": scope.mode,
                "base": scope.base,
                "files": [change.path for change in scope.files],
                "skipped": list(scope.skipped),
            },
            "error": error,
            "action": "settings",
        }

    key = report_mod.cache_key(scope, [e.get("model_id") or "" for e in entries])
    if not force:
        cached = report_mod.cached(session_id, key)
        if cached is not None:
            cached["cached"] = True
            emit("completed", {"report": cached, "cached": True})
            return cached

    redactions = _redact_scope(scope)
    max_file_bytes = int(settings.get("max_file_bytes") or config_mod.DEFAULT_MAX_FILE_BYTES)
    max_diff_bytes = int(settings.get("max_diff_bytes") or config_mod.DEFAULT_MAX_DIFF_BYTES)
    used = _fit_scope(scope, max_diff_bytes, max_file_bytes)
    contexts = _contexts(root, scope, bool(settings.get("full_context")), max_file_bytes)

    review_id = report_mod.new_id()
    estimate = config_mod.estimated_tokens(settings, used)
    emit(
        "started",
        {
            "review_id": review_id,
            "reviewers": [
                {"model_id": e.get("model_id"), "label": e.get("label") or _label_for(e.get("model_id") or "")}
                for e in usable
            ],
            "skipped": unusable,
            "files": [change.path for change in scope.files],
            "redacted": redactions,
            "estimate": estimate,
        },
    )

    outcomes = []
    mode_name = settings.get("mode") or "parallel"
    started = time.monotonic()

    if mode_name == "parallel" and len(usable) > 1:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(usable), 8), thread_name_prefix="review"
        ) as pool:
            futures = {
                pool.submit(
                    _run_reviewer, entry, scope, contexts, redactions, settings, cancel, emit, root
                ): entry
                for entry in usable
            }
            finished = {}
            for future in concurrent.futures.as_completed(futures):
                entry = futures[future]
                try:
                    finished[entry.get("model_id") or ""] = future.result()
                except Exception as failure:  # noqa: BLE001
                    finished[entry.get("model_id") or ""] = ModelOutcome(
                        model_id=entry.get("model_id") or "",
                        label=entry.get("label") or "",
                        status="failed",
                        error=f"{type(failure).__name__}: {failure}",
                    )
        # The reviewers report in the order the user set them, not in the
        # order they happened to finish.
        outcomes = [
            finished.get(entry.get("model_id") or "")
            for entry in usable
            if finished.get(entry.get("model_id") or "") is not None
        ]
    else:
        for entry in usable:
            if cancel is not None and cancel.is_set():
                outcomes.append(
                    ModelOutcome(
                        model_id=entry.get("model_id") or "",
                        label=entry.get("label") or "",
                        status="cancelled",
                        error="cancelled",
                    )
                )
                break
            outcomes.append(
                _run_reviewer(entry, scope, contexts, redactions, settings, cancel, emit, root)
            )

    for entry in unusable:
        outcomes.append(
            ModelOutcome(
                model_id=entry.get("model_id") or "",
                label=entry.get("label") or "",
                status="skipped",
                error=entry.get("reason", "this model cannot be reached"),
            )
        )

    ran = [o for o in outcomes if o.status == "ran"]
    if not ran:
        first = next((o for o in outcomes if o.status == "failed"), None)
        error = first.error if first else "every reviewer failed"
        emit(
            "failed",
            {
                "review_id": review_id,
                "error": error,
                "models": [o.to_dict() for o in outcomes],
            },
        )
        return {
            "review_id": review_id,
            "verdict": "failed",
            "verdict_label": "failed",
            "findings": [],
            "models": [o.to_dict() for o in outcomes],
            "by_severity": {},
            "by_category": {},
            "scope": {
                "mode": scope.mode,
                "base": scope.base,
                "files": [c.path for c in scope.files],
                "skipped": list(scope.skipped),
            },
            "error": error,
        }

    merged = merge(outcomes, threshold=settings.get("severity_threshold") or "low")
    total_tokens = sum(o.tokens for o in outcomes)
    limit = int(settings.get("max_tokens") or config_mod.DEFAULT_MAX_TOKENS)
    if total_tokens > limit:
        emit(
            "progress",
            {
                "state": "over-budget",
                "tokens": total_tokens,
                "limit": limit,
                "message": "the review went over its token limit",
            },
        )

    document = report_mod.build(
        review_id,
        scope,
        merged,
        redactions=redactions,
        seconds=time.monotonic() - started,
        message_index=message_index,
        cache_key=key,
        triggered_by=triggered_by,
    )
    report_mod.save(session_id, document)
    emit("completed", {"report": document, "cached": False})
    return document
