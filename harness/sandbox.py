"""Kernel-enforced limits on what bash can touch.

One policy - read anything, write only inside the project, no network - and a
different enforcement mechanism per OS. The idea ports; the mechanism never does.

The macOS profile is adapted from openai/codex (Apache-2.0), simplified.
https://github.com/openai/codex
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths


def _project():
    return paths.project_root()


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
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(root),
            str(root),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-net",
            "--die-with-parent",
            "/bin/sh",
            "-c",
            command,
        ]

    return None


_BASH_HINTS = (
    "C:/Program Files/Git/bin/bash.exe",
    "C:/Program Files (x86)/Git/bin/bash.exe",
    os.path.expandvars(r"%LOCALAPPDATA%/Programs/Git/bin/bash.exe"),
)
_BASH = None


def _find_bash():
    for cand in _BASH_HINTS:
        if os.path.exists(cand):
            return cand
    found = shutil.which("bash")
    if found and "windowsapps" not in found.lower():
        return found
    return None


def _posix_pathify(command):
    """Rewrite Windows paths so the POSIX shell can read them.

    Only path spellings change: a drive path ``D:\\a\\b`` or ``D:/a/b`` becomes
    the MSYS mount ``/d/a/b`` and the remaining backslashes become slashes.
    Operators, redirections, pipelines and quoting are left exactly as written,
    so a command is classified and executed on the same shape. (The one casualty
    is a literal ``\\n`` inside a printf - rarely needed here.)
    """
    text = re.sub(
        r"([A-Za-z]):[\\/]", lambda m: "/" + m.group(1).lower() + "/", command
    )
    return text.replace("\\", "/")


def _run_bash(command, timeout, cwd):
    argv = [_BASH, "-c", _posix_pathify(command)]
    try:
        return subprocess.run(
            argv, shell=False, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except NotADirectoryError:
        return subprocess.run(
            argv, shell=False, capture_output=True, text=True, timeout=timeout
        )


def run(command, timeout=60):
    """Run a command in the workspace, sandboxed when the OS lets us."""
    global _BASH
    root = _project()
    if sys.platform.startswith("win"):
        if _BASH is None:
            _BASH = _find_bash() or ""
        if _BASH:
            return _run_bash(command, timeout, str(root))
    sandboxed = wrap(command)
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
        return subprocess.run(
            sandboxed or command,
            shell=sandboxed is None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def name():
    if sys.platform == "darwin":
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap"
    if sys.platform.startswith("win") and (_BASH or _find_bash()):
        return "bash"
    return "none"
