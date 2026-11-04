"""Keeping the transcript small enough to send.

Three mechanisms, cheapest first. Only the first two live here; the expensive
one is compact.py.

1. cap    - a fresh tool result is trimmed and the full text parked in a temp
            file the agent can page through. Free: the decision is made once,
            when the result is created, so it never edits the prefix.
2. strip  - once a turn is over, its tool results shrink to a stub. The edit
            lands at the tail, right before the next user message, so the
            cached prefix in front of it survives.
3. drop   - a single request is still too big. Throw tool results away whole,
            oldest first, until it fits.

Everything here refuses to touch the locked prefix - the frozen
system + summary + head that compaction leaves behind. That block has to stay
byte-identical to stay cached.
"""

import contextvars
import json
import tempfile
from pathlib import Path

from . import config

CAP = 10_000  # chars of a fresh tool result the agent sees inline
STUB = 300  # chars kept once the turn that produced it is over

CAPPED = "[output capped:"  # cap() wrote this result's tail to a spill file
TRIMMED = "[output trimmed:"  # strip() marker; stripping twice is a no-op
SUMMARY = "<summary>"  # the handoff note compaction leaves behind
_OMITTED = "[left out of this request to fit the model's context window]"

# Spill files parked during the CURRENT turn. Set per turn by begin_turn();
# code outside a turn (tests, one-shot engine use) shares the fallback.
_SPILLS: list = []
_TURN_SPILLS = contextvars.ContextVar("harness_spills", default=None)


def _turn_spills():
    ref = _TURN_SPILLS.get()
    return ref if ref is not None else _SPILLS


def begin_turn():
    """Start a fresh spill registry for this turn.

    The previous value is simply replaced: a turn thread never outlives its
    turn, and sweep() only ever touches the current registry.
    """
    _TURN_SPILLS.set([])


# Oldest-to-newest: shrink tool output, then whole exchanges, then give up on
# the middle. Only ever applied to a copy, never to the stored transcript.
TAIL = 8


def _droppable(message):
    """Can this message go without orphaning the tool result that follows it?"""
    return message.get("role") != "tool" and not message.get("tool_calls")


def prepare(messages, window=None, tail=TAIL):
    """Assemble one request's messages without touching the stored history.

    Returns (request_messages, note). Smaller models do not get the whole
    transcript: the locked prefix and the live tail always go, older tool
    results shrink to a stub and older exchanges are left out entirely. The
    caller keeps its own copy, so what is stored stays exactly as it was - the
    note is what gets shown to the user instead of pretending it all fit.
    """

    limit = (
        int(window) if int(window or 0) > 0 else config.context_window()
    ) * config.COMPACT_AT
    if estimate(messages) <= limit:
        return list(messages), ""

    frozen = locked(messages)
    # Protect by object identity, not position: the second pass deletes from
    # the request, so positional indices shift and the live tail would lose
    # its protection (the current question could be deleted out of a request).
    protected = {
        id(message) for message in messages[max(frozen, len(messages) - tail) :]
    }
    request, omitted, stubbed = [], 0, 0
    for index, message in enumerate(messages):
        if index < frozen or index == 0 or id(message) in protected:
            request.append(message)
            continue
        if message.get("role") == "tool":
            stub = dict(message)
            stub["content"] = _OMITTED
            request.append(stub)
            stubbed += 1
        elif _droppable(message):
            omitted += 1
        else:
            request.append(message)

    # Still too big: leave out whole exchanges from the middle, oldest first.
    # Never the system prompt, the compaction handoff, or the live tail.
    index = max(frozen, 1)
    floor = max(frozen, 1)
    while estimate(request) > limit and index < len(request):
        if id(request[index]) in protected or not _droppable(request[index]):
            index += 1
            continue
        del request[index]
        omitted += 1
        index = floor

    if not (omitted or stubbed):
        return request, ""
    note = (
        f"trimmed the request to fit this model's context window: "
        f"{omitted} older message(s) left out, {stubbed} tool result(s) stubbed. "
        "The stored history is unchanged."
    )
    return request, note


# ------------------------------------------------------------------- 1. cap


def spill(text):
    """Park the full output on disk for the rest of this turn."""
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="harness-",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    )
    handle.write(text)
    handle.close()
    _turn_spills().append(Path(handle.name))
    return handle.name


def cap(text):
    """Trim a fresh tool result, leaving a pointer to the whole thing."""
    if len(text) <= CAP:
        return text

    try:
        path = spill(text)
    except OSError:
        # No temp file (read-only /tmp, no space). Still better to trim and
        # say so than to fail the tool call outright.
        return (
            text[:CAP]
            + f"\n\n{CAPPED} {len(text) - CAP} chars cut and the rest could not be saved.]"
        )
    return (
        text[:CAP] + f"\n\n{CAPPED} {len(text) - CAP} of {len(text)} chars cut. "
        f"The whole output is at {path} - page through it with "
        "head, tail, sed -n or grep. It is deleted when this turn ends.]"
    )


def sweep():
    """Delete this turn's temp files. Their paths die with the tool results."""
    files = _turn_spills()
    for path in files:
        path.unlink(missing_ok=True)
    files.clear()


def locked(messages):
    """Length of the frozen prefix - everything up to and including the newest
    summary. Derived rather than remembered, so it stays correct across
    /compact, /rewind and switching sessions.

    The summary is detected structurally (a user message that STARTS with the
    marker), never by substring: a substring match also fires on tool output
    that merely quotes the marker - e.g. the agent reading compact.py's own
    source - and silently froze everything older than that read for the rest
    of the session.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = message.get("content") or ""
        if message.get("role") == "user" and content.startswith(SUMMARY):
            return index + 1
    return 0


# ----------------------------------------------------------------- 2. strip


def strip(messages):
    """Shrink every tool result that is no longer part of the live turn.

    Called once a turn has finished, so by now "everything unlocked" and
    "everything the model no longer needs in full" are the same set. Honors
    only its own marker: a capped result (CAPPED) still shrinks here - that is
    the point, its full text is gone with the spill file anyway.
    """
    shrunk = 0
    for message in messages[locked(messages) :]:
        content = message.get("content") or ""
        if message["role"] != "tool" or TRIMMED in content or len(content) <= STUB:
            continue

        message["content"] = (
            content[:STUB] + f"\n\n{TRIMMED} {len(content) - STUB} more chars. "
            "Run the command again if you need them.]"
        )
        shrunk += 1
    return shrunk


# ------------------------------------------------------------------ 3. drop


def estimate(messages):
    """Rough token count. Good enough to decide whether to panic."""
    return sum(len(json.dumps(m)) for m in messages) // 4
