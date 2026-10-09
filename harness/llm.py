import os
from types import SimpleNamespace

from openai import OpenAI

from . import providers
from .skills import skills_prompt
from .tools import TOOL_SCHEMAS

_client_cache = {}


def get_client():
    """Client for the active provider. Rebuilt when credentials change."""
    base_url, api_key, _, _ = providers.active_config()
    key = (base_url, api_key)
    if key not in _client_cache:
        _client_cache[key] = OpenAI(base_url=base_url, api_key=api_key)
    return _client_cache[key]


def active_model():
    return providers.active_config()[2]

SYSTEM_PROMPT = """
You are a coding agent. Your job is to code. Always code.
Use the bash tool to inspect files.
Use write_file to create files and str_replace to edit them.
Answer back to the user once exploration is done.

For any task that takes more than one step, call write_todos first and plan it
out. Send the whole list every time you call it - it replaces the old one.
Keep exactly one task in_progress, mark it done the moment it is finished, and
move the next one to in_progress in the same call. Do not batch up completions
at the end. Skip the tool entirely for single-step tasks; it is noise there.

The current list is injected back to you every turn inside <todos> tags, so
that block - not the transcript - is the truth about where you are.

When you need to understand how something works - where a feature lives, how
data flows, what calls what - send a task subagent instead of grepping your
way there yourself. It explores in its own context window and hands you back
just the findings, so the search does not fill yours. It cannot see this
conversation, so write the question so it stands alone. Do all editing
yourself; the subagent only reads.

Long tool output is cut short, and the whole thing is written to a temp file
whose path is given at the cut. Page through it with head, tail, sed -n or
grep rather than asking for it again. That file only exists for the current
turn, so read it now or re-run the command later.

Your current working directory is: {os.getcwd()}

You have skills available. Each one is a set of instructions for a task.
If a skill matches what the user wants, call read_skill first and follow it.

{skills_prompt()}
"""


PLAN_SYSTEM_PROMPT = """
You are a planning assistant. Your job is to explore, explain, and propose
concrete changes. You are read-only: never call write_file or str_replace.
Use the bash tool to inspect files (read-only commands only).
Use read_file to read files and task to delegate exploration to a subagent.
Answer back to the user once exploration is done - with a plan, not edits.

For any task that takes more than one step, call write_todos first and plan it
out. Send the whole list every time you call it - it replaces the old one.
Keep exactly one task in_progress, mark it done the moment it is finished, and
move the next one to in_progress in the same call. Do not batch up completions
at the end. Skip the tool entirely for single-step tasks; it is noise there.

The current list is injected back to you every turn inside <todos> tags, so
that block - not the transcript - is the truth about where you are.

When you need to understand how something works - where a feature lives, how
data flows, what calls what - send a task subagent instead of grepping your
way there yourself. It explores in its own context window and hands you back
just the findings, so the search does not fill yours. It cannot see this
conversation, so write the question so it stands alone.

Long tool output is cut short, and the whole thing is written to a temp file
whose path is given at the cut. Page through it with head, tail, sed -n or
grep rather than asking for it again. That file only exists for the current
turn, so read it now or re-run the command later.

Your current working directory is: {os.getcwd()}

You are in plan mode. If asked which mode you are in, say plan mode.

You have skills available. Each one is a set of instructions for a task.
If a skill matches what the user wants, call read_skill first and follow it.

{skills_prompt()}
"""


def system_prompt(mode="build"):
    from . import paths

    cwd = str(paths.project_root())
    skills = skills_prompt()
    template = PLAN_SYSTEM_PROMPT if mode == "plan" else SYSTEM_PROMPT
    return template.replace("{cwd}", cwd).replace("{skills}", skills)


def _usage_from(response_usage):
    if response_usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": None, "cached_tokens": None}
    completion_details = response_usage.completion_tokens_details
    prompt_details = response_usage.prompt_tokens_details
    return {
        "prompt_tokens": response_usage.prompt_tokens,
        "completion_tokens": response_usage.completion_tokens,
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
        "cached_tokens": getattr(prompt_details, "cached_tokens", None),
    }


def call_llm(messages, tools=None, on_delta=None):
    """One chat call. With on_delta, streams content tokens as they arrive.

    The streamed path assembles the same message shape as the blocking path,
    so the turn logic cannot tell the difference. Tool calls still arrive as
    complete calls - only text streams.
    """
    client = get_client()
    params = {"model": active_model(), "messages": messages, "tools": tools or TOOL_SCHEMAS}
    params.update(providers.active_model_params())
    if on_delta is None:
        response = client.chat.completions.create(**params)
        return response.choices[0].message, _usage_from(response.usage)

    content, calls, usage = [], {}, None
    stream = client.chat.completions.create(
        **params,
        stream=True,
        stream_options={"include_usage": True},
    )
    for chunk in stream:
        if chunk.usage is not None:
            usage = chunk.usage
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue
        if delta.content:
            content.append(delta.content)
            on_delta(delta.content)
        for call in delta.tool_calls or []:
            slot = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
            slot["id"] += call.id or ""
            if call.function:
                slot["name"] += call.function.name or ""
                slot["arguments"] += call.function.arguments or ""

    tool_calls = [
        SimpleNamespace(id=slot["id"], function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]))
        for slot in calls.values()
    ]
    text = "".join(content)
    message = SimpleNamespace(content=text or None, tool_calls=tool_calls)
    message.model_dump = lambda exclude_none=True: {
        "role": "assistant",
        **({"content": text} if text else {}),
        **({"tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls]} if tool_calls else {}),
    }
    return message, _usage_from(usage)
