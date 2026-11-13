"""Session storage: sanitized dir, root migration, titles, rename, delete."""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from harness import (
    paths,
    session,
)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.abspath("tmp-session-test"))

        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(self.tmp / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        paths.init(project_root=self.tmp, app_data_dir=self.tmp / "appdata")
        self.addCleanup(paths.reset)
        self.dir = paths.session_dir()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        mock.patch.object(session, "CURRENT", "20260101-000000").start()
        mock.patch.object(session, "WRITTEN", 0).start()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_roundtrip(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hello"},
        ]
        session.save(messages)
        self.assertTrue((self.dir / "20260101-000000.jsonl").exists())
        self.assertEqual(session.load("20260101-000000"), messages)

    def test_migrate_moves_root_sessions(self):
        stray = Path("20251231-235959.jsonl")
        stray.write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")
        keep = Path("notes.jsonl")
        keep.write_text("{}")
        moved = session.migrate_root_sessions()
        self.assertEqual(moved, 1)
        self.assertTrue((self.dir / "20251231-235959.jsonl").exists())
        self.assertTrue(keep.exists())

    def test_titles_rename_delete(self):
        session.save(
            [{"role": "user", "content": "a fairly long first message about widgets"}]
        )
        self.assertIn("widgets", session.all_sessions()[0]["title"])
        session.set_title("20260101-000000", "my name")
        self.assertEqual(session.all_sessions()[0]["title"], "my name")
        self.assertEqual(
            session.title(session.load("20260101-000000"), "20260101-000000"), "my name"
        )
        session.delete_session("20260101-000000")
        self.assertEqual(session.all_sessions(), [])

    def test_ensure_title_uses_llm_once(self):
        messages = [{"role": "user", "content": "fix the login bug"}]
        session.save(messages)
        with mock.patch(
            "harness.llm.suggest_title", return_value="Login fix"
        ) as suggest:
            session.ensure_title(messages)
            self.assertEqual(suggest.call_count, 1)
            session.ensure_title(messages)
            self.assertEqual(suggest.call_count, 1)
        self.assertEqual(session.all_sessions()[0]["title"], "Login fix")


if __name__ == "__main__":
    unittest.main(verbosity=2)
