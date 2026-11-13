"""Project instructions: AGENTS.md reaches the system prompt - the workspace's
own file first, the global one as a fallback, and neither allowed to crowd out
the task."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from harness import instructions, llm, paths


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
        self.write(
            self.root / "AGENTS.md", "- Never use em dashes.\n- Use the venv python."
        )
        prompt = llm.system_prompt("build")
        self.assertIn("Never use em dashes", prompt)
        self.assertIn("Use the venv python", prompt)

        self.assertIn(str((self.root / "AGENTS.md").resolve()), prompt)

    def test_no_agents_md_injects_nothing(self):
        self.assertEqual(instructions.load(), "")
        prompt = llm.system_prompt("build")
        self.assertNotIn("{instructions}", prompt)
        self.assertIn("## Project rules", prompt)

        skills = llm.skills_prompt()
        without_skills = len(prompt) - (len(skills) if skills else 0)
        self.assertLess(without_skills, 4400, "an empty instructions block stays small")

    def test_the_global_file_is_the_fallback(self):
        home = Path(self.tmp.name) / "home"
        self.write(home / ".agents" / "AGENTS.md", "- Global rule: run the tests.")
        with mock.patch("pathlib.Path.home", return_value=home):
            self.write(self.root / "AGENTS.md", "- Project rule: single quotes.")
            prompt = llm.system_prompt("build")
            self.assertIn("Project rule: single quotes", prompt)
            self.assertIn("Global rule: run the tests", prompt)

    def test_the_app_profile_folder_is_a_global_source(self):

        app_dir = Path(self.tmp.name) / "app"
        self.write(app_dir / "AGENTS.md", "- App rule: ask before deleting.")
        prompt = llm.system_prompt("build")
        self.assertIn("App rule: ask before deleting", prompt)
        self.assertIn(str((app_dir / "AGENTS.md").resolve()), prompt)

    def test_sources_stack_project_first(self):
        home = Path(self.tmp.name) / "home"
        app_dir = Path(self.tmp.name) / "app"
        self.write(self.root / "AGENTS.md", "- Project rule.")
        self.write(home / ".agents" / "AGENTS.md", "- Engine global rule.")
        self.write(app_dir / "AGENTS.md", "- App profile rule.")
        with mock.patch("pathlib.Path.home", return_value=home):
            prompt = llm.system_prompt("build")
        self.assertLess(
            prompt.index("Project rule"), prompt.index("Engine global rule")
        )
        self.assertLess(
            prompt.index("Engine global rule"), prompt.index("App profile rule")
        )
        self.assertLess(prompt.index("Project rule"), prompt.index("App profile rule"))

    def test_the_profile_fallback_to_the_home_dir_is_not_duplicated(self):

        home = Path(self.tmp.name) / "home"
        with mock.patch("pathlib.Path.home", return_value=home):
            paths.reset()
            files = paths.instruction_files()
            paths.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.assertEqual(len(files), len(set(files)))
        self.assertIn(home / ".agents" / "AGENTS.md", files)

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

        self.write(self.root / "AGENTS.md", "Rules: {cwd} {skills} {instructions}")
        prompt = llm.system_prompt("build")
        self.assertIn("{cwd} {skills} {instructions}", prompt)
        self.assertIn(
            f"Your current working directory is: {paths.project_root()}", prompt
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
