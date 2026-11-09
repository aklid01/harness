"""Coercion helpers for config values read out of JSON settings files.

A leaf module on purpose: the role config (agent_roles) and the review
settings (review/config) both need these. The section vocabulary lives in
review/config and flows one way, into agent_roles; this module stays below
both of them.
"""


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def as_text(value):
    return value.strip() if isinstance(value, str) else ""


def as_int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))
