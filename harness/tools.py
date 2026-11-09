"""Local tools: registry, schemas, permission-checked execution, availability.

Everything a model can ask for lives in TOOLS with a JSON schema in
TOOL_SCHEMAS. Two things here are worth stating plainly:

* A tool is only advertised when it is actually usable in this session. The
  checks are cheap and non-destructive (registration, schema shape, whether an
  executable resolves on PATH, whether the approval channel is connected,
  whether the workspace is readable). A missing dependency costs that one
  tool, not all of them.
* What a model writes is untrusted input. The name may not exist, the
  arguments may not be JSON and they may not match the schema, so each of
  those comes back as a result the model can read and retry. None of them
  ends the session, and none of them is executed.
"""

import contextvars
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import history, paths, sandbox
from .permissions import approval_info, check
from .skills import read_skill
from .todos import TODO_SCHEMA, write_todos
from .toolsets import READONLY_WITHHELD, REQUIRES

_AVAILABILITY_CACHE = None
_AVAILABILITY_KEY = None


def approval_channel_ready() -> bool:
    """Is there a frontend that can answer an approval request?"""
    from . import events

    return events.has_interaction_channel()


def shell_available() -> bool:
    """A shell the sandbox could drive. Resolved, never run."""
    return bool(shutil.which("bash") or shutil.which("sh") or shutil.which("cmd"))


def workspace_readable() -> bool:
    root = paths.project_root()
    try:
        return root.is_dir() and os.access(root, os.R_OK)
    except OSError:
        return False


def skills_installed() -> bool:
    from .skills import find_skills

    return bool(find_skills())


def _reason_for(name):
    """(ok, reason) for one tool, from its declared requirements."""
    from . import permissions

    needs = REQUIRES.get(name, {})
    if needs.get("shell") and not shell_available():
        return False, "no shell was found on PATH"
    if needs.get("workspace") and not workspace_readable():
        return False, f"the workspace ({paths.project_root()}) is not readable"
    if needs.get("skills") and not skills_installed():
        return False, "no skills are installed"
    if (
        needs.get("approval")
        and not approval_channel_ready()
        and permissions.current_mode() not in ("allow-all", "autopilot")
    ):
        return False, "an approval prompt cannot be shown in this session"
    return True, ""


def _cache_key():
    from . import permissions

    return (
        str(paths.project_root()),
        approval_channel_ready(),
        shell_available(),
        skills_installed(),
        permissions.current_mode(),
    )


def availability(refresh=False) -> dict:
    """{tool name: {"available": bool, "reason": str}} for this session.

    Cached, and recomputed when the workspace, the approval channel, the
    frontend or the available dependencies change - so switching workspace or
    settings never serves a stale answer.
    """
    global _AVAILABILITY_CACHE, _AVAILABILITY_KEY

    key = _cache_key()
    if refresh or _AVAILABILITY_CACHE is None or _AVAILABILITY_KEY != key:
        report = {}
        for schema in TOOL_SCHEMAS:
            name = schema["function"]["name"]
            ok, reason = _reason_for(name)
            report[name] = {"available": ok, "reason": reason}
        _AVAILABILITY_CACHE = report
        _AVAILABILITY_KEY = key
    return _AVAILABILITY_CACHE


def available_tool_names() -> list[str]:
    return [name for name, state in availability().items() if state["available"]]


def tool_schemas() -> list:
    """Schemas for the tools this session can actually run."""
    return [
        schema
        for schema in TOOL_SCHEMAS
        if availability().get(schema["function"]["name"], {}).get("available")
    ]


def unavailable_summary() -> str:
    """What the model should be told when it cannot be given any tools."""
    missing = [
        f"{name}: {state['reason']}"
        for name, state in availability().items()
        if not state["available"]
    ]
    return "; ".join(missing) if missing else ""


def _resolve(path):
    """Resolve a model-provided path against the active workspace root."""
    p = Path(path)
    if p.is_absolute():
        return p
    return paths.project_root() / p


def resolve_path(path) -> str:
    """The resolved path of a model-provided argument, as execute() would set it."""
    return str(_resolve(path))


def bash(command: str) -> str:
    """Run a shell command and return its combined stdout and stderr."""
    try:
        result = sandbox.run(command)
    except subprocess.TimeoutExpired as expired:
        return (
            f"Timed out after {expired.timeout}s and was killed. "
            "Narrow it down - search inside the working directory rather than /."
        )
    return history.cap((result.stdout + result.stderr) or "(no output)")


def read_file(path: str) -> str:
    """Read a file and return its contents.

    A PDF or an Office document is extracted to text first, so the agent gets
    something it can read instead of mojibake. A file that has no text to give
    says so plainly and keeps its path, so the agent can report the reason
    rather than guess.
    """
    from . import attachments

    text, reason = attachments.readable_text(path)
    if text is None:
        return f"Error: {reason}"
    return history.cap(text)


def write_file(path: str, content: str) -> str:
    """Create a file, or overwrite it if it already exists.

    Written with LF endings exactly as given: text-mode translation would
    turn a script the model just wrote into a CRLF file the shell then
    refuses to run (`$'\\r': command not found`).
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"Wrote {path}"


def str_replace(path, old_str, new_str, allow_multi_edit=False):
    """Swap exact text in a file. old_str must match exactly once.

    Byte-preserving: the file must decode as UTF-8, and only the matched span
    changes. A lossy text-mode round trip would persist U+FFFD for every byte
    it could not decode and translate every line ending - one small edit in a
    cp1252 or LF-only file rewrote the whole file.
    """
    raw = Path(path).read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (
            f"Error: {path} is not valid UTF-8, so a text edit would "
            "corrupt it. Edit it with bash instead."
        )

    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str was not found in {path}"
    if count > 1 and not allow_multi_edit:
        return (
            f"Error: old_str matches {count} times in {path}. "
            "Add surrounding lines to make it unique, "
            "or set allow_multi_edit to replace them all."
        )

    Path(path).write_bytes(content.replace(old_str, new_str).encode("utf-8"))
    return f"Replaced {count} match(es) in {path}"


def _tracked_write(name, args):
    """Write a file and journal before/after so rewind can restore it."""
    from . import checkpoints, session

    path = args["path"]
    before = checkpoints.read_or_none(path)
    result = TOOLS[name](**args)
    if not isinstance(result, str) or result.startswith("Error"):
        return result
    after = checkpoints.read_or_none(path)
    checkpoints.record(
        session.current_id(), session.written() or 0, path, before, after
    )
    return result


def _validate_args(name, args):
    """Schema-check a native call's arguments before anything runs.

    Returns None when they are fine, or a reason string the model can act on.
    Mirrors the text-call layer, so both paths give the same kind of feedback:
    missing required arguments, arguments the tool does not accept, and wrong
    types are refused before the permission layer or the handler runs.
    """
    schema = None
    for candidate in TOOL_SCHEMAS:
        if candidate["function"]["name"] == name:
            schema = candidate["function"].get("parameters") or {}
            break
    if schema is None:
        return None
    properties = schema.get("properties") or {}

    missing = [
        key
        for key in (schema.get("required") or [])
        if key not in args or args[key] is None
    ]
    if missing:
        return f"{name} is missing required argument(s): {', '.join(missing)}."
    unknown = [key for key in args if key not in properties]
    if unknown:
        return f"{name} got argument(s) it does not accept: {', '.join(unknown)}."
    for key, value in args.items():
        kind = (properties.get(key) or {}).get("type")
        expected = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
        }.get(kind)
        if not expected or isinstance(value, expected):
            if kind in ("integer", "number") and isinstance(value, bool):
                return f"{name} argument '{key}' should be {kind}."
            continue
        return f"{name} argument '{key}' should be {kind}."
    return None


_CANCEL = contextvars.ContextVar("tools_cancel_event", default=None)


def execute(tool_call, note="", call_key=None, cancel=None) -> tuple[dict, str]:
    """Run one tool call through the permission layer.

    Shared by the main loop and by subagents, so a subagent is fenced in by
    exactly the same rules - it is not a way around them.

    A tool call is text the model wrote, so all of it is untrusted: the name
    may not exist, the arguments may not be JSON, and they may not match the
    signature. Every one of those comes back as a result the model can read
    and retry. None of them is allowed to end the session, and none of them
    is executed.

    ``note`` is the model's narration just before this call; it is surfaced in
    the approval dialog as the "why" alongside a validated risk level.

    ``call_key`` identifies one call inside one request. When it is given, a
    repeat (a retried frame, a re-entered stream, a duplicated event) returns
    the recorded result instead of running the tool a second time.
    """
    _CANCEL.set(cancel)

    from . import events

    executed = _executed()
    if call_key is not None and call_key in executed:
        return executed[call_key]

    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as broken:
        return {}, f"Error: arguments were not valid JSON ({broken})."

    if name not in TOOLS:
        return args, f"Error: no tool named '{name}'. Available: {', '.join(TOOLS)}."

    problem = _validate_args(name, args)
    if problem is not None:
        return args, f"Error: {problem}"

    if name in ("write_file", "str_replace", "read_file"):
        args["path"] = str(_resolve(args["path"]))

    try:
        action, reason = check(name, args)
        if action == "deny":
            return args, f"Blocked by policy: {reason}"
        if action == "ask":
            info = approval_info(name, args, reason, note)
            if not events.interact(events.APPROVAL, info):
                return args, "The user denied this tool call."
            approved = _approved()
            approved.append((name, dict(args)))
            while len(approved) > _USER_APPROVED_LIMIT:
                approved.pop(0)
        if name in ("write_file", "str_replace"):
            outcome = args, _tracked_write(name, args)
        else:
            outcome = args, TOOLS[name](**args)
    except TypeError as mismatch:
        return args, f"Error: wrong arguments for {name} ({mismatch})."
    except KeyError as missing:
        return args, f"Error: {name} needs an argument you did not send: {missing}."
    except Exception as failure:  # noqa: BLE001
        return args, f"Error: {name} failed - {type(failure).__name__}: {failure}"

    if call_key is not None:
        _remember(call_key, outcome)
    return outcome


_EXECUTED = contextvars.ContextVar("tools_executed", default=None)
_EXECUTED_LIMIT = 64
_USER_APPROVED = contextvars.ContextVar("tools_user_approved", default=None)
_USER_APPROVED_LIMIT = 32


def begin_turn():
    """Fresh registries for one turn.

    A server turn runs in its own thread and would start empty anyway; this
    also resets one shared thread that runs several turns in sequence. A
    context that never calls it gets lazily created registries on first use.
    """
    _EXECUTED.set({})
    _USER_APPROVED.set([])


def _executed() -> dict:
    seen = _EXECUTED.get()
    if seen is None:
        seen = {}
        _EXECUTED.set(seen)
    return seen


def _approved() -> list:
    approved = _USER_APPROVED.get()
    if approved is None:
        approved = []
        _USER_APPROVED.set(approved)
    return approved


def user_approved(name, args) -> bool:
    """Was this exact call approved by the user through the permission dialog?"""
    return any(n == name and a == args for n, a in _approved())


def _remember(call_key, outcome):
    seen = _executed()
    seen[call_key] = outcome
    while len(seen) > _EXECUTED_LIMIT:
        seen.pop(next(iter(seen)))


def executed_calls() -> dict:
    """The registry, for tests and diagnostics."""
    return dict(_executed())


def current_cancel():
    """The cancel event of the agent loop that dispatched this call."""
    return _CANCEL.get()


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return its combined stdout and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Open a skill by name and return its full instructions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the skill to open",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a file, or overwrite it if it already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to write"},
                    "content": {"type": "string", "description": "The full contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace",
            "description": (
                "Replace exact text in a file. old_str must appear exactly once, "
                "so include surrounding lines if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit"},
                    "old_str": {"type": "string", "description": "Exact text to find"},
                    "new_str": {
                        "type": "string",
                        "description": "Text to put in its place",
                    },
                    "allow_multi_edit": {
                        "type": "boolean",
                        "description": "Replace every match instead of failing",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    TODO_SCHEMA,
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": (
                "Hand a self-contained exploration question to a fresh agent that "
                "has its own context window, and get back its findings. Use this "
                "to learn how the codebase works - tracing behaviour, locating "
                "where something is implemented, surveying files - so the search "
                "costs you one answer instead of dozens of tool results. It cannot "
                "see this conversation, so include every detail it needs. It reads "
                "and reports; it never edits. Do your own editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "The question, written to stand alone: what to find "
                            "out, where to start looking, and what the answer "
                            "should contain."
                        ),
                    }
                },
                "required": ["description"],
            },
        },
    },
]


def _task(description: str) -> str:
    """Deferred dispatch: the guest loop gets the registry handed to it,
    so an implementation never reads the registry back."""
    from . import subagent

    guest = [s for s in TOOL_SCHEMAS if s["function"]["name"] not in READONLY_WITHHELD]
    return subagent.task(
        description,
        execute=execute,
        tool_schemas=guest,
        cancel=current_cancel(),
    )


TOOLS = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "str_replace": str_replace,
    "read_skill": read_skill,
    "write_todos": write_todos,
    "task": _task,
}
