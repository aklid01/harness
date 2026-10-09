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

import json
import tempfile
from pathlib import Path

from . import config

CAP = 10_000  # chars of a fresh tool result the agent sees inline
STUB = 300  # chars kept once the turn that produced it is over

TRIMMED = "[output trimmed:"  # marker, so stripping twice is a no-op
SUMMARY = "<summary>"  # marks the handoff note compaction leaves behind
SPILLS = []  # temp files belonging to the current turn

OMITTED = "[left out of this request to fit the model's context window]"

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
    from . import config

    limit = (int(window) if int(window or 0) > 0 else config.CONTEXT_WINDOW) * config.COMPACT_AT
    if estimate(messages) <= limit:
        return list(messages), ""

    frozen = locked(messages)
    protected = set(range(max(frozen, len(messages) - tail), len(messages)))
    request, omitted, stubbed = [], 0, 0
    for index, message in enumerate(messages):
        if index < frozen or index == 0 or index in protected:
            request.append(message)
            continue
        if message.get("role") == "tool":
            stub = dict(message)
            stub["content"] = OMITTED
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
        if index in protected or not _droppable(request[index]):
            index += 1
            continue
        del request[index]
        omitted += 1
        index = floor

    if not (omitted or stubbed):
        return request, ""
    note = (f"trimmed the request to fit this model's context window: "
            f"{omitted} older message(s) left out, {stubbed} tool result(s) stubbed. "
            "The stored history is unchanged.")
    return request, note



# ------------------------------------------------------------------- 1. cap


def spill(text):
    """Park the full output on disk for the rest of this turn."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="harness-", suffix=".txt", delete=False,
        encoding="utf-8",
    )
    handle.write(text)
    handle.close()
    SPILLS.append(Path(handle.name))
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
        return text[:CAP] + f"\n\n{TRIMMED} {len(text) - CAP} chars cut and the rest could not be saved.]"
    return (
        text[:CAP] + f"\n\n{TRIMMED} {len(text) - CAP} of {len(text)} chars cut. "
        f"The whole output is at {path} - page through it with "
        "head, tail, sed -n or grep. It is deleted when this turn ends.]"
    )


def sweep():
    """Delete this turn's temp files. Their paths die with the tool results."""
    for path in SPILLS:
        path.unlink(missing_ok=True)
    SPILLS.clear()


def locked(messages):
    """Length of the frozen prefix - everything up to and including the newest
    summary. Derived rather than remembered, so it stays correct across
    /compact, /rewind and switching sessions."""
    for index in range(len(messages) - 1, -1, -1):
        if SUMMARY in (messages[index].get("content") or ""):
            return index + 1
    return 0


# ----------------------------------------------------------------- 2. strip


def strip(messages):
    """Shrink every tool result that is no longer part of the live turn.

    Called once a turn has finished, so by now "everything unlocked" and
    "everything the model no longer needs in full" are the same set.
    """
    shrunk = 0
    for message in messages[locked(messages):]:
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


def fit(messages):
    """Last resort: discard whole tool results, oldest first, until it fits.

    Returns how many went. Normally zero - cap and strip do the real work.
    """
    budget = config.CONTEXT_WINDOW * config.COMPACT_AT
    dropped = 0
    for message in messages[locked(messages):]:
        if estimate(messages) <= budget:
            break
        if message["role"] == "tool" and TRIMMED not in (message.get("content") or ""):
            message["content"] = f"{TRIMMED} dropped to fit the context window.]"
            dropped += 1
    return dropped
