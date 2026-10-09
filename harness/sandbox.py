"""Kernel-enforced limits on what bash can touch.

One policy - read anything, write only inside the project, no network - and a
different enforcement mechanism per OS. The idea ports; the mechanism never does.

The macOS profile is adapted from openai/codex (Apache-2.0), simplified.
https://github.com/openai/codex
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths

PROJECT = Path.cwd().resolve()


def _project():
    if paths.is_initialized():
        return paths.project_root()
    return PROJECT


PROFILE_TEMPLATE = """(version 1)
(deny default)
(allow process-exec process-fork signal)
(allow file-read*)
(allow sysctl-read)
(deny network*)
(allow file-write* (subpath "{root}") (literal "/dev/null"))
(deny file-write* (subpath "{root}/.git"))
"""


def wrap(command):
    """Wrap a shell command in an OS sandbox. None means we have no sandbox."""
    root = _project()
    if sys.platform == "darwin":
        profile = Path(tempfile.gettempdir()) / "harness.sb"
        profile.write_text(PROFILE_TEMPLATE.format(root=root))
        return ["sandbox-exec", "-f", str(profile), "/bin/sh", "-c", command]

    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return [
            "bwrap",
            "--ro-bind", "/", "/",
            "--bind", str(root), str(root),
            "--dev", "/dev", "--proc", "/proc",
            "--unshare-net", "--die-with-parent",
            "/bin/sh", "-c", command,
        ]

    return None  # Windows, or Linux without bubblewrap


def name():
    if sys.platform == "darwin":
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap"
    return "none"


def run(command, timeout=60):
    """Run a command, sandboxed when the OS lets us."""
    sandboxed = wrap(command)
    root = _project()
    try:
        return subprocess.run(
            sandboxed or command,
            shell=sandboxed is None,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
        )
    except NotADirectoryError:
        # The configured root vanished; run from the process cwd rather than fail.
        return subprocess.run(sandboxed or command, shell=sandboxed is None, capture_output=True, text=True, timeout=timeout)
