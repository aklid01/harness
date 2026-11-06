"""Slash commands. Anything typed starting with / lands here."""

from . import compact as compaction
from . import events, session

COMMANDS = {
    "/plan": "plan this turn, read-only (alone: plan every message until /build)",
    "/ask": "answer this turn, read-only (alone: ask every message until /build)",
    "/build": "build now, the default (alone: leave plan or ask)",
    "/rewind": "jump back to an earlier point in this chat",
    "/sessions": "open a past chat",
    "/compact": "summarise the history so far and free up the context window",
    "/multi-model-review": "have independent reviewer models critique the change",
}


MODE_COMMANDS = {"/plan": "plan", "/ask": "ask", "/build": "build"}


def parse_mode_command(text):
    """Split a leading mode command off ``text``.

    Returns (kind, rest) with kind in ("plan", "ask", "build") and the text
    after the command, or (None, None) when the text does not start with one.
    The first token must match exactly, so "/planx" stays an unknown command.
    """
    parts = str(text or "").strip().split(None, 1)
    kind = MODE_COMMANDS.get(parts[0]) if parts else None
    if kind is None:
        return None, None
    return kind, (parts[1].strip() if len(parts) > 1 else "")


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


def review(messages, ctx, argument):
    """Run the multi-model review.

    The review happens in the background: the command returns immediately
    and the card follows the review as it runs. Nothing here edits a file.
    """
    from .review import command as review_command

    args = review_command.parse_args(argument)
    if args["help"]:
        events.emit(events.NOTE, {"text": review_command.HELP})
        return messages
    if args["error"]:
        events.emit(events.NOTE, {"text": args["error"]})
        return messages
    if ctx is None or not hasattr(ctx, "request_review"):
        events.emit(
            events.NOTE,
            {"text": "reviews need the desktop app - run it from a session there"},
        )
        return messages
    ctx.request_review(args)
    return messages


def handle(command, messages, ctx=None):
    if command == "/compact":
        return compact(messages)
    if command == "/rewind":
        return rewind(messages)
    if command == "/sessions":
        return sessions(messages)
    if command == "/multi-model-review" or command.startswith("/multi-model-review "):
        argument = command[len("/multi-model-review") :].strip()
        return review(messages, ctx, argument)
    events.emit(
        events.NOTE,
        {"text": "\n".join(f"{name}  -  {help}" for name, help in COMMANDS.items())},
    )
    return messages
