import json

from . import commands
from . import compact
from . import events
from . import history
from . import providers
from . import session
from .context import reminder
from .errors import ModelError  # noqa: F401 - kept for callers importing it from here
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

    def _turn(self):
        self._announce_mode()
        # The user message that opened this turn, so a retry resumes from it
        # instead of appending a second copy.
        self._turn_index = len(self.messages) - 1
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

            self.messages.append(message.model_dump(exclude_none=True))
            session.save(self.messages)
            events.emit(events.USAGE, dict(usage))

            if self.debug:
                events.emit(events.DEBUG, message.model_dump(exclude_none=True))

            if message.content:
                events.emit(events.MESSAGE, {"role": "assistant", "content": message.content})

            if not message.tool_calls:
                break

            for tool_call in message.tool_calls:
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
                    args, result = execute(tool_call, note=(message.content or ""))
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
