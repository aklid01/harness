"""The model call. One place that knows how to talk to a provider.

Every request goes through retry.run(), so a dropped connection or an
upstream 5xx is retried inside one bounded window instead of surfacing on the
first hiccup - while a bad key or an unknown model id fails straight away with
an explanation. The client is built with the SDK's own retries switched off:
this module owns that policy now.
"""

from types import SimpleNamespace

from . import events
from . import instructions
from . import providers
from . import retry as retry_mod
from . import errors as errors_mod
from .errors import Cancelled, stream_interrupted
from .skills import skills_prompt
from .tools import TOOL_SCHEMAS

_client_cache = {}

RETRY = "retry"


def get_client():
    """Client for the active provider. Rebuilt when credentials change."""
    base_url, api_key, _, _ = providers.active_config()
    key = (base_url, api_key)
    client = _client_cache.get(key)
    if client is None:
        if not api_key:
            caps = providers.active_model_capabilities()
            raise errors_mod.no_key(model=caps.get("name") or caps.get("model"),
                                    provider=caps.get("provider_name"))
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key=api_key,
                       timeout=retry_mod.timeouts(), max_retries=0)
        _client_cache[key] = client
    return client


def active_model():
    return providers.active_config()[2]


def _params(messages, tools, caps):
    """Request body for the active model - only what it actually supports."""
    from . import tools as tools_mod

    params = {"model": caps["model"], "messages": messages}
    if caps.get("supports_tools", True):
        # Only advertise what this session can actually run, and only from the
        # set the caller asked for. A tool that would fail on arrival is noise
        # the model has to pay for; a session where nothing runs sends no tools
        # at all and says so instead of pretending.
        wanted = {schema["function"]["name"]: schema
                  for schema in (tools if tools is not None else TOOL_SCHEMAS)}
        offered = [schema for schema in tools_mod.tool_schemas()
                   if schema["function"]["name"] in wanted]
        if offered:
            params["tools"] = offered
    params.update(providers.active_model_params())
    return params


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


def _close(handle):
    for closer in ("close", "response"):
        target = getattr(handle, closer, None)
        if callable(target):
            try:
                target()
            except Exception:  # noqa: BLE001 - cleaning up must never raise
                pass
            return


def _progress(caps):
    def emit(state):
        payload = {"attempt": state["attempt"], "wait": state["wait"],
                   "elapsed": state["elapsed"], "window": state["window"],
                   "model": caps.get("name") or caps.get("model"),
                   "model_id": caps.get("model")}
        error = state.get("error")
        if error is not None:
            payload.update(error.payload())
        events.emit(RETRY, payload)

    return emit


def call_llm(messages, tools=None, on_delta=None, cancel=None):
    """One chat call. With on_delta, streams content tokens as they arrive.

    The streamed path assembles the same message shape as the blocking path,
    so the turn logic cannot tell the difference. Tool calls still arrive as
    complete calls - only text streams.

    Failures are retried up to retry.MAX_ATTEMPTS times inside one bounded
    window (retry.RETRY_WINDOW). A stream that breaks after it has already
    delivered text is not retried: the text is on screen and sending it again
    would duplicate it.
    """
    client = get_client()
    caps = providers.active_model_capabilities()
    params = _params(messages, tools, caps)
    base_url, api_key, _, _ = providers.active_config()
    label = caps.get("name") or caps.get("model")
    model_id = caps.get("model")
    host = base_url or ""

    if on_delta is None:
        def attempt():
            response = client.chat.completions.create(**params)
            return response.choices[0].message, _usage_from(response.usage)
    else:
        def attempt():
            content, calls, usage, streamed = [], {}, None, []
            stream = client.chat.completions.create(
                **params,
                stream=True,
                stream_options={"include_usage": True},
            )
            try:
                for chunk in stream:
                    # Stop reading as soon as the user cancels or moves to
                    # another model: closing here is what stops the tokens.
                    if cancel is not None and cancel.is_set():
                        raise errors_mod.Cancelled("the stream was abandoned")
                    if chunk.usage is not None:
                        usage = chunk.usage
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue
                    if delta.content:
                        content.append(delta.content)
                        streamed.append(delta.content)
                        on_delta(delta.content)
                    for call in delta.tool_calls or []:
                        slot = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                        slot["id"] += call.id or ""
                        if call.function:
                            slot["name"] += call.function.name or ""
                            slot["arguments"] += call.function.arguments or ""
            except Exception as failure:
                if isinstance(failure, errors_mod.Cancelled):
                    raise
                if streamed:
                    raise stream_interrupted(failure, partial="".join(streamed),
                                             api_key=api_key, model=label,
                                             model_id=model_id) from failure
                raise
            finally:
                _close(stream)

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

    return retry_mod.run(
        attempt,
        cancel=cancel,
        model=label,
        model_id=model_id,
        api_key=api_key,
        host=host,
        on_attempt=_progress(caps),
    )


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

Your current working directory is: {cwd}

You have skills available. Each one is a set of instructions for a task.
If a skill matches what the user wants, call read_skill first and follow it.

{skills}

## Project rules

The workspace keeps its rules in AGENTS.md. Where they disagree with anything
above or with your own defaults, the project rules win.

{instructions}
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

Your current working directory is: {cwd}

You are in plan mode. If asked which mode you are in, say plan mode.

You have skills available. Each one is a set of instructions for a task.
If a skill matches what the user wants, call read_skill first and follow it.

{skills}

## Project rules

The workspace keeps its rules in AGENTS.md. Where they disagree with anything
above or with your own defaults, the project rules win.

{instructions}
"""


def system_prompt(mode="build", role_instructions=""):
    from . import paths

    cwd = str(paths.project_root())
    skills = skills_prompt()
    template = PLAN_SYSTEM_PROMPT if mode == "plan" else SYSTEM_PROMPT
    # Instructions last: their own text can contain anything, and replacing
    # placeholders after injecting it would let it rewrite the prompt.
    prompt = (template.replace("{cwd}", cwd)
                    .replace("{skills}", skills)
                    .replace("{instructions}", instructions.load()))
    # Role instructions come after substitution too, so a custom instruction
    # can never smuggle a placeholder expansion into the prompt. They are the
    # user's words for this one role, kept separate from the project's rules.
    extra = (role_instructions or "").strip()
    if extra:
        prompt += f"\n\n## Instructions you were given for this task\n\n{extra}"
    return prompt
