"""Provider store: CRUD, masking, legacy fallback, keyring resolution."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import paths, providers


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "app",
        )
        self.addCleanup(paths.reset)
        self.fake = FakeKeyring()
        mock.patch.dict("sys.modules", {"keyring": self.fake}).start()
        self.addCleanup(mock.patch.stopall)

    def entry(self, pid="p1"):
        return {
            "id": pid,
            "name": "P1",
            "base_url": "https://x/v1",
            "model": "m",
            "headers": {},
            "disabled": False,
        }

    def test_crud_and_active(self):
        self.assertEqual(providers.list_models(), [])
        providers.save_provider(self.entry())
        providers.save_provider(self.entry("p2"))
        self.assertEqual(providers.get_active_id(), "p1")
        self.assertTrue(providers.set_active("p2"))
        self.assertEqual(providers.get_active_id(), "p2")
        self.assertFalse(providers.set_active("nope"))
        providers.delete_provider("p2")
        self.assertEqual(providers.get_active_id(), "p1")

    def test_key_status_masked(self):
        providers.save_provider(self.entry())
        with mock.patch.object(providers, "_legacy_env", return_value={}):
            self.assertEqual(providers.provider_status("p1")["key"], "missing")
        self.fake.set_password(providers.SERVICE, "p1", "sk-secret-value")
        self.assertEqual(providers.provider_status("p1")["key"], "keyring")
        masked = providers.mask("sk-secret-value")
        self.assertNotIn("secret", masked)
        self.assertTrue(masked.startswith("sk-"))

    def test_active_config_prefers_keyring(self):
        providers.save_provider(self.entry())
        providers.set_key("p1", "sk-ring")
        base, key, model, source = providers.active_config()
        self.assertEqual(
            (base, key, model, source), ("https://x/v1", "sk-ring", "m", "keyring")
        )

    def test_active_config_falls_back_to_legacy(self):
        base, key, model, source = providers.active_config()
        self.assertEqual((key, source), ("test-key", "legacy-env"))

    def test_file_has_no_secrets(self):
        providers.save_provider(self.entry())
        providers.set_key("p1", "sk-ring")
        raw = paths.providers_file().read_text()
        self.assertNotIn("sk-ring", raw)
        self.assertNotIn("test-key", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
