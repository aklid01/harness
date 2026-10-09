"""Permission modes: deny list wins everywhere; modes only relax ask."""

import os
import unittest

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import permissions  # noqa: E402


class PermissionCase(unittest.TestCase):
    def setUp(self):
        permissions.set_mode("default")
        self.addCleanup(permissions.set_mode, "default")

    def inside_write(self):
        return permissions.check("write_file", {"path": str(os.path.abspath("x.txt"))})

    def test_deny_wins_in_every_mode(self):
        for mode in permissions.MODES:
            permissions.set_mode(mode)
            action, _ = permissions.check("bash", {"command": "rm -rf /tmp/x"})
            self.assertEqual(action, "deny", mode)

    def test_default_asks_unknown_bash(self):
        action, _ = permissions.check("bash", {"command": "python -c 'print(1)'"})
        self.assertEqual(action, "ask")

    def test_allow_all_relaxes_ask(self):
        permissions.set_mode("allow-all")
        action, _ = permissions.check("bash", {"command": "python -c 'print(1)'"})
        self.assertEqual(action, "allow")

    def test_autopilot_relaxes_ask(self):
        permissions.set_mode("autopilot")
        action, _ = permissions.check("bash", {"command": "python -c 'print(1)'"})
        self.assertEqual(action, "allow")

    def test_strict_asks_project_writes(self):
        self.assertEqual(self.inside_write()[0], "allow")
        permissions.set_mode("strict")
        self.assertEqual(self.inside_write()[0], "ask")

    def test_outside_writes_stay_ask_except_autopilot(self):
        outside = {"path": "C:/Windows/Temp/x.txt"}
        self.assertEqual(permissions.check("write_file", outside)[0], "ask")
        permissions.set_mode("allow-all")
        self.assertEqual(permissions.check("write_file", outside)[0], "ask")
        permissions.set_mode("autopilot")
        self.assertEqual(permissions.check("write_file", outside)[0], "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
