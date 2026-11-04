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
from ..coerce import as_bool, as_int, as_text
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


def clean_section(raw):
    """One review section, from whatever the saved role config holds.

    The single cleaner for the review settings: agent_roles.py hands the raw
    section over untouched, so the option set, the limits and their
    bounds live here and nowhere else.
    """
    raw = raw if isinstance(raw, dict) else {}
    clean = defaults()
    clean["enabled"] = as_bool(raw.get("enabled"))
    auto = raw.get("auto_run")
    if isinstance(auto, bool):
        auto = "start" if auto else "off"
    auto = as_text(auto).lower() or "off"
    clean["auto_run"] = auto if auto in AUTO_RUN else "off"
    mode = as_text(raw.get("mode")) or "parallel"
    clean["mode"] = mode if mode in MODES else "parallel"

    reviewers = []
    for item in raw.get("reviewers") or []:
        if not isinstance(item, dict):
            continue
        model_id = as_text(item.get("model_id"))
        if not model_id:
            continue
        reviewers.append(
            {
                "model_id": model_id,
                "label": as_text(item.get("label")),
                "enabled": as_bool(item.get("enabled", True), default=True),
                "focus": as_text(item.get("focus")),
            }
        )
    clean["reviewers"] = reviewers

    clean["max_reviewers"] = as_int(
        raw.get("max_reviewers"), DEFAULT_MAX_REVIEWERS, 1, 16
    )
    clean["max_files"] = as_int(raw.get("max_files"), DEFAULT_MAX_FILES, 1, 500)
    clean["max_diff_bytes"] = as_int(
        raw.get("max_diff_bytes"), DEFAULT_MAX_DIFF_BYTES, 1_000, 20_000_000
    )
    clean["max_file_bytes"] = as_int(
        raw.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES, 1_000, 5_000_000
    )
    clean["max_tokens"] = as_int(
        raw.get("max_tokens"), DEFAULT_MAX_TOKENS, 1_000, 10_000_000
    )
    threshold = as_text(raw.get("severity_threshold")) or "low"
    clean["severity_threshold"] = (
        threshold if threshold in findings_mod.SEVERITIES else "low"
    )
    clean["full_context"] = as_bool(raw.get("full_context"))

    listed = raw.get("exclusions")
    if isinstance(listed, list):
        clean["exclusions"] = [as_text(x) for x in listed if as_text(x)]

    clean["prompt"] = as_text(raw.get("prompt"))[:8000]
    return clean


def reviewer_entries(config) -> list[ReviewerEntry]:
    """The reviewers a user configured, enabled or not."""
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


def enabled_reviewers(config) -> list[ReviewerEntry]:
    """The reviewers that would run, in the order the user set them."""
    entries = [e for e in reviewer_entries(config) if e["enabled"]]
    limit = int(config.get("max_reviewers") or DEFAULT_MAX_REVIEWERS)
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


def reason(state):
    """Why a reviewer model cannot be used, in plain words."""
    return {
        MISSING: "that model is not in the provider registry any more",
        DISABLED: "that provider or model is switched off",
        NO_KEY: "that provider has no API key",
    }.get(state, "that model cannot be reached")


def estimated_tokens(config, chars=0):
    """A rough token estimate for the change about to be sent.

    There is no pricing table in the engine, so the estimate is in tokens -
    the unit the budget is actually enforced in - and the settings screen
    shows it before a multi-model run so the spend is never a surprise.
    """
    limit = int(config.get("max_tokens") or DEFAULT_MAX_TOKENS)
    per_model = int(chars / 4)
    reviewers = len(enabled_reviewers(config))
    return {
        "tokens_per_model": per_model,
        "tokens_total": per_model * max(1, reviewers),
        "limit": limit,
        "over_limit": per_model * max(1, reviewers) > limit,
    }
