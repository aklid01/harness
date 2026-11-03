"""Skill discovery: scan the skill dirs, expose names and read one on demand.

A skill is a directory with a SKILL.md (frontmatter name/description plus
instructions). Scanning happens per call so a workspace's own skills win
over global ones of the same name.
"""

from pathlib import Path

import yaml

from . import paths


def _dirs():
    if paths.is_initialized():
        return paths.skill_dirs()
    return [Path.cwd() / ".agents" / "skills", Path.home() / ".agents" / "skills"]


def find_skills():
    """Map each skill name to its description and SKILL.md path.

    Re-scanned on every call so the active workspace's own skills are picked
    up: the engine serves many workspaces from one process, and an import
    snapshot would freeze the first one forever. The scan is a glob plus a
    handful of small frontmatter reads.

    Project skills win over global ones of the same name - the same rule the
    instruction files follow (workspace first). The later directory used to
    overwrite, so a global install silently shadowed the workspace's own
    skill.
    """
    skills = {}
    for directory in _dirs():
        for path in sorted(directory.glob("*/SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                _, frontmatter, _ = text.split("---", 2)
                meta = yaml.safe_load(frontmatter)
                description = " ".join(str(meta["description"]).split())
                skills.setdefault(
                    meta["name"], {"description": description, "path": path}
                )
            except (ValueError, KeyError, TypeError, yaml.YAMLError, OSError):
                continue
    return skills


def skills_prompt():
    skills = find_skills()
    return "\n".join(f"- {name}: {s['description']}" for name, s in skills.items())


def read_skill(name: str) -> str:
    """Open a skill and return its full instructions."""
    skill = find_skills().get(name)
    if skill is None:
        return f"No skill named '{name}'."
    return skill["path"].read_text(encoding="utf-8", errors="replace")
