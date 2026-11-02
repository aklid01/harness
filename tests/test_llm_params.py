"""Reasoning effort / temperature reach the provider call only when set."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import llm, providers


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def canned():
    usage = SimpleNamespace(
        prompt_tokens=1,
        completion_tokens=1,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=None),
        prompt_tokens_details=SimpleNamespace(cached_tokens=None),
    )
    message = SimpleNamespace(content="ok", tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return canned()


class ParamCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(
            providers, "PROVIDERS_FILE", Path(self.tmp.name) / "providers.json"
        ).start()
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.completions = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        mock.patch.object(llm, "get_client", return_value=client).start()
        self.addCleanup(mock.patch.stopall)

    def use_provider(self, **extra):
        entry = {
            "id": "p",
            "name": "P",
            "base_url": "https://x/v1",
            "model": "m",
            "headers": {},
            "disabled": False,
        }
        entry.update(extra)
        providers.save_provider(entry)
        providers.set_active("p")

    def test_effort_and_temperature_forwarded(self):
        self.use_provider(reasoning_effort="xhigh", temperature="0.2")
        llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual(self.completions.kwargs.get("reasoning_effort"), "xhigh")
        self.assertEqual(self.completions.kwargs.get("temperature"), 0.2)

    def test_defaults_omitted(self):
        self.use_provider()
        llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertNotIn("reasoning_effort", self.completions.kwargs)
        self.assertNotIn("temperature", self.completions.kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
