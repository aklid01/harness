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
from .coerce import as_bool, as_int, as_text
from .review.config import (
    AUTO_RUN,
    clean_section as _clean_review,
    defaults as _review_defaults,
)

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


def _clean_role(raw, approve_default=None):
    raw = raw if isinstance(raw, dict) else {}
    clean = {
        "enabled": as_bool(raw.get("enabled")),
        "model_id": as_text(raw.get("model_id")),
        "instructions": as_text(raw.get("instructions")),
    }
    if approve_default is not None:
        clean["approve"] = as_bool(raw.get("approve"), default=approve_default)
    if "intensity" in raw or "max_rounds" in raw:
        intensity = as_text(raw.get("intensity")) or DEFAULT_INTENSITY
        clean["intensity"] = (
            intensity if intensity in INTENSITIES else DEFAULT_INTENSITY
        )
        clean["max_rounds"] = as_int(
            raw.get("max_rounds"), DEFAULT_MAX_ROUNDS, 1, MAX_ROUNDS_LIMIT
        )
    return clean


def normalise(raw) -> dict:
    """A full config with defaults filled, from whatever is on disk."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"version": FORMAT_VERSION}
    # The review role is not in DEFAULTS (its defaults live in
    # review/config), so it is cleaned separately - it must still land in
    # every normalised config.
    out["review"] = _clean_review(raw.get("review"))
    for role, default in DEFAULTS.items():
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


def get() -> dict:
    """The current config, defaults filled. Never raises."""
    return copy.deepcopy(_cached())


def save(config) -> dict:
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


def review_settings() -> dict:
    """The saved review settings, defaults filled.

    The review section lives in this role config, so the accessor lives here
    too: review/config stays a leaf that never imports back into its parent.
    """
    section = get().get("review")
    return section if section is not None else copy.deepcopy(_review_defaults())


def save_review_section(section) -> dict:
    """Store the review section, leaving the other roles untouched."""
    config = get()
    config["review"] = section
    return save(config).get("review", section)


def reset_cache() -> None:
    """Forget the cached file. Used by tests that point _file at a temp dir."""
    _CACHE.clear()


def resolve_model(role_config, default_id) -> str | None:
    """The model id a role should use: its own when set, else the session's.

    A disabled role, or one with no model chosen, answers None so the caller
    keeps the existing default agent instead of routing anywhere new.
    """
    if not role_config.get("enabled"):
        return None
    return role_config.get("model_id") or default_id


def plan_active(config) -> bool:
    """True when a dedicated planning pass should run before building."""
    return bool(config["plan"].get("enabled"))


def plan_wants_approval(config) -> bool:
    """True when the plan should pause for the user before building.

    Defaults to false, so a plan agent that is off (or one that left this off)
    behaves exactly as before: plan then build automatically.
    """
    return bool(config["plan"].get("enabled")) and bool(config["plan"].get("approve"))


def oversight_enabled(config) -> bool:
    return bool(config["oversight"].get("enabled"))


def review_enabled(config) -> bool:
    """True when the multi-model review role is switched on."""
    review = config.get("review") or {}
    return bool(review.get("enabled"))


def review_auto_run(config) -> str:
    """How a review starts after a run that edited files: off, offer or start.

    A review that starts on its own costs money, so it is never the default.
    "offer" shows the card with a button; "start" runs the review by itself.
    """
    review = config.get("review") or {}
    if not review.get("enabled"):
        return "off"
    auto = review.get("auto_run") or "off"
    return auto if auto in AUTO_RUN else "off"
