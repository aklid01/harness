"""Skill discovery: the workspace's own skills win over global installs."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import skills


def write_skill(root, name, description, body="do the thing"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return directory / "SKILL.md"


class SkillsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project-skills"
        self.global_ = self.tmp / "global-skills"
        self.project.mkdir()
        self.global_.mkdir()
        patcher = mock.patch.object(
            skills, "_dirs", return_value=[self.project, self.global_]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_workspace_skill_wins_over_global_of_the_same_name(self):
        write_skill(self.global_, "deploy", "global deploy", body="global steps")
        write_skill(self.project, "deploy", "project deploy", body="project steps")
        found = skills.find_skills()
        self.assertEqual(found["deploy"]["description"], "project deploy")
        self.assertEqual(found["deploy"]["path"].parent, self.project / "deploy")

    def test_global_skill_is_visible_when_the_workspace_has_none(self):
        write_skill(self.global_, "lint", "global lint")
        found = skills.find_skills()
        self.assertEqual(found["lint"]["description"], "global lint")

    def test_prompt_lists_name_and_description(self):
        write_skill(self.project, "review", "run the reviewers")
        self.assertEqual(skills.skills_prompt(), "- review: run the reviewers")

    def test_read_skill_returns_the_file_and_names_unknowns(self):
        path = write_skill(self.project, "ship", "ship it", body="step one")
        self.assertEqual(
            skills.read_skill("ship"),
            "---\nname: ship\ndescription: ship it\n---\nstep one\n",
        )
        self.assertEqual(path.parent.name, "ship")
        self.assertIn("No skill named 'nope'", skills.read_skill("nope"))

    def test_a_broken_skill_file_is_skipped_without_killing_the_scan(self):
        (self.project / "broken").mkdir()
        (self.project / "broken" / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
        write_skill(self.project, "fine", "a fine skill")
        found = skills.find_skills()
        self.assertEqual([name for name in found], ["fine"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
