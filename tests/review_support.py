"""Shared fixtures for the multi-model review tests.

Every review test needs the same three things: a workspace of its own, an
isolated provider registry, and - for the scope tests - a git repository it can
shape. That lives here so each test file can stay about the review itself.
"""

import base64
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import agents
from harness import paths as paths_mod
from harness import providers


class FakeKeyring:
    """A keyring in memory. No credential ever leaves the test."""

    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def git(root, *args):
    """Run git in ``root`` and fail loudly when it does not work."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def init_repo(root):
    """A repository with a deterministic identity, so commits always work."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "review@example.com")
    git(root, "config", "user.name", "Review Test")
    git(root, "config", "commit.gpgsign", "false")
    return root


def commit_all(root, message="change"):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def b64(text):
    """The checkpoint journal keeps file bytes as base64."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeLLM:
    """Stands in for the model call, and records what the reviewer was sent.

    ``replies`` maps a model id to the content it answers with (a list means
    one answer per call, the last one repeating). ``errors`` maps a model id
    to the exception it raises. A ``barrier`` makes every call wait for the
    others, which is how a test proves the reviewers really run at the same
    time.
    """

    def __init__(self, replies=None, errors=None, barrier=None):
        self.replies = replies or {}
        self.errors = errors or {}
        self.barrier = barrier
        self.on_call = None
        self.calls = []
        self.spans = []
        self._lock = threading.Lock()
        self._counts = {}

    def __call__(
        self, messages, tools=None, on_delta=None, on_reasoning=None, cancel=None
    ):
        model_id = providers.get_active_id()
        started = time.monotonic()
        with self._lock:
            self.calls.append(
                {
                    "model_id": model_id,
                    "messages": messages,
                    "tools": tools,
                    "user": str(messages[-1]["content"]) if messages else "",
                    "system": str(messages[0]["content"]) if messages else "",
                }
            )
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=15)
            if self.on_call is not None:
                self.on_call(model_id, cancel)
            failure = self.errors.get(model_id)
            if failure is not None:
                raise failure
            content = self._reply(model_id)
        finally:
            with self._lock:
                self.spans.append((model_id, started, time.monotonic()))
        return SimpleNamespace(content=content), {
            "prompt_tokens": 12,
            "completion_tokens": 7,
        }

    def _reply(self, model_id):
        value = self.replies.get(model_id, '{"findings": []}')
        if not isinstance(value, list):
            return value
        with self._lock:
            index = self._counts.get(model_id, 0)
            self._counts[model_id] = index + 1
        return value[min(index, len(value) - 1)]

    def calls_for(self, model_id):
        return [call for call in self.calls if call["model_id"] == model_id]

    def overlap(self):
        """True when two calls were inside the model at the same time."""
        if len(self.spans) < 2:
            return False
        starts = [span[1] for span in self.spans]
        ends = [span[2] for span in self.spans]
        return max(starts) < min(ends)


class ReviewCase(unittest.TestCase):
    """A workspace, an app data dir and a session store, all in a temp dir."""

    session_id = "sess-1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        self.appdata = Path(self.tmp.name) / "appdata"
        self.appdata.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=self.appdata)
        self.addCleanup(paths_mod.reset)

        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        # Stop git from walking up past the temp dir: a workspace that must
        # not be a repository stays that one, whatever the machine looks like.
        os.environ["GIT_CEILING_DIRECTORIES"] = str(Path(self.tmp.name))
        self.addCleanup(lambda: os.environ.pop("GIT_CEILING_DIRECTORIES", None))

        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)
        agents.reset_cache()
        self.addCleanup(agents.reset_cache)

    def write(self, name, text):
        """Write a file in the workspace and return its path."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def record_change(self, name, before, after, session_id=None):
        """Record an edit in the checkpoint journal, the way the engine does."""
        from harness import checkpoints

        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8")
        checkpoints.record(
            session_id or self.session_id, 1, str(path), b64(before), b64(after)
        )
        return path

    def register_models(self, *model_ids):
        """Put models in the provider registry, with a key, so they can run."""
        from harness import providers

        providers.save_group(
            {"id": "grp-1", "name": "Mock", "base_url": "http://127.0.0.1:1/v1"}
        )
        for model_id in model_ids:
            providers.save_model(
                {"id": model_id, "name": model_id.upper(), "model": f"{model_id}-model"},
                "grp-1",
            )
        providers.set_active(model_ids[0])
        providers.set_key("grp-1", "sk-test-key")
        return list(model_ids)

    def settings(self, **overrides):
        """Review settings with the shipped defaults, plus any overrides."""
        from harness.review import config as config_mod
        from harness.review import scope as scope_mod

        base = dict(config_mod.defaults())
        base["exclusions"] = list(scope_mod.DEFAULT_EXCLUSIONS)
        base.update(overrides)
        return base
