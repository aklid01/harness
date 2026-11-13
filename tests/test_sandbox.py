"""The bash tool on Windows: run ordinary commands through a real POSIX shell.

Two things matter and are tested where the environment provides them: reading a
file with ``cat`` and a Windows path works, and the Win->MSYS path translation
is a pure, correct function everywhere. Where no POSIX shell backs the tool, the
behaviour tests skip rather than fail a platform we cannot change.
"""

import tempfile
import unittest
from pathlib import Path

from harness import paths, sandbox


def has_posix_shell():
    return sandbox._find_bash() is not None


class PathifyCase(unittest.TestCase):
    """Path translation is deterministic and never disturbs shell operators."""

    def test_drive_paths_become_msys_mounts(self):
        self.assertEqual(sandbox._posix_pathify(r"cat D:\a\b\c.py"), "cat /d/a/b/c.py")
        self.assertEqual(sandbox._posix_pathify(r"D:/x/y"), "/d/x/y")
        self.assertEqual(sandbox._posix_pathify(r"cat C:\p\f"), "cat /c/p/f")

    def test_operators_and_redirects_are_untouched(self):
        self.assertEqual(
            sandbox._posix_pathify(r"cat D:\f.txt && echo hi 2>/dev/null || ls"),
            "cat /d/f.txt && echo hi 2>/dev/null || ls",
        )


@unittest.skipUnless(
    has_posix_shell(), "no POSIX shell available to back the bash tool"
)
class SandboxRunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        paths.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths.reset)

    def run_cmd(self, command):
        return sandbox.run(command)

    def test_reading_a_file_with_cat_and_a_windows_path_works(self):
        target = self.root / "note.txt"
        target.write_text("line-one\nline-two\n", encoding="utf-8")
        out = self.run_cmd(f"cat {target}")
        self.assertIn("line-one", out.stdout + out.stderr)

    def test_head_and_a_compound_command_work(self):
        target = self.root / "note.txt"
        target.write_text("a\nb\nc\n", encoding="utf-8")
        out = self.run_cmd(f"head -1 {target}; echo done")
        self.assertIn("a", out.stdout)
        self.assertIn("done", out.stdout)

    def test_a_listing_with_operators_does_not_error_out(self):
        out = self.run_cmd(f"ls -la {self.root} 2>/dev/null || ls {self.root}")
        self.assertEqual(out.returncode, 0, (out.stdout + out.stderr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
