"""The review settings: which models review, how, and how much.

The settings live in the same non-secret config file the other roles use
(``agents.json``), so a reviewer is named by the id it already has in the
provider registry and borrows the same keyring entry. No secret is ever
written here.

A reviewer whose provider is missing, disabled or has no key is *flagged*,
not silently dropped: the settings screen says which one and why, and a run
says so too.
"""

from typing import TypedDict

from .. import providers
from . import findings as findings_mod

MODES = ("parallel", "sequential")

# How a review may start after a run that edited files.
AUTO_RUN = ("off", "offer", "start")


class ReviewerEntry(TypedDict):
    """One configured reviewer, as the engine and the settings screen see it."""

    model_id: str
    label: str
    enabled: bool
    focus: str

DEFAULT_MAX_REVIEWERS = 4
DEFAULT_MAX_FILES = 40
DEFAULT_MAX_DIFF_BYTES = 200_000
DEFAULT_MAX_FILE_BYTES = 60_000
DEFAULT_MAX_TOKENS = 150_000

# Statuses a reviewer model can be in, from the settings' point of view.
OK = "ok"
MISSING = "missing"
DISABLED = "disabled"
NO_KEY = "no-key"


def defaults():
    """A fresh review section, with nothing enabled.

    The single source of the review defaults: agent_roles.py starts its own
    validation from here, so the numbers exist exactly once.
    """
    from .scope import DEFAULT_EXCLUSIONS

    return {
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


def clean_section(raw):
    """One review section, from whatever the saved role config holds.

    The single cleaner for the review settings: agent_roles.py hands the raw
    section over untouched, so the option set, the limits and their
    bounds live here and nowhere else.
    """
    raw = raw if isinstance(raw, dict) else {}
    clean = defaults()
    clean["enabled"] = _as_bool(raw.get("enabled"))
    auto = raw.get("auto_run")
    if isinstance(auto, bool):
        auto = "start" if auto else "off"
    auto = _as_text(auto).lower() or "off"
    clean["auto_run"] = auto if auto in AUTO_RUN else "off"
    mode = _as_text(raw.get("mode")) or "parallel"
    clean["mode"] = mode if mode in MODES else "parallel"

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
        threshold if threshold in findings_mod.SEVERITIES else "low"
    )
    clean["full_context"] = _as_bool(raw.get("full_context"))

    listed = raw.get("exclusions")
    if isinstance(listed, list):
        clean["exclusions"] = [_as_text(x) for x in listed if _as_text(x)]

    clean["prompt"] = _as_text(raw.get("prompt"))[:8000]
    return clean


def load():
    """The review section of the role config, with defaults filled."""
    from .. import agent_roles

    return agent_roles.get().get("review", defaults())


def save(section):
    """Store the review section, leaving the other roles untouched."""
    from .. import agent_roles

    config = agent_roles.get()
    config["review"] = section
    return agent_roles.save(config).get("review", section)


def reviewer_entries(config=None) -> list[ReviewerEntry]:
    """The reviewers a user configured, enabled or not."""
    config = config or load()
    entries = []
    for raw in config.get("reviewers") or []:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("model_id") or "").strip()
        if not model_id:
            continue
        entries.append(
            {
                "model_id": model_id,
                "label": str(raw.get("label") or "").strip(),
                "enabled": bool(raw.get("enabled", True)),
                "focus": str(raw.get("focus") or "").strip(),
            }
        )
    return entries


def enabled_reviewers(config=None) -> list[ReviewerEntry]:
    """The reviewers that would run, in the order the user set them."""
    entries = [e for e in reviewer_entries(config) if e["enabled"]]
    limit = int((config or load()).get("max_reviewers") or DEFAULT_MAX_REVIEWERS)
    return entries[: max(0, limit)]


def model_state(model_id):
    """How usable a reviewer model is right now."""
    try:
        status = providers.provider_status(model_id)
    except Exception:  # noqa: BLE001 - a broken registry must not crash settings
        status = None
    if status is None:
        try:
            entry = providers.get_model(model_id)
        except Exception:  # noqa: BLE001
            entry = None
        if not entry:
            return MISSING
        return NO_KEY
    if status.get("disabled"):
        return DISABLED
    if status.get("key") in ("missing", None):
        return NO_KEY
    return OK


def is_usable(model_id):
    return model_state(model_id) == OK


def reviewer_states(config=None):
    """Every configured reviewer with its state, for the settings screen."""
    rows = []
    for entry in reviewer_entries(config):
        state = model_state(entry["model_id"])
        rows.append({**entry, "state": state, "usable": state == OK})
    return rows


def usable_reviewers(config=None):
    """The enabled reviewers whose provider can actually be reached."""
    return [row for row in reviewer_states(config) if row["enabled"] and row["usable"]]


def unusable_reviewers(config=None):
    """The enabled reviewers that cannot run, with the reason."""
    return [
        {**row, "reason": reason(row["state"])}
        for row in reviewer_states(config)
        if row["enabled"] and not row["usable"]
    ]


def reason(state):
    """Why a reviewer model cannot be used, in plain words."""
    return {
        MISSING: "that model is not in the provider registry any more",
        DISABLED: "that provider or model is switched off",
        NO_KEY: "that provider has no API key",
    }.get(state, "that model cannot be reached")




def estimated_tokens(config=None, chars=0):
    """A rough token estimate for the change about to be sent.

    There is no pricing table in the engine, so the estimate is in tokens -
    the unit the budget is actually enforced in - and the settings screen
    shows it before a multi-model run so the spend is never a surprise.
    """
    config = config or load()
    limit = int(config.get("max_tokens") or DEFAULT_MAX_TOKENS)
    per_model = int(chars / 4)
    return {
        "tokens_per_model": per_model,
        "tokens_total": per_model * max(1, len(enabled_reviewers(config))),
        "limit": limit,
        "over_limit": per_model * max(1, len(enabled_reviewers(config))) > limit,
    }
