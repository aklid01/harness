import json
import subprocess
from pathlib import Path

from . import history
from . import paths
from . import sandbox
from .permissions import approval_info, check
from .subagent import TASK_SCHEMA, task
from .skills import read_skill
from .todos import TODO_SCHEMA, write_todos


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


def execute(tool_call, note=""):
    """Run one tool call through the permission layer.

    Shared by the main loop and by subagents, so a subagent is fenced in by
    exactly the same rules - it is not a way around them.

    A tool call is text the model wrote, so all of it is untrusted: the name
    may not exist, the arguments may not be JSON, and they may not match the
    signature. Every one of those comes back as a result the model can read
    and retry. None of them is allowed to end the session.

    ``note`` is the model's narration just before this call; it is surfaced in
    the approval dialog as the "why" alongside a validated risk level.
    """
    from . import events

    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as broken:
        return {}, f"Error: arguments were not valid JSON ({broken})."

    if name not in TOOLS:
        return args, f"Error: no tool named '{name}'. Available: {', '.join(TOOLS)}."

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
            return args, _tracked_write(name, args)
        return args, TOOLS[name](**args)
    except TypeError as mismatch:
        return args, f"Error: wrong arguments for {name} ({mismatch})."
    except KeyError as missing:
        return args, f"Error: {name} needs an argument you did not send: {missing}."
    except Exception as failure:  # noqa: BLE001 - the model gets to see and retry
        return args, f"Error: {name} failed - {type(failure).__name__}: {failure}"


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
