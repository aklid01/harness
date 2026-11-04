"""Settings: real environment variables first, then .env, then ~/.agents/env."""

import os
from pathlib import Path


def _load_env_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def reload():
    from . import paths

    for candidate in paths.env_files():
        _load_env_file(candidate)


reload()

# Optional: the keyring is the primary credential source. These only matter
# for the legacy no-registry flow, and a missing key surfaces at request time
# as a no_key error instead of killing the engine at import.
BASE_URL = os.environ.get("BASE_URL")
API_KEY = os.environ.get("API_KEY")

MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4-flash")

# How much room the model has, and how we spend it. A malformed value falls
# back to the default at first use rather than killing the engine at import -
# the same no-key-at-request-time posture as the credentials above.
def _context_window():
    try:
        return int(os.environ.get("CONTEXT_WINDOW", 128_000))
    except ValueError:
        return 128_000


CONTEXT_WINDOW = _context_window()
COMPACT_AT = 0.85  # compact once the prompt crosses this much of the window
COMPACT_TO = 0.35  # and cut back to this much, so it does not retrigger soon
