"""Engine event bus and frontend interaction point.

The agent engine emits events instead of drawing to a screen. Any frontend
(PySide6 GUI, TUI, the Electron sidecar, future API) subscribes to render them
and registers a responder to answer blocking interaction requests.

Two modes coexist:

* Global: the classic single-owner model. Subscribers get every event and a
  single responder answers approvals/picks. Used by the legacy PySide6 GUI and
  the tests.

* Scoped (per session): while a ``Session`` is attached to the current thread
  (see ``attached``), ``emit``/``interact`` route to that session only. The
  server keeps many sessions running concurrently in one process, so this is how
  each turn's events reach the right client without a global bus fight.

Single process, synchronous by design: interact() blocks the calling engine
thread until the frontend answers, so approval stays blocking from the engine's
perspective. No UI imports here.
"""

import contextlib
import contextvars

MESSAGE = "message"
MESSAGE_DELTA = "message_delta"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
TODOS = "todos"
USAGE = "usage"
STATUS = "status"
NOTE = "note"
INJECTION = "injection"
DEBUG = "debug"
COMPACTED = "compacted"
SESSION = "session"
SUBAGENT = "subagent"
ERROR = "error"
RETRY = "retry"

APPROVAL = "approval"
PICK = "pick"

_subscribers = []
_responder = None
_ACTIVE = contextvars.ContextVar("harness_active_session", default=None)


def subscribe(fn):
    _subscribers.append(fn)
    return fn


def unsubscribe(fn):
    if fn in _subscribers:
        _subscribers.remove(fn)


def emit(type, payload=None):
    session = _ACTIVE.get()
    if session is not None:
        session.on_event(type, payload or {})
        return
    for fn in list(_subscribers):
        fn(type, payload or {})


def set_responder(fn):
    global _responder
    _responder = fn


def interact(kind, payload=None):
    session = _ACTIVE.get()
    if session is not None:
        return session.on_interact(kind, payload or {})
    if _responder is None:
        return None
    return _responder(kind, payload or {})


@contextlib.contextmanager
def attached(session):
    """Route emit/interact to ``session`` for the current thread/context."""
    token = _ACTIVE.set(session)
    try:
        yield session
    finally:
        try:
            _ACTIVE.reset(token)
        except ValueError:
            pass


def reset():
    del _subscribers[:]
    global _responder
    _responder = None
