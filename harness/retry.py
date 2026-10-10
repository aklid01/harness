"""Bounded retries for model requests.

The budget is an attempt count plus one monotonic deadline shared by every
attempt, and that deadline is only a backstop. The invariant that matters: it
has to span more than one attempt, because the old 30s window was smaller
than two 20s read timeouts - one slow provider burned the entire budget
before the retry had even started, so a conversation that only needed a
second chance died instead. Only failures that are plausibly transient are
retried; everything else (a bad key, an unknown model id, parameters the
provider will not accept) fails straight away with an explanation of what to
fix.

Retries are cancellable: every wait and the loop itself watch the caller's
cancel event, and a request abandoned that way leaves nothing running behind it.
"""

import random
import threading
import time

from . import errors as errors_mod

MAX_ATTEMPTS = 3
RETRY_WINDOW = 270.0
BACKOFF_BASE = 0.4
BACKOFF_CAP = 4.0
MIN_WAIT = 0.1
_SLICE = 0.05

CONNECT_TIMEOUT = 6.0
# What one attempt waits for the provider to say something: on a stream that
# is the first token, and a reasoning model at high effort legitimately
# thinks for longer than 20s before its first word.
READ_TIMEOUT = 90.0
POOL_TIMEOUT = 5.0

_GATES = {}
_GATES_LOCK = threading.Lock()


def timeouts():
    """Connect/read budget for one attempt. Overriding openai means WE decide."""
    import openai

    return openai.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT,
                          write=READ_TIMEOUT, pool=POOL_TIMEOUT)


def _gate(host):
    with _GATES_LOCK:
        return _GATES.setdefault(host or "", threading.Lock())


def _stopped(cancel):
    return cancel is not None and cancel.is_set()


def _sleep(seconds, cancel):
    if cancel is None:
        time.sleep(seconds)
        return False
    return bool(cancel.wait(seconds))


def _pause(host, seconds, cancel, limit=None):
    """Wait before the next attempt, without a stampede of parallel retries.

    One retry slot per endpoint: whoever gets it sleeps while holding it, so
    anything else failing against the same host waits its turn instead of
    hammering a provider that is already in trouble. A waiter never blocks
    past ``limit`` (its remaining retry budget), and the loop checks for
    cancellation between slices, so Stop stays responsive.
    """
    if seconds <= 0:
        return _stopped(cancel)
    gate = _gate(host)
    budget = limit if limit is not None else seconds
    deadline = time.monotonic() + max(seconds, budget)
    while True:
        if _stopped(cancel):
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            break
        if gate.acquire(timeout=min(_SLICE, left)):
            try:
                return _sleep(seconds, cancel)
            finally:
                gate.release()
        if _stopped(cancel):
            return True
    return _sleep(0.0, cancel)


def _backoff(attempt):
    top = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1)))
    return random.uniform(0.0, top)


def _next_wait(attempt, err, remaining):
    """Seconds to wait before the next attempt, or None to give up now."""
    if err.kind == "rate_limited" and err.retry_after is not None:
        asked = err.retry_after
        # Wait only when the provider's own delay still fits the deadline.
        if asked + MIN_WAIT > remaining:
            return None
        return max(asked, MIN_WAIT)
    return min(_backoff(attempt), max(MIN_WAIT, remaining - MIN_WAIT))


def run(request, *, window=RETRY_WINDOW, cancel=None, on_attempt=None,
        model=None, model_id=None, api_key=None, host="", attempts=MAX_ATTEMPTS):
    """Run request() until it succeeds, gives up, or runs out of budget.

    The budget is the attempt count first and the shared window second: one
    attempt that burns its whole read timeout must leave a whole retry behind
    it, which is why window is sized to span every attempt. on_attempt()
    receives a progress dict before each retry wait: {"attempt", "wait",
    "error", "model", "elapsed", "window"}.
    """
    deadline = time.monotonic() + float(window)
    limit = max(1, int(attempts))
    attempt = 0
    while True:
        if _stopped(cancel):
            raise errors_mod.cancelled(model=model, model_id=model_id)
        attempt += 1
        try:
            return request()
        except Exception as failure:
            err = errors_mod.classify(failure, api_key=api_key, model=model,
                                      model_id=model_id)
            if _stopped(cancel):
                raise errors_mod.cancelled(model=model, model_id=model_id)
            if not err.transient:
                raise err
            remaining = deadline - time.monotonic()
            if attempt >= limit or remaining <= MIN_WAIT:
                # Out of tries or out of window - either way the turn is over,
                # and the caller says so with "still failing after attempts".
                err.exhausted = True
                raise err
            wait = _next_wait(attempt, err, remaining)
            if wait is None:
                err.exhausted = True
                raise err
            if on_attempt is not None:
                on_attempt({
                    "attempt": attempt + 1,
                    "wait": round(wait, 1),
                    "error": err,
                    "model": model,
                    "elapsed": round(time.monotonic() - (deadline - float(window)), 1),
                    "window": float(window),
                })
            if _pause(host, wait, cancel, limit=remaining):
                raise errors_mod.cancelled(model=model, model_id=model_id)
