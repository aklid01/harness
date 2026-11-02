"""Slash commands. Anything typed starting with / lands here."""

from . import compact as compaction
from . import events, session

COMMANDS = {
    "/rewind": "jump back to an earlier point in this chat",
    "/sessions": "open a past chat",
    "/compact": "summarise the history so far and free up the context window",
}


def preview(message):
    if message.get("tool_calls"):
        return "-> " + message["tool_calls"][0]["function"]["name"]
    return " ".join(str(message.get("content") or "").split())[:70]


def redraw(messages, label):
    """The screen no longer matches the history, so redraw from scratch."""
    events.emit(
        events.SESSION,
        {"label": label, "messages": messages, "id": session.current_id()},
    )
    return messages


def rewind(messages):
    rows = [f"{m['role']:<9} {preview(m)}" for m in messages]
    choice = events.interact(events.PICK, {"title": "rewind to", "rows": rows})
    if choice is None:
        return messages
    session.rewind_to(choice + 1)
    return redraw(messages[: choice + 1], "rewound")


def sessions(messages):
    saved = session.all_sessions()
    if not saved:
        events.emit(events.NOTE, {"text": "no saved chats yet"})
        return messages
    rows = [f"{s['id']}  {s['title']}" for s in saved]
    choice = events.interact(events.PICK, {"title": "open chat", "rows": rows})
    if choice is None:
        return messages

    return redraw(session.open_session(saved[choice]["id"]), "opened")


def compact(messages, window=None):
    before = len(messages)
    try:
        events.emit(events.STATUS, {"active": "compacting"})
        compacted = compaction.compact(messages, window)
    except Exception as failure:  # noqa: BLE001
        # Compaction is one more API call, and it fires when the window is
        # nearly full - the worst moment to lose the session over a rate limit.
        events.emit(events.STATUS, {"active": None})
        events.emit(
            events.NOTE,
            {
                "text": f"compaction failed ({type(failure).__name__}); transcript kept as is"
            },
        )
        return messages
    events.emit(events.STATUS, {"active": None})
    if len(compacted) == before:
        events.emit(events.NOTE, {"text": "nothing old enough to compact yet"})
        return messages
    session.compacted(compacted)
    events.emit(events.COMPACTED, {"before": before, "messages": compacted})
    return compacted


def handle(command, messages):
    if command == "/compact":
        return compact(messages)
    if command == "/rewind":
        return rewind(messages)
    if command == "/sessions":
        return sessions(messages)
    events.emit(
        events.NOTE,
        {"text": "\n".join(f"{name}  -  {help}" for name, help in COMMANDS.items())},
    )
    return messages
