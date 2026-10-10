"""Central path resolution: explicit project root plus app data dir.

All cwd/home lookups live here. Other modules must not call Path.cwd()
directly for storage - they ask this module instead, so the Electron sidecar
can pass an explicit project folder and user-data dir.
"""

import contextlib
import contextvars
import os
import re
from pathlib import Path

_PROJECT_ROOT: Path | None = None
_APP_DATA_DIR: Path | None = None
_INITIALIZED = False
_ACTIVE_ROOT = contextvars.ContextVar("harness_active_root", default=None)


def slug_for(root: Path | str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(Path(root).resolve())).strip("-")


def init(project_root: Path | str | None = None, app_data_dir: Path | str | None = None) -> None:
    global _PROJECT_ROOT, _APP_DATA_DIR, _INITIALIZED
    _PROJECT_ROOT = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    if app_data_dir:
        _APP_DATA_DIR = Path(app_data_dir).resolve()
    else:
        _APP_DATA_DIR = Path.home() / ".agents"
    _APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _INITIALIZED = True


def reset() -> None:
    global _PROJECT_ROOT, _APP_DATA_DIR, _INITIALIZED
    _PROJECT_ROOT = None
    _APP_DATA_DIR = None
    _INITIALIZED = False


def is_initialized() -> bool:
    return _INITIALIZED


@contextlib.contextmanager
def rooted(root: Path | str | None):
    """Resolve project_root()/session_dir()/... against ``root`` for this context.

    This is how one process serves many workspaces at once: a session's worker
    sets its own root, and helpers below pick it up. Falls back to the process
    default when nothing is attached (the legacy GUI / single-root mode).
    """
    token = _ACTIVE_ROOT.set(str(root) if root else None)
    try:
        yield
    finally:
        try:
            _ACTIVE_ROOT.reset(token)
        except ValueError:
            pass


def _active_root() -> Path | None:
    r = _ACTIVE_ROOT.get()
    return Path(r) if r else None


def project_root() -> Path:
    active = _active_root()
    if active is not None:
        return active
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT
    return Path.cwd().resolve()


def default_project_root() -> Path:
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT
    return Path.cwd().resolve()


def app_data_dir() -> Path:
    if _APP_DATA_DIR is not None:
        return _APP_DATA_DIR
    return Path.home() / ".agents"


def project_name() -> str:
    return project_root().name


def providers_file() -> Path:
    return app_data_dir() / "providers.json"


def legacy_providers_files() -> list[Path]:
    seen: list[Path] = []
    for candidate in (Path.cwd() / "providers.json",
                      project_root() / "providers.json",
                      Path.home() / ".agents" / "providers.json"):
        if candidate not in seen:
            seen.append(candidate)
    current = providers_file().resolve() if _INITIALIZED else None
    return [p for p in seen if current is None or p.resolve() != current]


def migrate_providers() -> Path | None:
    target = providers_file()
    if target.exists():
        return None
    for source in legacy_providers_files():
        try:
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                return source
        except OSError:
            continue
    return None


def sessions_root() -> Path:
    """Where transcripts live. Isolated for tests via HARNESS_SESSIONS_ROOT."""
    override = os.environ.get("HARNESS_SESSIONS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agents" / "sessions"


def session_dir(root: Path | str | None = None) -> Path:
    base = Path(root).resolve() if root else project_root()
    return sessions_root() / slug_for(base)


def titles_file(root: Path | str | None = None) -> Path:
    return session_dir(root) / "titles.json"


def env_files() -> list[Path]:
    return [project_root() / ".env", Path.home() / ".agents" / "env"]


def instruction_files() -> list[Path]:
    """Project instructions: the workspace's own, then the global fallbacks.

    The engine's own global (`~/.agents`) comes before the app profile's, and
    the two collapse to one entry when they are the same folder - the profile
    falls back to `~/.agents` when no explicit app data dir was given.
    """
    found = []
    for path in (project_root() / "AGENTS.md",
                 Path.home() / ".agents" / "AGENTS.md",
                 app_data_dir() / "AGENTS.md"):
        if path not in found:
            found.append(path)
    return found


def skill_dirs() -> list[Path]:
    return [project_root() / ".agents" / "skills", Path.home() / ".agents" / "skills"]
