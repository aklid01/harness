"""Optional role agents and decision oversight: persistent configuration.

The harness already lets a user pick one model per session and point it at
shared project instructions in AGENTS.md. This module adds an *optional* layer
on top of that: dedicated models for planning and for building, and a separate
reviewer model for decision oversight. It never stores a credential - each
model is named by the id it already has in providers.json, so routing to it
(see providers.use) reuses the same keyring entry and provider config the
session model uses. A user who configures nothing gets exactly today's
behaviour.

The file is small and app-global (one per app data dir), read fresh on change
and cached by mtime so every session in the process sees the same settings as
soon as they are saved.
"""

import copy
import json
from pathlib import Path

from . import paths

FORMAT_VERSION = 1

INTENSITIES = ("low", "medium", "high")
DEFAULT_INTENSITY = "medium"
DEFAULT_MAX_ROUNDS = 2
MAX_ROUNDS_LIMIT = 6

# Roles are keyed by name so callers can index config["plan"], config["build"]
# and config["oversight"] uniformly, while oversight carries its own extra
# fields for the review loop.
_ROLE_FIELDS = ("enabled", "model_id", "instructions")

DEFAULTS = {
    "plan": {"enabled": False, "model_id": "", "instructions": "", "approve": False},
    "build": {"enabled": False, "model_id": "", "instructions": ""},
    "oversight": {
        "enabled": False,
        "model_id": "",
        "instructions": "",
        "intensity": DEFAULT_INTENSITY,
        "max_rounds": DEFAULT_MAX_ROUNDS,
    },
    # The multi-model review role is not here: its defaults are the review
    # module's own constants, so the numbers exist in exactly one place (see
    # _clean_review). It differs from the other roles in one way: it holds a
    # *list* of models, not one, because the whole point is that several
    # independent reviewers look at the same change.
}


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_text(value):
    return value.strip() if isinstance(value, str) else ""


def _as_int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _clean_role(raw, approve_default=None):
    raw = raw if isinstance(raw, dict) else {}
    clean = {
        "enabled": _as_bool(raw.get("enabled")),
        "model_id": _as_text(raw.get("model_id")),
        "instructions": _as_text(raw.get("instructions")),
    }
    if approve_default is not None:
        clean["approve"] = _as_bool(raw.get("approve"), default=approve_default)
    if "intensity" in raw or "max_rounds" in raw:
        intensity = _as_text(raw.get("intensity")) or DEFAULT_INTENSITY
        clean["intensity"] = (
            intensity if intensity in INTENSITIES else DEFAULT_INTENSITY
        )
        clean["max_rounds"] = _as_int(
            raw.get("max_rounds"), DEFAULT_MAX_ROUNDS, 1, MAX_ROUNDS_LIMIT
        )
    return clean


def _clean_review(raw):
    """The review role: a list of reviewers, and the limits around them.

    The defaults come from review/config's constants so the limits are
    owned there, not restated here.
    """
    from .review.config import (
        DEFAULT_MAX_DIFF_BYTES,
        DEFAULT_MAX_FILE_BYTES,
        DEFAULT_MAX_FILES,
        DEFAULT_MAX_REVIEWERS,
        DEFAULT_MAX_TOKENS,
    )
    from .review.scope import DEFAULT_EXCLUSIONS

    raw = raw if isinstance(raw, dict) else {}
    clean = {
        "enabled": False,
        "auto_run": "off",
        "mode": "parallel",
        "reviewers": [],
        "max_reviewers": DEFAULT_MAX_REVIEWERS,
        "max_files": DEFAULT_MAX_FILES,
        "max_diff_bytes": DEFAULT_MAX_DIFF_BYTES,
        "max_file_bytes": DEFAULT_MAX_FILE_BYTES,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "severity_threshold": "low",
        "full_context": False,
        "exclusions": list(DEFAULT_EXCLUSIONS),
        "prompt": "",
    }
    clean["enabled"] = _as_bool(raw.get("enabled"))
    auto = raw.get("auto_run")
    if isinstance(auto, bool):
        auto = "start" if auto else "off"
    auto = _as_text(auto).lower() or "off"
    clean["auto_run"] = auto if auto in ("off", "offer", "start") else "off"
    mode = _as_text(raw.get("mode")) or "parallel"
    clean["mode"] = mode if mode in ("parallel", "sequential") else "parallel"

    reviewers = []
    for item in raw.get("reviewers") or []:
        if not isinstance(item, dict):
            continue
        model_id = _as_text(item.get("model_id"))
        if not model_id:
            continue
        reviewers.append(
            {
                "model_id": model_id,
                "label": _as_text(item.get("label")),
                "enabled": _as_bool(item.get("enabled", True), default=True),
                "focus": _as_text(item.get("focus")),
            }
        )
    clean["reviewers"] = reviewers

    clean["max_reviewers"] = _as_int(
        raw.get("max_reviewers"), DEFAULT_MAX_REVIEWERS, 1, 16
    )
    clean["max_files"] = _as_int(raw.get("max_files"), DEFAULT_MAX_FILES, 1, 500)
    clean["max_diff_bytes"] = _as_int(
        raw.get("max_diff_bytes"), DEFAULT_MAX_DIFF_BYTES, 1_000, 20_000_000
    )
    clean["max_file_bytes"] = _as_int(
        raw.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES, 1_000, 5_000_000
    )
    clean["max_tokens"] = _as_int(
        raw.get("max_tokens"), DEFAULT_MAX_TOKENS, 1_000, 10_000_000
    )
    threshold = _as_text(raw.get("severity_threshold")) or "low"
    clean["severity_threshold"] = (
        threshold if threshold in ("blocker", "high", "medium", "low", "nit") else "low"
    )
    clean["full_context"] = _as_bool(raw.get("full_context"))

    listed = raw.get("exclusions")
    if isinstance(listed, list):
        clean["exclusions"] = [_as_text(x) for x in listed if _as_text(x)]

    clean["prompt"] = _as_text(raw.get("prompt"))[:8000]
    return clean


def normalise(raw):
    """A full config with defaults filled, from whatever is on disk."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"version": FORMAT_VERSION}
    for role, default in DEFAULTS.items():
        if role == "review":
            out[role] = _clean_review(raw.get(role))
            continue
        merged = dict(default)
        merged.update(
            _clean_role(
                raw.get(role), approve_default=False if role == "plan" else None
            )
        )
        out[role] = merged
    return out


def _file():
    if paths.is_initialized():
        return paths.agents_file()
    return Path.home() / ".agents" / "agents.json"


_CACHE = {}
_MISSING = object()


def _cached():
    try:
        stamp = _file().stat().st_mtime_ns
    except OSError:
        stamp = None
    if _CACHE.get("stamp", _MISSING) != stamp:
        _CACHE["stamp"] = stamp
        try:
            raw = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        _CACHE["data"] = normalise(raw)
    return _CACHE["data"]


def get():
    """The current config, defaults filled. Never raises."""
    return copy.deepcopy(_cached())


def save(config):
    """Persist a normalised config and return what was stored."""
    body = normalise(config)
    body["version"] = FORMAT_VERSION
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    _CACHE["stamp"] = _MISSING  # force a re-read on the next get()
    return copy.deepcopy(body)


def reset_cache():
    """Forget the cached file. Used by tests that point _file at a temp dir."""
    _CACHE.clear()


def resolve_model(role_config, default_id):
    """The model id a role should use: its own when set, else the session's.

    A disabled role, or one with no model chosen, answers None so the caller
    keeps the existing default agent instead of routing anywhere new.
    """
    if not role_config.get("enabled"):
        return None
    return role_config.get("model_id") or default_id


def plan_active(config):
    """True when a dedicated planning pass should run before building."""
    return bool(config["plan"].get("enabled"))


def plan_wants_approval(config):
    """True when the plan should pause for the user before building.

    Defaults to false, so a plan agent that is off (or one that left this off)
    behaves exactly as before: plan then build automatically.
    """
    return bool(config["plan"].get("enabled")) and bool(config["plan"].get("approve"))


def oversight_enabled(config):
    return bool(config["oversight"].get("enabled"))


def review_enabled(config):
    """True when the multi-model review role is switched on."""
    review = config.get("review") or {}
    return bool(review.get("enabled"))


def review_auto_run(config):
    """How a review starts after a run that edited files: off, offer or start.

    A review that starts on its own costs money, so it is never the default.
    "offer" shows the card with a button; "start" runs the review by itself.
    """
    review = config.get("review") or {}
    if not review.get("enabled"):
        return "off"
    auto = review.get("auto_run") or "off"
    return auto if auto in ("offer", "start") else "off"
