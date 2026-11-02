"""Subagents: exploration that happens somewhere else.

A subagent is a whole agent loop with its own message list. That list is never
shown to the main agent and never outlives the call - the only thing that
crosses back is the final report.

That is the entire point, and it is compact.py's idea from the other end.
Exploring a repo burns tens of thousands of tokens of tool output to produce a
few hundred tokens of answer. compact.py throws context away after it has been
spent; a subagent spends it somewhere that gets thrown away by design, so the
main transcript never pays for the difference at all.

Four rules, and the code below is really just these:

  1. it starts from an empty history          (no memory of anything)
  2. it holds every tool but two              (no recursion, no touching the plan)
  3. it runs the same loop as the main agent  (nothing special happens here)
  4. only its last message comes back         (the rest is discarded)
"""

import os

MAX_TURNS = 12  # a runaway explorer is worse than a missing answer


# --- rule 2 -----------------------------------------------------------------
# `task` would let a subagent spawn subagents, forever. `write_todos` writes to
# a module global that belongs to the main agent's plan, and this one is a
# guest in someone else's session. The file-writing tools are withheld too, so
# the read-only rule below is structural and not just asking nicely.
WITHHELD = {"task", "write_todos", "str_replace", "write_file"}


def _workspace_root() -> str:
    from . import paths
    try:
        if paths.is_initialized():
            return str(paths.project_root())
    except Exception:  # noqa: BLE001 - a label, never worth crashing over
        pass
    return os.getcwd()


def _system_prompt() -> str:
    return f"""
You are an exploration subagent. You were given one question by a lead agent
and you answer it. That is the whole job.

You cannot see the conversation that spawned you, and the lead agent cannot
see anything you do here. Only your final message crosses back, so it has to
stand on its own.

You are working in {_workspace_root()}. Search inside it. Never search from /
or from the home directory - that scans the whole machine and will time out.

How to work:
- Use bash, read_file and read_skill to find out what is actually true.
  Prefer rg, grep and find to guess at where things live.
- You are here to read and report, not to change anything. Do not write or
  edit files, and do not run commands with side effects.
- Search in batches. Several greps in one turn beats one grep per turn.
- Stop as soon as you can answer. Do not keep looking to be thorough.

Your final message is the entire report, and it is the only thing that costs
the lead agent anything - so keep it short. Aim for under 150 words. Findings
only: file paths with line numbers, names, values. No preamble, no restating
the question, no long code blocks - cite the path and line and let the lead
agent open it. Say plainly what you could not find; a gap is useful, a guess
is not.
"""


def toolset():
    """Every tool except the ones a guest should not hold."""
    from .tools import TOOL_SCHEMAS

    return [s for s in TOOL_SCHEMAS if s["function"]["name"] not in WITHHELD]


def task(description: str) -> str:
    """Run a fresh agent on one question and return only its final answer."""
    # Imported inside the function, not at the top: llm imports tools, and
    # tools imports us, so importing them up there would close the circle.
    from .history import prepare
    from .llm import call_llm
    from .tools import execute
    from . import events
    from . import providers

    # --- rule 1 ---
    # Two messages. Not a copy of the caller's transcript, not a trimmed
    # version of it, and not whatever the last subagent left behind - this
    # list is born here and dies at the return statement.
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": description},
    ]
    events.emit(events.SUBAGENT, {"description": description})

    report = None  # newest thing it has said, kept in case we run out of turns

    # --- rule 3 ---
    # Compare this with the inner loop in agent.py: call, append, run the
    # tools, append, repeat. A subagent is not a new kind of thing. It is the
    # loop you already have, pointed at a different list of messages.
    for _ in range(MAX_TURNS):
        # Its context can overflow too, and nobody compacts it. Prepared as a
        # copy: the throwaway list is the only thing that ever gets trimmed.
        request, trimmed = prepare(messages, providers.active_context_window())
        if trimmed:
            events.emit(events.NOTE, {"text": trimmed})

        events.emit(events.STATUS, {"active": "subagent exploring"})
        try:
            message, usage = call_llm(request, tools=toolset())
        except Exception as failure:  # noqa: BLE001
            events.emit(events.STATUS, {"active": None})
            events.emit(events.ERROR, {"text": f"subagent failed - {type(failure).__name__}: {failure}", "nested": True})
            return report or "(the subagent hit an error before reporting.)"
        events.emit(events.STATUS, {"active": None})

        messages.append(message.model_dump(exclude_none=True))
        events.emit(events.USAGE, dict(usage))
        report = message.content or report

        # --- rule 4 ---
        # No tool calls means it has stopped looking and started answering.
        # `messages` goes out of scope on this line - every tool result it
        # gathered, every path it explored, all of it. One string comes back.
        if not message.tool_calls:
            return report or "(the subagent came back with nothing)"

        for tool_call in message.tool_calls:
            # The same executor the main loop uses, so the same permission
            # rules and the same sandbox apply. A subagent is a second caller,
            # not a privileged one - it is not a way around any of that.
            args, result = execute(tool_call)
            call_id = getattr(tool_call, "id", None)
            events.emit(events.TOOL_CALL, {"call_id": call_id, "name": tool_call.function.name, "args": args, "nested": True})
            events.emit(events.TOOL_RESULT, {"call_id": call_id, "name": tool_call.function.name, "args": args, "result": result, "nested": True})
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # Out of turns. Hand back whatever it last managed to say rather than
    # nothing at all - a partial finding still beats making the lead agent
    # start the whole search again from scratch.
    if report:
        return (
            f"(stopped after {MAX_TURNS} turns, before finishing. Partial "
            f"findings below - narrow the question and ask again.)\n\n{report}"
        )
    return f"(stopped after {MAX_TURNS} turns with nothing to report.)"


TASK_SCHEMA = {
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
}
