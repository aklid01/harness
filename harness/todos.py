"""The plan. Lives here, not in the transcript, and is re-injected every turn."""

MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}

TODOS = []  # [{"content": ..., "activeForm": ..., "status": ...}]


def write_todos(todos):
    """Replace the whole list. Exactly one task may be in_progress."""
    active = [t for t in todos if t["status"] == "in_progress"]
    if len(active) > 1:
        return f"Error: {len(active)} tasks are in_progress. Only one may be."

    TODOS[:] = todos
    return todos_prompt() or "Todo list cleared."


def todos_prompt():
    return "\n".join(f"{MARKS[t['status']]} {t['content']}" for t in TODOS)


def active_form():
    """What the agent is doing right now, for the spinner."""
    for todo in TODOS:
        if todo["status"] == "in_progress":
            return todo["activeForm"]
    return "thinking"


# Words that, in a todo, mean "this is a verification/validation step". A bash
# command that actually runs one marks the step done - from evidence, not from
# the agent claiming it.
_COMPLETION_WORDS = ("py_compile", "py_test", "pytest", "unittest", "test",
                    "tests", "lint", "flake8", "ruff", "mypy", "typecheck",
                    "compile", "build", "validate", "check")
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
    if head.startswith(("error", "blocked", "plan mode", "the user denied",
                        "decision review", "timed out", "<system-reminder", "stopped")):
        return False
    changed = False
    for todo in TODOS:
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
