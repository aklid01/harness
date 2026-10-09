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

try:
    BASE_URL = os.environ["BASE_URL"]
    API_KEY = os.environ["API_KEY"]
except KeyError as missing:
    raise SystemExit(
        f"Missing {missing}. Set it in the environment, in ./.env, or in "
        f"~/.agents/env. See .env.example. Free OpenAI-compatible "
        f"endpoints are listed in settings.json."
    ) from None

MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4-flash")

# How much room the model has, and how we spend it.
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", 128_000))
COMPACT_AT = 0.85  # compact once the prompt crosses this much of the window
COMPACT_TO = 0.35  # and cut back to this much, so it does not retrigger soon
