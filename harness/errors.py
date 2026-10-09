"""Turn a failed model request into something the user can act on.

One failure mode is not like another: a dropped connection is worth another
try inside the retry window, a wrong API key is not. Classify() maps whatever
the SDK raised onto a ModelError carrying kind, transient-ness, the HTTP status,
any Retry-After the provider sent, and a message that never leaks the key.
"""

import json
import re

TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 529})
AUTH_STATUS = frozenset({401, 403})
REJECTED_STATUS = frozenset({400, 404, 405, 413, 422})

HEADLINES = {
    "network": "could not reach the provider",
    "timeout": "the request timed out",
    "rate_limited": "the provider rate-limited this request",
    "server": "the provider reported a server error",
    "auth": "authentication failed",
    "rejected": "the provider rejected the request",
    "not_found": "the model was not found",
    "unsupported": "the provider does not support these request parameters",
    "malformed": "the provider returned a malformed response",
    "stream_interrupted": "the response stream was interrupted",
    "no_key": "no API key is configured for this model",
    "cancelled": "cancelled",
    "unknown": "the request failed",
}

TRANSIENT_KINDS = frozenset({"network", "timeout", "rate_limited", "server", "malformed"})

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer|token|api[-_]?key|authorization)\b[\"'=:\s]+\S+"),
    re.compile(r"\b(sk|thk|atr|pk)-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}"),
)


class Cancelled(Exception):
    """The user stopped the operation (or switched away from it)."""


def scrub(text, secrets=()):
    """Keep secrets out of anything a human, a log line or the UI will see."""
    if not text:
        return text
    out = str(text)
    for secret in secrets:
        if secret and isinstance(secret, str) and len(secret) >= 6:
            out = out.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("***", out)
    return out.strip()


class ModelError(Exception):
    """A model request failed, with enough context to explain what to do."""

    def __init__(self, kind, detail="", *, status=None, retry_after=None,
                 model=None, model_id=None, partial=None, transient=None,
                 exhausted=False, cause=None):
        self.kind = kind
        self.detail = scrub(detail)
        self.status = status
        self.retry_after = retry_after
        self.model = model
        self.model_id = model_id
        self.partial = partial
        self.transient = kind in TRANSIENT_KINDS if transient is None else transient
        self.exhausted = exhausted
        self.cause = cause

    @property
    def headline(self):
        return HEADLINES.get(self.kind, HEADLINES["unknown"])

    def __str__(self):
        body = self.detail or self.headline
        return scrub(f"{self.headline}: {body}" if self.detail else self.headline)

    def text(self):
        """One line for a card, log line or status bar."""
        parts = [self.headline]
        if self.model:
            parts.append(f"({self.model})")
        if self.status:
            parts.append(f"HTTP {self.status}")
        out = " ".join(parts)
        if self.detail:
            out = f"{out}: {self.detail}"
        if self.exhausted:
            out = f"{out} - still failing after repeated attempts"
        return scrub(out)

    def payload(self):
        """Event-bus shape: enough for the UI to render and to retry with."""
        return {
            "kind": self.kind,
            "headline": self.headline,
            "detail": self.detail,
            "status": self.status,
            "retry_after": self.retry_after,
            "transient": self.transient,
            "exhausted": self.exhausted,
            "model": self.model,
            "model_id": self.model_id,
        }


def _status_of(exc):
    for holder in (exc, getattr(exc, "response", None)):
        status = getattr(holder, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _retry_after(exc):
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds <= 3600 else None


def _err_detail(exc, status):
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        elif error:
            detail = str(error)
        if not detail:
            detail = str(body.get("message") or "")
    if not detail:
        detail = str(exc)
    return scrub(detail)


def _matches(text, needles):
    return any(n in text for n in needles)


def classify(exc, api_key=None, model=None, model_id=None, kind=None):
    """ModelError for whatever the SDK raised. Never raises, never leaks."""
    if isinstance(exc, ModelError):
        return exc
    if isinstance(exc, Cancelled):
        return ModelError("cancelled", "stopped before the request finished",
                         model=model, model_id=model_id)
    name = type(exc).__name__
    status = _status_of(exc)
    text = str(exc).lower()
    detail = _err_detail(exc, status)
    retry_after = _retry_after(exc)

    mapped = kind
    if mapped is None:
        mapped = _by_name(name) or _by_status(status) or _by_text(text)
    if mapped == "stream_interrupted":
        return ModelError("stream_interrupted", detail, status=status,
                          retry_after=retry_after, model=model, model_id=model_id,
                          transient=False, cause=exc)
    if mapped in HEADLINES and mapped != "unknown":
        return ModelError(mapped, detail, status=status, retry_after=retry_after,
                          model=model, model_id=model_id, cause=exc)
    return ModelError("unknown", f"{name}: {detail}", status=status,
                      retry_after=retry_after, model=model, model_id=model_id,
                      transient=False, cause=exc)


def _by_name(name):
    table = {
        "APITimeoutError": "timeout",
        "TimeoutError": "timeout",
        "ReadTimeout": "timeout",
        "ConnectTimeout": "timeout",
        "APIConnectionError": "network",
        "ConnectionError": "network",
        "ConnectionRefusedError": "network",
        "ConnectionResetError": "network",
        "ConnectError": "network",
        "RateLimitError": "rate_limited",
        "InternalServerError": "server",
        "AuthenticationError": "auth",
        "PermissionDeniedError": "auth",
        "NotFoundError": "not_found",
        "BadRequestError": "rejected",
        "UnprocessableEntityError": "unsupported",
        "JSONDecodeError": "malformed",
        "RemoteProtocolError": "network",
        "ReadError": "network",
        "CancelledError": "cancelled",
    }
    return table.get(name)


def _by_status(status):
    if status is None:
        return None
    if status in AUTH_STATUS:
        return "auth"
    if status == 429:
        return "rate_limited"
    if status in TRANSIENT_STATUS:
        return "server"
    if status == 404:
        return "not_found"
    if status == 422:
        return "unsupported"
    if status in REJECTED_STATUS:
        return "rejected"
    if 500 <= status <= 599:
        return "server"
    return None


def _by_text(text):
    if _matches(text, ("timed out", "timeout", "deadline exceeded")):
        return "timeout"
    if _matches(text, ("connection error", "connection refused", "connection reset",
                       "name or service not known", "temporary failure in name resolution",
                       "getaddrinfo", "nodename nor servname", "network is unreachable",
                       "peer closed connection", "ssl", "certificate verify",
                       "cannot connect", "unreachable", "connection aborted",
                       "broken pipe", "eof occurred", "server disconnected")):
        return "network"
    if _matches(text, ("rate limit", "too many requests", "quota")):
        return "rate_limited"
    if _matches(text, ("expecting value", "invalid json", "not valid json", "json decode")):
        return "malformed"
    return None


def no_key(model=None, model_id=None, provider=None):
    """No credential for the selected model. Permanent: configuration issue."""
    who = model or "this model"
    detail = f"add an API key for {provider}" if provider else f"add an API key for {who}"
    return ModelError("no_key", detail, model=model, model_id=model_id, transient=False)


def stream_interrupted(failure=None, partial="", api_key=None, model=None, model_id=None):
    """The response broke after it started streaming.

    The streamed text is already on screen, so retrying would duplicate it.
    """
    cause = failure
    detail = scrub(str(cause)) if cause else "connection lost mid-stream"
    if partial:
        detail = f"{detail} after {len(partial)} characters"
    return ModelError("stream_interrupted", detail, model=model, model_id=model_id,
                      partial=partial, transient=False, cause=cause)


def cancelled(model=None, model_id=None):
    return ModelError("cancelled", "stopped before the request finished",
                      model=model, model_id=model_id, transient=False)


def explain(exc, api_key=None, model=None, model_id=None):
    """One-line explanation for a log line or a status area."""
    if not isinstance(exc, ModelError):
        exc = classify(exc, api_key=api_key, model=model, model_id=model_id)
    return exc.text()


def parse_retry_after(value):
    """Retry-After is seconds here; a date form is ignored rather than guessed."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds <= 3600 else None


def json_failure(exc, api_key=None, model=None, model_id=None):
    """A 200 response whose body was not the JSON we expected."""
    detail = scrub(str(exc))
    if isinstance(exc, json.JSONDecodeError):
        detail = f"could not parse the response body ({exc.msg} at char {exc.pos})"
    return ModelError("malformed", detail, model=model, model_id=model_id, transient=True)
