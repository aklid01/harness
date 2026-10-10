"""Project instructions: AGENTS.md, read into the system prompt.

The rules a repository keeps in AGENTS.md are instructions to whoever works
in it - an agent here, a human there - so they reach the model the same way
they reach a person: the workspace's own file first, then the global one for
everything else, capped so a runaway file cannot crowd the task out of the
context window.
"""

from . import paths

MAX_CHARS = 20000


def load() -> str:
    """The instructions to inject, or "" when there are none."""
    parts = []
    left = MAX_CHARS
    for path in paths.instruction_files():
        if left <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > left:
            text = f"{text[:left]}\n\n[... truncated: {path} is {len(text)} characters]"
            left = 0
        else:
            left -= len(text)
        parts.append(f"### Rules from {path}\n\n{text}")
    return "\n\n".join(parts)
