"""Git reads and the branch creator, for the transport's op layer.

Read-only queries plus one guarded checkout: the permission layer decides
who may call these, this module only knows how to ask git safely.
"""

import re
import subprocess

from . import gitcmd

def _git_run(root, *args, timeout=10):

    code, out = gitcmd.run(root, *args, timeout=timeout)
    return out.strip() if code == 0 else None


def git_status(root):
    if _git_run(root, "rev-parse", "--is-inside-work-tree") != "true":
        return {
            "has_git": False,
            "branch": "",
            "default_branch": "",
            "is_protected": False,
        }
    branch = _git_run(root, "branch", "--show-current") or ""
    default = ""
    head_ref = _git_run(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if head_ref and head_ref.startswith("refs/remotes/origin/"):
        default = head_ref.rsplit("/", 1)[-1]
    if not default:
        for candidate in ("main", "master"):
            if (
                _git_run(root, "show-ref", "--verify", f"refs/heads/{candidate}")
                is not None
            ):
                default = candidate
                break
    if not default:
        default = branch
    return {
        "has_git": True,
        "branch": branch,
        "default_branch": default,
        "is_protected": bool(branch) and branch == default,
    }


def git_create_branch(root, name):

    name = (name or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", name)
        or name.startswith(("-", ".", "/"))
        or ".." in name
        or name.endswith(("/", ".", ".lock"))
    ):
        return False, "invalid branch name"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "checkout", "-b", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as failure:
        return False, f"{type(failure).__name__}: {failure}"
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "git failed"
    return True, name
