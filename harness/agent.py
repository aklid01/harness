import itertools
import json
from types import SimpleNamespace

from . import commands
from . import compact
from . import events
from . import history
from . import providers
from . import texttools
from . import session
from .context import reminder
from .errors import ModelError
from . import errors as errors_mod
from .llm import call_llm, system_prompt
from .todos import active_form
from .tools import execute


PLAN_NOTICE = (
    "<system-reminder>\n"
    "Your operational mode is PLAN (read-only).\n"
    "You are a planning assistant right now, not a coding agent. Explore, explain, "
    "and propose concrete changes, but do NOT call write_file or str_replace - they "
    "are blocked and the call will be wasted.\n"
    "If asked which mode you are in, say plan mode.\n"
    "</system-reminder>"
)
BUILD_NOTICE = (
    "<system-reminder>\n"
    "Your operational mode has changed to BUILD.\n"
    "You are no longer read-only. You may edit files, run commands, and use tools.\n"
    "</system-reminder>"
)


# Every Runner gets its own request-id space, so a tool call made by one
# conversation can never collide with an identical call made by another.
TURN_KEYS = itertools.count(1)


class Runner:
    """One agent conversation. Emits events; never touches the screen."""

    def __init__(self, resume=False, debug=False, stream=False, cancel_event=None, mode="build"):
        self.debug = debug
        self.stream = stream
        self.cancel_event = cancel_event
        self.mode = mode
        self._announced_mode = None
        self._turn_index = 0
        self._turn_text = ""
        self._step = 0
        self._text_retries = 0
        self._text_calls: set = set()
        self._request_prefix = f"r{next(TURN_KEYS)}"
        self.messages = [{"role": "system", "content": system_prompt(mode)}]
        if resume:
            saved = session.all_sessions()
            if saved:
                self.messages = session.open_session(saved[0]["id"])
                history.strip(self.messages)
                events.emit(events.SESSION, {"label": "resumed", "messages": self.messages, "id": session.current_id()})

    def handle_input(self, user_text):
        if user_text.startswith("/"):
            self.messages = commands.handle(user_text, self.messages)
            session.save(self.messages)
            return

        self.messages.append({"role": "user", "content": user_text})
        # Saved before the call, so a failed request never loses what was asked.
        session.save(self.messages)
        events.emit(events.MESSAGE, {"role": "user", "content": user_text})
        self._turn()

    def _announce_mode(self):
        """Put mode switches in the transcript, where the model will see them.

        A fresh build session is the status quo ante, so it stays silent.
        Anything else - starting in plan, or switching either way - gets an
        explicit notice appended silently (no GUI event, never rendered).
        """
        if self.mode == self._announced_mode:
            return
        previous, self._announced_mode = self._announced_mode, self.mode
        if self.mode == "plan" or previous is not None:
            notice = PLAN_NOTICE if self.mode == "plan" else BUILD_NOTICE
            self.messages.append({"role": "user", "content": notice})

    def cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _model_label(self):
        caps = providers.active_model_capabilities()
        return caps.get("name") or caps.get("model")

    def _retry_info(self):
        """Where to resume from if the user retries this request later."""
        return {"count": self._turn_index, "text": self._turn_text,
                "model": self._model_label()}

    def _fail(self, failure):
        """Report a failed request without losing the message that caused it."""
        api_key = providers.active_config()[1]
        error = errors_mod.classify(failure, api_key=api_key, model=self._model_label())
        if error.kind == "cancelled":
            events.emit(events.NOTE, {"text": "stopped"})
            return
        events.emit(events.STATUS, {"active": None})
        payload = error.payload()
        payload["text"] = error.text()
        if error.kind != "stream_interrupted":
            payload["retry"] = self._retry_info()
        events.emit(events.ERROR, payload)

    def _text_call(self, message):
        """A model that cannot do native tool calls may write one out in text.

        Only ever consulted when the message carries call markup. Returns
        (call, narration):

        * a usable call and its narration, when this model is allowed the
          text-call format;
        * a refused call explaining why, when the format is not enabled for
          this model or the markup is unusable;
        * (None, None) when there is no call markup at all.
        """
        content = message.content or ""
        if not texttools.looks_like_call(content):
            return None, None
        caps = providers.active_model_capabilities()
        from . import tools as tools_module

        if not caps.get("supports_text_tools"):
            model = caps.get("name") or "this model"
            return (texttools.TextCall(error=(
                f"{model} does not use native tool calls and the text-call format "
                "is not enabled for it. Nothing was executed - enable "
                "'text tool calls' for this model in Settings, or ask it in plain "
                "words.")), None)
        available = tools_module.available_tool_names()
        schemas = tools_module.tool_schemas()
        return texttools.parse_with_narration(content, schemas=schemas, available=available)

    def _unrecognised_call(self, text):
        """Tell the user, plainly, that a call was not run - and why."""
        events.emit(events.NOTE, {
            "text": "The response looked like a tool call but was not one we could run, "
                    "so nothing was executed.",
            "detail": text,
            "kind": "unrecognised_tool_call",
        })

    def _corrective_note(self, call):
        """What the model is told when its text call could not be used."""
        available = ", ".join(__import__("harness.tools", fromlist=["x"]).available_tool_names())
        return (
            "<system-reminder>\n"
            f"Your last message contained a tool call that could not be run: {call.error}\n"
            "Send the call as a real tool call (the tools list), not as text, or answer "
            f"in plain language. Tools available right now: {available}.\n"
            "</system-reminder>"
        )

    def _turn(self):
        self._announce_mode()
        # The user message that opened this turn, so a retry resumes from it
        # instead of appending a second copy.
        self._turn_index = len(self.messages) - 1
        self._text_retries = 0
        self._text_calls = set()
        for message in reversed(self.messages[:self._turn_index + 1]):
            if message.get("role") == "user" and not str(message.get("content") or "").startswith("<"):
                self._turn_text = str(message.get("content") or "")
                break
        while True:
            if self.cancelled():
                events.emit(events.NOTE, {"text": "stopped"})
                return
            injection = reminder()
            if self.mode == "plan":
                injection["content"] += (
                    "\n<mode>\nPlan mode: you are read-only. Explore, explain, and "
                    "propose concrete changes, but do NOT call write_file or "
                    "str_replace - they are blocked and this turn will be wasted.\n</mode>"
                )
            events.emit(events.INJECTION, {"content": injection["content"]})

            # Assemble the request separately from the stored history, so a
            # model with a smaller context window gets a trimmed copy and the
            # transcript on disk stays exactly as it was.
            request, trimmed = history.prepare(
                self.messages + [injection], providers.active_context_window())
            if trimmed:
                events.emit(events.NOTE, {"text": trimmed})

            events.emit(events.STATUS, {"active": active_form()})
            self._step += 1
            request_id = f"{self._request_prefix}:{self._step}"
            try:
                on_delta = None
                if self.stream:
                    def on_delta(text):
                        if self.cancelled():
                            return
                        events.emit(events.MESSAGE_DELTA, {"text": text})
                message, usage = call_llm(request, on_delta=on_delta,
                                          cancel=self.cancel_event)
            except Exception as failure:  # noqa: BLE001 - report, don't kill the session
                if self.cancelled():
                    events.emit(events.NOTE, {"text": "stopped"})
                    return
                self._fail(failure)
                return
            events.emit(events.STATUS, {"active": None})
            # A generation the user has moved on from (stop, model switch) is
            # abandoned here: its answer is not appended and its turn is over.
            if self.cancelled():
                events.emit(events.NOTE, {"text": "stopped"})
                return

            text_call, narration = (None, None)
            if not message.tool_calls:
                text_call, narration = self._text_call(message)
                if text_call is not None and text_call.ok:
                    # The same call text again means the model is looping, not
                    # working: it already got this exact result. Tell it so and
                    # stop - never execute the same text call twice.
                    fingerprint = f"{text_call.name}:{text_call.arguments}"
                    if fingerprint in self._text_calls:
                        self.messages.append(message.model_dump(exclude_none=True))
                        session.save(self.messages)
                        events.emit(events.NOTE, {
                            "text": "The model sent the same text tool call again "
                                    "after receiving its result, so the turn was "
                                    "stopped instead of running it twice.",
                            "kind": "repeated_text_call",
                        })
                        self.messages.append({"role": "user", "content": (
                            "<system-reminder>\n"
                            "You already sent this exact tool call and its result is "
                            "in the conversation. Do not send it again; use the result "
                            "you have and answer the user.\n"
                            "</system-reminder>"
                        )})
                        session.save(self.messages)
                        break
                    self._text_calls.add(fingerprint)
                    # The narration is real output: show it, keep it in the
                    # transcript, then run the call exactly like a native one.
                    if narration:
                        events.emit(events.MESSAGE, {"role": "assistant", "content": narration})
                elif text_call is not None:
                    # Unusable markup. Say so plainly, and give the model one
                    # bounded chance to redo it as a real call. Never silently.
                    if narration:
                        events.emit(events.MESSAGE, {"role": "assistant", "content": narration})
                    self.messages.append(message.model_dump(exclude_none=True))
                    session.save(self.messages)
                    events.emit(events.USAGE, dict(usage))
                    self._unrecognised_call(text_call.reason())
                    if self._text_retries < 1:
                        self._text_retries += 1
                        self.messages.append({"role": "user",
                                              "content": self._corrective_note(text_call)})
                        session.save(self.messages)
                        continue
                    break

            if text_call is not None and text_call.ok:
                converted = SimpleNamespace(
                    content=narration or None,
                    tool_calls=[text_call.as_tool_call(f"text-{self._turn_index}-{self._step}")])
                converted.model_dump = lambda exclude_none=True: {
                    "role": "assistant",
                    **({"content": narration} if narration else {}),
                    "tool_calls": [
                        {"id": call.id, "type": "function",
                         "function": {"name": call.function.name,
                                      "arguments": call.function.arguments}}
                        for call in converted.tool_calls],
                }
                message = converted

            self.messages.append(message.model_dump(exclude_none=True))
            session.save(self.messages)
            events.emit(events.USAGE, dict(usage))

            if self.debug:
                events.emit(events.DEBUG, message.model_dump(exclude_none=True))

            if message.content:
                events.emit(events.MESSAGE, {"role": "assistant", "content": message.content})

            if not message.tool_calls:
                break

            for index, tool_call in enumerate(message.tool_calls):
                if self.cancelled():
                    events.emit(events.NOTE, {"text": "stopped"})
                    return
                name = tool_call.function.name
                call_id = getattr(tool_call, "id", None)
                try:
                    args = json.loads(tool_call.function.arguments)
                except ValueError:
                    args = {}
                if self.mode == "plan" and name in ("write_file", "str_replace"):
                    result = f"Plan mode: read-only, no edits made. Describe the change instead of calling {name}."
                else:
                    # One call, one request, one execution: a replayed frame or
                    # a re-entered stream replays the recorded result.
                    args, result = execute(tool_call, note=(message.content or ""),
                                           call_key=(request_id, call_id or index))
                events.emit(events.TOOL_CALL, {"call_id": call_id, "name": name, "args": args, "nested": False})
                events.emit(events.TOOL_RESULT, {"call_id": call_id, "name": name, "args": args, "result": result, "nested": False})
                if tool_call.function.name == "write_todos" and isinstance(args.get("todos"), list):
                    events.emit(events.TODOS, {"todos": args["todos"]})

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
                session.save(self.messages)

        history.sweep()   # the turn is over: bin its temp files
        history.strip(self.messages)  # ...and shrink the tool output it produced

        if compact.needed(usage, providers.active_context_window()):
            self.messages = commands.compact(self.messages, providers.active_context_window())


def main():
    from .gui.app import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
