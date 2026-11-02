"""Tool calls that arrive as text instead of as native tool_calls.

Some endpoints, and the models behind them, have no native tool-calling
support: they accept a request that advertises tools and still answer by
writing the call out in the XML style they were fine-tuned on. This module
turns that back into a real tool call, or refuses to.

It is a compatibility layer, not a parser for anything the model feels like
writing:

* the markup must stand alone. A fenced code block, a quoted example, an
  explanation or a partial message is not a call and is never run;
* the tool name must be one this session implements and can run;
* the arguments are validated against that tool's own schema, so unknown
  arguments, missing required ones and wrong types are refused;
* a parsed call is handed to the ordinary dispatcher, so it goes through the
  same permission checks and approval prompt as a native call.

Anything refused comes back with a reason the model can read and act on.
Nothing here executes anything by itself.
"""

import json
import re
from types import SimpleNamespace

# Tag names are built from pieces so this file holds no sample markup.
OPEN = "".join(["<", "tool", "_call", ">"])
CLOSE = "".join(["</", "tool", "_call", ">"])
CLOSE_PARAM = "".join(["</", "parameter", ">"])
CLOSE_FUNCTION = "".join(["</", "function", ">"])
CALL_OPEN = "".join(["<", "function", "="])
PARAM_OPEN = "".join(["<", "parameter", "="])

CALL_PATTERN = (
    re.escape(OPEN)
    + r"\s*"
    + re.escape(CALL_OPEN)
    + r"(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)>\s*"
    r"(?P<params>(?:"
    + re.escape(PARAM_OPEN)
    + r".*?"
    + re.escape(CLOSE_PARAM)
    + r"\s*)*)"
    + re.escape(CLOSE_FUNCTION)
    + r"\s*"
    + re.escape(CLOSE)
)
STANDALONE_RE = re.compile(r"^\s*" + CALL_PATTERN + r"\s*$", re.DOTALL)
ANY_CALL_RE = re.compile(CALL_PATTERN, re.DOTALL)
PARAM_RE = re.compile(
    re.escape(PARAM_OPEN) + r"(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)>\s*"
    r"(?P<value>.*?)\s*" + re.escape(CLOSE_PARAM),
    re.DOTALL,
)

TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


class TextCall:
    """A parsed call, or the reason there isn't one."""

    def __init__(self, name=None, arguments=None, error=None):
        self.name = name
        self.arguments = arguments or {}
        self.error = error

    @property
    def ok(self):
        return self.error is None

    def as_tool_call(self, call_id="text-1"):
        """The same shape a native tool_call arrives in."""
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name=self.name, arguments=json.dumps(self.arguments)
            ),
        )

    def reason(self):
        return self.error or ""

    def __repr__(self):  # pragma: no cover - debugging aid
        return "TextCall(name=%r, arguments=%r, error=%r)" % (
            self.name,
            self.arguments,
            self.error,
        )


def _schema_for(name, schemas):
    for schema in schemas or []:
        function = schema.get("function") or {}
        if function.get("name") == name:
            return function.get("parameters") or {}
    return None


def _available():
    from . import tools

    return tools.available_tool_names()


def _coerce(value, spec):
    """Text parameters are always strings; make them the schema's type."""
    kind = (spec or {}).get("type")
    if kind == "boolean":
        return value.strip().lower() in ("1", "true", "yes", "on")
    try:
        if kind == "integer":
            return int(value.strip())
        if kind == "number":
            return float(value.strip())
    except ValueError:
        return value
    return value


def _validate(name, arguments, schema, available):
    if schema is None:
        return "'%s' is not a tool this session implements" % name
    if available is not None and name not in available:
        return "'%s' is not available in this session" % name
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    # An empty string is a value, not a missing argument (mirrors tools._validate_args).
    missing = [key for key in required if arguments.get(key) is None]
    if missing:
        return "%s is missing required argument(s): %s" % (name, ", ".join(missing))
    unknown = [key for key in arguments if key not in properties]
    if unknown:
        return "%s got argument(s) it does not accept: %s" % (name, ", ".join(unknown))
    for key, value in arguments.items():
        kind = (properties.get(key) or {}).get("type")
        expected = TYPES.get(kind)
        if not expected or isinstance(value, expected):
            continue
        return "%s argument '%s' should be %s" % (name, key, kind)
    return None


def looks_like_call(text):
    """Cheap pre-check so the caller does not parse every message."""
    return (
        isinstance(text, str)
        and OPEN in text
        and CLOSE_FUNCTION in text
        and CALL_OPEN in text
    )


def parse(text, schemas=None, available=None):
    """Parse a standalone text tool call. Never raises, never executes."""
    if not isinstance(text, str):
        return TextCall(error="nothing to parse")
    body = text.strip()
    if not body:
        return TextCall(error="empty message")
    if "```" in body:
        return TextCall(error="the markup is inside a code fence, so it is not a call")
    match = STANDALONE_RE.match(body)
    if not match:
        return TextCall(error="the call markup is not a well-formed standalone call")
    if available is None:
        available = _available()

    name = match.group("name")
    raw = dict(PARAM_RE.findall(match.group("params") or ""))
    if not raw and (match.group("params") or "").strip():
        return TextCall(error="the call arguments could not be read")

    schema = _schema_for(name, schemas)
    arguments = {
        key: _coerce(value, (schema or {}).get("properties", {}).get(key, {}))
        for key, value in raw.items()
    }
    problem = _validate(name, arguments, schema, available)
    if problem:
        return TextCall(name=name, arguments=arguments, error=problem)
    return TextCall(name=name, arguments=arguments)


def parse_with_narration(text, schemas=None, available=None):
    """Parse a call that has prose in front of it, and return the prose too.

    Only for models known to emit this format, and only when the call itself
    is well formed. The narration comes back so it can be shown to the user
    rather than swallowed.
    """
    if not looks_like_call(text):
        return TextCall(error="no call markup"), ""
    if available is None:
        available = _available()
    match = ANY_CALL_RE.search(text)
    if not match:
        return TextCall(error="the call markup is not well formed"), ""
    if "```" in text[: match.start()] or "```" in text[match.end() :]:
        return TextCall(
            error="the markup is inside a code fence, so it is not a call"
        ), ""
    call = parse(match.group(0), schemas, available)
    if not call.ok:
        return call, ""
    around = " ".join((text[: match.start()] + " " + text[match.end() :]).split())
    return call, around


def describe(call):
    """One line for the user: what the text call asks for."""
    if not call.ok:
        return call.error or "unrecognized tool call"
    summary = ", ".join(
        "%s=%r" % (key, value) for key, value in list(call.arguments.items())[:3]
    )
    return "%s(%s)" % (call.name, summary)
