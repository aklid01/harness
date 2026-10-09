from pathlib import Path

import yaml

from . import paths

SKILL_DIRS = [
    Path.cwd() / ".agents" / "skills",
    Path.home() / ".agents" / "skills",
]


def _dirs():
    if paths.is_initialized():
        return paths.skill_dirs()
    return SKILL_DIRS


def find_skills():
    """Map each skill name to its description and SKILL.md path."""
    skills = {}
    for directory in _dirs():
        for path in sorted(directory.glob("*/SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                _, frontmatter, _ = text.split("---", 2)
                meta = yaml.safe_load(frontmatter)
                description = " ".join(str(meta["description"]).split())
                skills[meta["name"]] = {"description": description, "path": path}
            except (ValueError, KeyError, TypeError, yaml.YAMLError, OSError):
                continue
    return skills


SKILLS = find_skills()


def skills_prompt():
    return "\n".join(f"- {name}: {s['description']}" for name, s in SKILLS.items())


def read_skill(name: str) -> str:
    """Open a skill and return its full instructions."""
    if name not in SKILLS:
        return f"No skill named '{name}'."
    return SKILLS[name]["path"].read_text(encoding="utf-8", errors="replace")
