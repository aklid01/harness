"""The review settings: which models review, how, and how much.

The settings live in the same non-secret config file the other roles use
(``agents.json``), so a reviewer is named by the id it already has in the
provider registry and borrows the same keyring entry. No secret is ever
written here.

A reviewer whose provider is missing, disabled or has no key is *flagged*,
not silently dropped: the settings screen says which one and why, and a run
says so too.
"""

from .. import providers

MODES = ("parallel", "sequential")

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

    The single source of the review defaults: agents.py starts its own
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


def load():
    """The review section of the role config, with defaults filled."""
    from .. import agents

    return agents.get().get("review", defaults())


def save(section):
    """Store the review section, leaving the other roles untouched."""
    from .. import agents

    config = agents.get()
    config["review"] = section
    return agents.save(config).get("review", section)


def reviewer_entries(config=None):
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


def enabled_reviewers(config=None):
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
        {**row, "reason": _reason(row["state"])}
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


def _reason(state):
    return reason(state)


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
