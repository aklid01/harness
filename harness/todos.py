"""The write_todos checklist: the turn's task list, not the plan file.

harness/plan.py owns "the plan" - the durable plan artifact a planning turn
writes. This module owns the ephemeral checklist the write_todos tool
maintains: it lives here, not in the transcript, and is re-injected every
turn.

State is scoped per session: one turn thread inherits the ambient list when it
binds and diverges from there, so two concurrent sessions never see or rewrite
each other's checklist. Code outside any session (tests, engine-less use)
shares the module fallback.
"""

import contextlib
import contextvars

MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}

TODOS: list = []

_CURRENT = contextvars.ContextVar("harness_todos", default=None)


def _state():
    ref = _CURRENT.get()
    return ref if ref is not None else TODOS


@contextlib.contextmanager
def scoped(initial=None):
    """Isolate the plan for one session/turn; inherits ``initial`` by copy."""
    token = _CURRENT.set(list(initial if initial is not None else _state()))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current():
    """The plan list for the running session."""
    return _state()


def write_todos(todos):
    """Replace the whole list. Exactly one task may be in_progress."""
    active = [t for t in todos if t["status"] == "in_progress"]
    if len(active) > 1:
        return f"Error: {len(active)} tasks are in_progress. Only one may be."

    _state()[:] = todos
    return todos_prompt() or "Todo list cleared."


def todos_prompt():
    return "\n".join(f"{MARKS[t['status']]} {t['content']}" for t in _state())


def active_form():
    """What the agent is doing right now, for the spinner."""
    for todo in _state():
        if todo["status"] == "in_progress":
            return todo["activeForm"]
    return "thinking"


_COMPLETION_WORDS = (
    "py_compile",
    "py_test",
    "pytest",
    "unittest",
    "test",
    "tests",
    "lint",
    "flake8",
    "ruff",
    "mypy",
    "typecheck",
    "compile",
    "build",
    "validate",
    "check",
)
_DONE = {"done", "completed"}


def reconcile(tool_name, args, result):
    """Mark open steps whose evidence just landed, from real tool output.

    Returns True if any step changed. Conservative: it only completes a step on
    a concrete match - the exact file a step names was just written, or the
    verification a step asks for was just run successfully. A failed or blocked
    tool never completes anything, and the agent keeps control of the rest.
    """
    if not isinstance(result, str) or not result.strip():
        return False
    head = result.lstrip().lower()
    if head.startswith(
        (
            "error",
            "blocked",
            "plan mode",
            "the user denied",
            "decision review",
            "timed out",
            "<system-reminder",
            "stopped",
        )
    ):
        return False
    changed = False
    for todo in _state():
        if str(todo.get("status")) in _DONE:
            continue
        text = str(todo.get("content") or "").lower()
        if not text:
            continue
        matched = False
        if tool_name in ("write_file", "str_replace"):
            path = str(args.get("path") or "").replace("\\", "/")
            base = path.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            for name in (base, stem):
                if len(name) >= 3 and name.lower() in text:
                    matched = True
        elif tool_name == "bash":
            command = str(args.get("command") or "").lower()
            for word in _COMPLETION_WORDS:
                if word in text and word in command:
                    matched = True
                    break
        if matched:
            todo["status"] = "done"
            changed = True
    return changed


TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_todos",
        "description": (
            "Record the plan for a multi-step task. Send the whole list every "
            "time. Keep exactly one task in_progress and update it as you go."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "The task, imperative: 'Fix the parser'",
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Present continuous: 'Fixing the parser'",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done"],
                            },
                        },
                        "required": ["content", "activeForm", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
}
