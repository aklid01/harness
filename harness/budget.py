"""Independent phase budgets.

Planning, building and decision review are separate finite phases, and each
gets its own budget - requests, tool calls, elapsed time. One phase running out
never spent another's allowance, because a Budget belongs to the phase that
created it and nothing else reads it.

A budget is not a turn counter to burn through: a phase that has finished stops
early, and a phase that stops making progress is cut off with a reason rather
than spending its whole allowance rediscovering nothing. Tracking ``seen`` lets
a caller detect a repeated, identical iteration and give up.
"""

import time


class Budget:
    """Requests/tools/time limits for one phase. None means unlimited."""

    def __init__(self, turns=None, tool_calls=None, seconds=None):
        self.turns = turns
        self.tool_calls = tool_calls
        self.seconds = seconds
        self._turns = 0
        self._tools = 0
        self._start = time.monotonic()

    def turn(self):
        """Count one model request. True once the request budget is passed."""
        self._turns += 1
        return self.turns is not None and self._turns > self.turns

    def tool(self):
        """Count one tool call. True once the tool budget is passed."""
        self._tools += 1
        return self.tool_calls is not None and self._tools > self.tool_calls

    def timed_out(self):
        return (
            self.seconds is not None and (time.monotonic() - self._start) > self.seconds
        )

    def snapshot(self):
        return {
            "turns": self._turns,
            "tools": self._tools,
            "elapsed_s": round(time.monotonic() - self._start, 1),
        }

    def reason(self):
        if self.seconds is not None and self.timed_out():
            return "hit its time budget"
        if self.turns is not None and self._turns > self.turns:
            return "hit its request budget"
        if self.tool_calls is not None and self._tools > self.tool_calls:
            return "hit its tool-call budget"
        return "ran out of budget"


class Progress:
    """Detects a phase that keeps saying the same thing.

    A loop is confused when it repeats an identical step - the same tool call
    with the same arguments - more than ``limit`` times in a row. That is the
    harness's own earlier idea (the repeated-text-call guard) applied across a
    phase, so a stuck phase ends with a status instead of a silent grind.
    """

    def __init__(self, limit=3):
        self.limit = limit
        self._last = None
        self._count = 0

    def repeat(self, key):
        """Feed one step's fingerprint. True when it has repeated too often."""
        import hashlib

        h = hashlib.sha1(str(key).encode("utf-8"), usedforsecurity=False).hexdigest()
        if h == self._last:
            self._count += 1
        else:
            self._last = h
            self._count = 1
        return self._count > self.limit
