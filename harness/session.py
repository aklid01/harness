"""Transcripts on disk. One JSONL file per chat, plus a titles sidecar."""

import contextlib
import contextvars
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import paths

PROJECT = re.sub(r"[^A-Za-z0-9._-]+", "-", str(Path.cwd().resolve())).strip("-")
SESSION_DIR = Path.home() / ".agents" / "sessions" / PROJECT
TITLES_FILE = SESSION_DIR / "titles.json"
_DEFAULT_DIR = Path(SESSION_DIR)
_DEFAULT_TITLES = Path(TITLES_FILE)
CURRENT = datetime.now().strftime("%Y%m%d-%H%M%S")
WRITTEN = 0

# Per-session overrides. When a context is scoped (see ``scoped``), these take
# precedence over the module globals, so one process can own many active chats.
_CUR = contextvars.ContextVar("session_current", default=None)
_WRIT = contextvars.ContextVar("session_written", default=None)
_ROOT = contextvars.ContextVar("session_root", default=None)


@contextlib.contextmanager
def scoped(root=None, session_id=None, written=None):
    """Scope the active chat to ``root`` + ``session_id`` for this context."""
    tokens = []
    if root is not None:
        tokens.append(_ROOT.set(str(root)))
    if session_id is not None:
        tokens.append(_CUR.set(session_id))
    if written is not None:
        tokens.append(_WRIT.set(int(written)))
    try:
        yield
    finally:
        for t in reversed(tokens):
            try:
                (t.var).reset(t)
            except (ValueError, AttributeError):
                pass


def current_id():
    v = _CUR.get()
    return v if v is not None else CURRENT


def written():
    v = _WRIT.get()
    return v if v is not None else WRITTEN


def _set_current(x):
    if _CUR.get() is not None:
        _CUR.set(x)
    else:
        global CURRENT
        CURRENT = x


def _set_written(x):
    if _WRIT.get() is not None:
        _WRIT.set(int(x))
    else:
        global WRITTEN
        WRITTEN = int(x)


def _dir():
    r = _ROOT.get()
    if r is not None:
        return paths.session_dir(r)
    if Path(SESSION_DIR) != _DEFAULT_DIR:
        return Path(SESSION_DIR)
    if paths.is_initialized():
        return paths.session_dir()
    return Path(SESSION_DIR)


def _titles_path():
    r = _ROOT.get()
    if r is not None:
        return paths.titles_file(r)
    if Path(TITLES_FILE) != _DEFAULT_TITLES:
        return Path(TITLES_FILE)
    if paths.is_initialized():
        return paths.titles_file()
    return Path(TITLES_FILE)


def migrate_root_sessions():
    """Move timestamp session files out of the project root."""

    root = paths.project_root() if paths.is_initialized() else Path.cwd()
    target = _dir()
    try:
        if target.resolve() == root.resolve():
            return 0
    except OSError:
        pass
    moved = 0
    for path in root.glob("*.jsonl"):
        if re.fullmatch(r"\d{8}-\d{6}", path.stem) and path.is_file():
            try:
                target.mkdir(parents=True, exist_ok=True)
                path.rename(target / path.name)
                moved += 1
            except OSError:
                continue
    return moved


def _titles():
    try:
        data = json.loads(_titles_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_title(session_id, title):
    titles = _titles()
    titles[session_id] = title
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        _titles_path().write_text(json.dumps(titles, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def delete_session(session_id):
    try:
        path_for(session_id).unlink(missing_ok=True)
    except OSError:
        pass
    titles = _titles()
    if session_id in titles:
        del titles[session_id]
        try:
            _titles_path().write_text(json.dumps(titles, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def path_for(session_id):
    return _dir() / f"{session_id}.jsonl"


def save(messages):
    """Append what is new. Never rewrite what is already on disk."""
    w = written()
    _dir().mkdir(parents=True, exist_ok=True)
    with path_for(current_id()).open("a", encoding="utf-8") as f:
        for message in messages[w:]:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
    _set_written(len(messages))


def fresh_id():
    """A unique 14-char timestamp id (YYYYMMDD-HHMMSS), bumping past collisions."""
    base = datetime.now()
    while True:
        sid = base.strftime("%Y%m%d-%H%M%S")
        if not path_for(sid).exists():
            return sid
        base += timedelta(seconds=1)


def blank(session_id):
    """Materialise a session immediately so it shows up before the first turn."""
    _dir().mkdir(parents=True, exist_ok=True)
    path_for(session_id).touch()


def rewind_to(count):
    """Record a rewind as an entry, so the old messages stay in the file."""
    _dir().mkdir(parents=True, exist_ok=True)
    with path_for(current_id()).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"rewind_to": count}) + "\n")
    _set_written(count)


def compacted(messages):
    """Compaction rewrites history, so record the result and start from it."""
    _dir().mkdir(parents=True, exist_ok=True)
    with path_for(current_id()).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"compacted": messages}, ensure_ascii=False) + "\n")
    _set_written(len(messages))


def load(session_id):
    """Replay the log: messages accumulate, rewinds cut them back."""
    messages = []
    for line in path_for(session_id).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A half-written last line, usually from a kill mid-save. Skipping
            # it costs one message; raising would break /sessions for every
            # chat in the project, because listing them all calls load().
            continue
        if "rewind_to" in entry:
            del messages[entry["rewind_to"]:]
        elif "compacted" in entry:
            messages = list(entry["compacted"])
        else:
            messages.append(entry)
    return messages


def open_session(session_id):
    """Switch to a past chat and become it."""
    _set_current(session_id)
    messages = load(session_id)
    _set_written(len(messages))
    return messages


def title(messages, session_id=None):
    if session_id:
        saved = _titles().get(session_id)
        if saved:
            return saved
    for message in messages:
        if message["role"] == "user":
            text = " ".join(str(message.get("content") or "").split())
            if text and not text.startswith("<"):
                return text[:60]
    return "(empty)"


def ensure_title(messages):
    """Ask the model for a short name once per session. Best effort."""
    from . import providers

    if _titles().get(current_id()):
        return None
    first = title(messages)
    if first in ("(empty)", "New Session"):
        return None
    name = providers.suggest_title(first)
    if name:
        set_title(current_id(), name)
    return name


def all_sessions():
    """Newest first, with LLM titles where present."""
    target = _dir()
    if not target.exists():
        return []
    files = sorted(
        target.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return [{"id": p.stem, "title": title(load(p.stem), p.stem)} for p in files]
