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

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import history
from . import paths
from . import sandbox
from .permissions import approval_info, check
from .subagent import TASK_SCHEMA, task
from .skills import read_skill
from .todos import TODO_SCHEMA, write_todos

# What each tool needs before it can be offered at all. The question is "can
# this run", not "will it succeed": a command that fails is a result the model
# can react to, not a reason to hide the tool from it.
REQUIRES = {
    "bash": {"shell": True, "workspace": True, "approval": True},
    "read_file": {"workspace": True},
    "write_file": {"workspace": True, "approval": True},
    "str_replace": {"workspace": True, "approval": True},
    "read_skill": {"skills": True},
    "write_todos": {},
    "task": {"workspace": True},
}

_AVAILABILITY_CACHE = None
_AVAILABILITY_KEY = None


def approval_channel_ready():
    """Is there a frontend that can answer an approval request?"""
    from . import events

    if events._ACTIVE.get() is not None:
        return True
    return events._responder is not None


def shell_available():
    """A shell the sandbox could drive. Resolved, never run."""
    return bool(shutil.which("bash") or shutil.which("sh") or shutil.which("cmd"))


def workspace_readable():
    root = paths.project_root()
    try:
        return root.is_dir() and os.access(root, os.R_OK)
    except OSError:
        return False


def skills_installed():
    from .skills import SKILLS

    return bool(SKILLS)


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
    if needs.get("approval") and not approval_channel_ready() and permissions.current_mode() not in ("allow-all", "autopilot"):
        return False, "an approval prompt cannot be shown in this session"
    return True, ""


def _cache_key():
    from . import permissions

    return (str(paths.project_root()), approval_channel_ready(), shell_available(),
            skills_installed(), permissions.current_mode())


def availability(refresh=False):
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


def available_tool_names():
    return [name for name, state in availability().items() if state["available"]]


def tool_schemas():
    """Schemas for the tools this session can actually run."""
    return [schema for schema in TOOL_SCHEMAS
            if availability().get(schema["function"]["name"], {}).get("available")]


def unavailable_summary():
    """What the model should be told when it cannot be given any tools."""
    missing = [f"{name}: {state['reason']}" for name, state in availability().items()
               if not state["available"]]
    return "; ".join(missing) if missing else ""


def _resolve(path):
    """Resolve a model-provided path against the active workspace root."""
    p = Path(path)
    if p.is_absolute():
        return p
    return paths.project_root() / p


def bash(command: str) -> str:
    """Run a shell command and return its combined stdout and stderr."""
    try:
        result = sandbox.run(command)
    except subprocess.TimeoutExpired as expired:
        # Hand the failure back as a result. A slow command is the model's
        # problem to work around, not a reason to take the session down.
        return (
            f"Timed out after {expired.timeout}s and was killed. "
            "Narrow it down - search inside the working directory rather than /."
        )
    return history.cap((result.stdout + result.stderr) or "(no output)")


def read_file(path: str) -> str:
    """Read a file and return its contents."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return history.cap(f.read())


def write_file(path: str, content: str) -> str:
    """Create a file, or overwrite it if it already exists."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {path}"


def str_replace(path, old_str, new_str, allow_multi_edit=False):
    """Swap exact text in a file. old_str must match exactly once."""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str was not found in {path}"
    if count > 1 and not allow_multi_edit:
        return (
            f"Error: old_str matches {count} times in {path}. "
            "Add surrounding lines to make it unique, "
            "or set allow_multi_edit to replace them all."
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write(content.replace(old_str, new_str))
    return f"Replaced {count} match(es) in {path}"


def _tracked_write(name, args):
    """Write a file and journal before/after so rewind can restore it."""
    from . import checkpoints
    from . import session

    path = args["path"]
    before = checkpoints.read_or_none(path)
    result = TOOLS[name](**args)
    if not isinstance(result, str) or result.startswith("Error"):
        return result
    after = checkpoints.read_or_none(path)
    checkpoints.record(session.current_id(), session.written() or 0, path, before, after)
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
    missing = [key for key in (schema.get("required") or [])
               if key not in args or args[key] in (None, "")]
    if missing:
        return (f"{name} is missing required argument(s): {', '.join(missing)}.")
    unknown = [key for key in args if key not in properties]
    if unknown:
        return f"{name} got argument(s) it does not accept: {', '.join(unknown)}."
    for key, value in args.items():
        kind = (properties.get(key) or {}).get("type")
        expected = {"string": str, "integer": int, "number": (int, float),
                    "boolean": bool}.get(kind)
        if not expected or isinstance(value, expected):
            # bool is an int subclass: a boolean where a number is wanted is
            # a wrong type, the other way round is fine.
            if kind in ("integer", "number") and isinstance(value, bool):
                return f"{name} argument '{key}' should be {kind}."
            continue
        return f"{name} argument '{key}' should be {kind}."
    return None


def execute(tool_call, note="", call_key=None):
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
    from . import events

    if call_key is not None and call_key in _EXECUTED:
        return _EXECUTED[call_key]

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
        if name in ("write_file", "str_replace"):
            outcome = args, _tracked_write(name, args)
        else:
            outcome = args, TOOLS[name](**args)
    except TypeError as mismatch:
        return args, f"Error: wrong arguments for {name} ({mismatch})."
    except KeyError as missing:
        return args, f"Error: {name} needs an argument you did not send: {missing}."
    except Exception as failure:  # noqa: BLE001 - the model gets to see and retry
        return args, f"Error: {name} failed - {type(failure).__name__}: {failure}"

    if call_key is not None:
        _remember(call_key, outcome)
    return outcome


# One entry per executed call, keyed by (request, call id). Bounded: only the
# most recent calls are remembered, which is the window a retry can land in.
_EXECUTED: dict = {}
_EXECUTED_LIMIT = 64


def _remember(call_key, outcome):
    _EXECUTED[call_key] = outcome
    while len(_EXECUTED) > _EXECUTED_LIMIT:
        _EXECUTED.pop(next(iter(_EXECUTED)))


def executed_calls():
    """The registry, for tests and diagnostics."""
    return dict(_EXECUTED)


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
                    "new_str": {"type": "string", "description": "Text to put in its place"},
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
    TASK_SCHEMA,
]

TOOLS = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "str_replace": str_replace,
    "read_skill": read_skill,
    "write_todos": write_todos,
    "task": task,
}
