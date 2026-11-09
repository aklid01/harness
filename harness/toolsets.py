"""The tool-name groups the rest of the engine derives from.

A leaf module on purpose: permissions may not import tools (tools imports
permissions), so the static taxonomy of tool names lives here where the
policy layer, the registry, the agent loops and oversight all read the same
sets. A new tool lands in REQUIRES and every group below follows from it.
"""

REQUIRES = {
    "bash": {"shell": True, "workspace": True, "approval": True},
    "read_file": {"workspace": True},
    "write_file": {"workspace": True, "approval": True},
    "str_replace": {"workspace": True, "approval": True},
    "read_skill": {"skills": True},
    "write_todos": {},
    "task": {"workspace": True},
}

_EDIT_TOOLS = frozenset({"write_file", "str_replace"})
_SESSION_TOOLS = frozenset({"write_todos", "task"})
_APPROVAL_TOOLS = frozenset(
    name for name, requires in REQUIRES.items() if requires.get("approval")
)

READONLY_WITHHELD = _EDIT_TOOLS | _SESSION_TOOLS
REVIEWABLE = _EDIT_TOOLS | frozenset({"bash"})
SAFE_TOOLS = frozenset(REQUIRES) - _APPROVAL_TOOLS
