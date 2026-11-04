"""One git runner for the engine: argv list, never a shell.

Every git call site goes through run() so a path with a space or a
non-ascii character survives the trip, and there is exactly one failure
shape to handle: (returncode, stdout) with stdout replaced-decoded utf-8.
"""

import subprocess


def run(root, *args, timeout=10, config=()):
    """Run git in ``root``; (returncode, stdout).

    ``config`` holds leading ``-c`` settings. A missing git binary or a
    timeout is (1, "") - the callers treat it as "git answered no".
    """
    try:
        result = subprocess.run(
            ["git", *config, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout or ""
