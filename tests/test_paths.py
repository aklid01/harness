"""Paths: explicit project root plus app-data dir, with legacy migration."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import paths  # noqa: E402
from harness import providers  # noqa: E402
from harness import session  # noqa: E402


class PathsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.appdata = Path(self.tmp.name) / "appdata"
        paths.init(project_root=self.project, app_data_dir=self.appdata)
        self.addCleanup(paths.reset)

    def test_slug_is_filename_safe(self):
        self.assertNotRegex(paths.slug_for(self.project), r"[:\\\\/]")
        self.assertTrue(paths.slug_for(self.project))

    def test_providers_file_lives_in_app_data(self):
        self.assertEqual(paths.providers_file(), self.appdata.resolve() / "providers.json")

    def test_session_dir_uses_explicit_root(self):
        expected = Path.home() / ".agents" / "sessions" / paths.slug_for(self.project)
        self.assertEqual(paths.session_dir(), expected)
        self.assertEqual(session._dir(), expected)

    def test_migrate_copies_legacy_file_once(self):
        legacy = self.project / "providers.json"
        legacy.write_text(json.dumps({"active_id": None, "providers": []}))
        with mock.patch.object(Path, "cwd", return_value=self.project):
            found = paths.migrate_providers()
        self.assertEqual(found, legacy)
        self.assertTrue(paths.providers_file().exists())
        self.assertIsNone(paths.migrate_providers())

    def test_providers_crud_uses_new_location(self):
        providers.save_provider({"id": "p1", "name": "P1", "base_url": "https://x/v1",
                                 "model": "m", "headers": {}, "disabled": False})
        self.assertTrue(paths.providers_file().exists())
        self.assertEqual(providers.get_active_id(), "p1")

    def test_env_and_skill_dirs_follow_root(self):
        self.assertEqual(paths.env_files()[0], self.project.resolve() / ".env")
        self.assertEqual(paths.skill_dirs()[0], self.project.resolve() / ".agents" / "skills")

    def test_cwd_independence(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        paths.init(project_root=other, app_data_dir=self.appdata)
        self.assertNotEqual(paths.session_dir(), Path.home() / ".agents" / "sessions" / paths.slug_for(self.project))


if __name__ == "__main__":
    unittest.main(verbosity=2)
