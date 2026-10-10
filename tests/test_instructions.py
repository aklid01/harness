"""Project instructions: AGENTS.md reaches the system prompt - the workspace's
own file first, the global one as a fallback, and neither allowed to crowd out
the task."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import instructions, llm, paths  # noqa: E402


class InstructionsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        paths.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths.reset)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_project_agents_md_reaches_the_prompt(self):
        self.write(self.root / "AGENTS.md", "- Never use em dashes.\n- Use the venv python.")
        prompt = llm.system_prompt("build")
        self.assertIn("Never use em dashes", prompt)
        self.assertIn("Use the venv python", prompt)
        # paths.init resolves the root, so compare against the resolved path.
        self.assertIn(str((self.root / "AGENTS.md").resolve()), prompt)

    def test_no_agents_md_injects_nothing(self):
        self.assertEqual(instructions.load(), "")
        prompt = llm.system_prompt("build")
        self.assertNotIn("{instructions}", prompt)
        self.assertIn("## Project rules", prompt)
        self.assertLess(len(prompt), 4000, "an empty instructions block stays small")

    def test_the_global_file_is_the_fallback(self):
        home = Path(self.tmp.name) / "home"
        self.write(home / ".agents" / "AGENTS.md", "- Global rule: run the tests.")
        with mock.patch("pathlib.Path.home", return_value=home):
            self.write(self.root / "AGENTS.md", "- Project rule: single quotes.")
            prompt = llm.system_prompt("build")
            self.assertIn("Project rule: single quotes", prompt)
            self.assertIn("Global rule: run the tests", prompt)

    def test_a_huge_agents_md_is_capped(self):
        self.write(self.root / "AGENTS.md", "x" * (instructions.MAX_CHARS * 2))
        text = instructions.load()
        self.assertLessEqual(len(text), instructions.MAX_CHARS + 200)
        self.assertIn("truncated", text)
        self.assertIn("## Project rules", llm.system_prompt("build"))

    def test_plan_mode_gets_the_rules_too(self):
        self.write(self.root / "AGENTS.md", "- Plan rule: read before writing.")
        self.assertIn("Plan rule: read before writing", llm.system_prompt("plan"))

    def test_injected_content_cannot_rewrite_the_prompt(self):
        # The file's own text is substituted last, so a placeholder inside it
        # must stay literal rather than being expanded again.
        self.write(self.root / "AGENTS.md", "Rules: {cwd} {skills} {instructions}")
        prompt = llm.system_prompt("build")
        self.assertIn("{cwd} {skills} {instructions}", prompt)
        self.assertIn(f"Your current working directory is: {paths.project_root()}", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
