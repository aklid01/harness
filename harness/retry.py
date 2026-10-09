"""Bounded retries for model requests.

One monotonic deadline owns the whole retry process - it is never restarted
per attempt - so a request that keeps failing is abandoned after at most
RETRY_WINDOW seconds of trying instead of grinding on forever. Only failures
that are plausibly transient are retried; everything else (a bad key, an
unknown model id, parameters the provider will not accept) fails straight away
with an explanation of what to fix.

Retries are cancellable: every wait and the loop itself watch the caller's
cancel event, and a request abandoned that way leaves nothing running behind it.
"""

import random
import threading
import time

from . import errors as errors_mod

RETRY_WINDOW = 30.0
BACKOFF_BASE = 0.4
BACKOFF_CAP = 4.0
MIN_WAIT = 0.1

CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 20.0
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


def _pause(host, seconds, cancel):
    """Wait before the next attempt, without a stampede of parallel retries.

    One retry slot per endpoint: the first waiter sleeps while holding it, so
    anything else failing against the same provider waits its turn instead of
    hammering a host that is already in trouble. Bailing out of the acquire
    still burns the wait, so no attempt retries sooner than it planned to.
    """
    if seconds <= 0:
        return _stopped(cancel)
    gate = _gate(host)
    if gate.acquire(timeout=seconds):
        try:
            return _sleep(seconds, cancel)
        finally:
            gate.release()
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
        model=None, model_id=None, api_key=None, host=""):
    """Run request() until it succeeds, gives up, or runs out of window.

    request() takes no arguments. on_attempt() receives a progress dict before
    each retry wait: {"attempt", "wait", "error", "model", "elapsed", "window"}.
    """
    deadline = time.monotonic() + float(window)
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
            if remaining <= MIN_WAIT:
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
            if _pause(host, wait, cancel):
                raise errors_mod.cancelled(model=model, model_id=model_id)
