"""Headless sidecar: Python engine plus WebSocket transport for Electron.

Electron spawns `uv run python -m harness.server` and talks to it over
localhost. The engine itself is unchanged - this file puts the event bus
(harness/events.py) on a socket, one session per client connection.
"""

import argparse
import asyncio
import json
import threading
from pathlib import Path

from . import checkpoints, events


class EngineService:
    def __init__(self, send_json):
        from .agent import Runner

        self._runner_cls = Runner
        self._send = send_json
        self.cancel = threading.Event()
        self.runner = None
        self.running = False
        self._lock = threading.Lock()
        self._title_lock = threading.Lock()
        self._titling: set = set()
        self._seq = 0
        self.pending: dict = {}
        self._submissions: dict = {}
        events.subscribe(self._forward)
        events.set_responder(self._respond)

    def _forward(self, type, payload):
        try:
            self._send({"event": type, "payload": dict(payload)})
        except Exception:
            pass

    def _next_id(self):
        self._seq += 1
        return f"r{self._seq}"

    def _respond(self, kind, payload):
        if kind not in (events.APPROVAL, events.PICK):
            return None
        req_id = self._next_id()
        box: dict = {}
        gate = threading.Event()
        self.pending[req_id] = (box, gate)
        try:
            body = {"request": kind, "id": req_id}
            body.update(payload or {})
            self._send(body)
            gate.wait(timeout=300)
            return box.get("result", False if kind == events.APPROVAL else None)
        finally:
            self.pending.pop(req_id, None)

    def reply(self, req_id, result):
        entry = self.pending.get(req_id)
        if entry is None:
            return False
        box, gate = entry
        box["result"] = result
        gate.set()
        return True

    def _ensure_runner(self, mode="build"):
        if self.runner is None:
            self.runner = self._runner_cls(
                stream=True, cancel_event=self.cancel, mode=mode
            )
        else:
            self.runner.mode = mode

    def _seen_submission(self, submission_id):
        """Exactly-once guard, keyed by submission id and never by text."""
        if not submission_id:
            return False
        if submission_id in self._submissions:
            return True
        self._submissions[submission_id] = True
        if len(self._submissions) > 32:
            self._submissions.pop(next(iter(self._submissions)))
        return False

    def submit(self, text, mode="build", submission_id=None):
        with self._lock:
            if self._seen_submission(submission_id):
                self._send(
                    {
                        "event": events.NOTE,
                        "payload": {"text": "that submission was already sent"},
                    }
                )
                return False
            if self.running:
                self._send(
                    {
                        "event": events.NOTE,
                        "payload": {"text": "already running - press Stop first"},
                    }
                )
                return False
            self.running = True
        self.cancel.clear()
        threading.Thread(target=self._turn, args=(text, mode), daemon=True).start()
        return True

    def _turn(self, text, mode):
        try:
            self._ensure_runner(mode)
            self.runner.handle_input(text)
        finally:
            with self._lock:
                self.running = False
            self._send({"event": "done", "payload": {}})
            self._maybe_title()

    def _maybe_title(self):
        try:
            from . import session as session_mod

            sid = session_mod.CURRENT
            if session_mod._titles().get(sid):
                return
            with self._title_lock:
                if sid in self._titling:
                    return
                self._titling.add(sid)
            try:
                messages = session_mod.load(sid)
            except OSError:
                with self._title_lock:
                    self._titling.discard(sid)
                return
            first = session_mod.title(messages)
            if first in ("(empty)", ""):
                with self._title_lock:
                    self._titling.discard(sid)
                return
        except Exception:
            return

        def fetch():
            try:
                from . import providers as providers_mod
                from . import session as session_mod

                name = providers_mod.suggest_title(first)
                if name and not session_mod._titles().get(sid):
                    session_mod.set_title(sid, name)
                    self._send({"event": "sessions_updated", "payload": {}})
            except Exception:
                pass
            finally:
                with self._title_lock:
                    self._titling.discard(sid)

        threading.Thread(target=fetch, daemon=True).start()

    def open_session(self, session_id, mode="build"):
        with self._lock:
            if self.running:
                return False
            self.running = True
        self.cancel.clear()
        threading.Thread(
            target=self._open, args=(session_id, mode), daemon=True
        ).start()
        return True

    def _open(self, session_id, mode):
        try:
            from . import history
            from . import session as session_mod

            if session_id:
                messages = session_mod.open_session(session_id)
                history.strip(messages)
                self.runner = self._runner_cls(stream=True, cancel_event=self.cancel)
                self.runner.messages = messages
                label = "opened"
            else:
                session_mod.CURRENT = session_mod.fresh_id()
                session_mod.WRITTEN = 0
                session_mod.blank(session_mod.CURRENT)
                self.runner = self._runner_cls(
                    stream=True, cancel_event=self.cancel, mode=mode or "build"
                )
                label = "new"
            events.emit(
                events.SESSION,
                {
                    "label": label,
                    "messages": self.runner.messages,
                    "id": session_mod.CURRENT,
                },
            )
        finally:
            with self._lock:
                self.running = False
            self._send({"event": "done", "payload": {}})

    def rewind_to(self, count):
        from . import session as session_mod

        with self._lock:
            if self.running:
                return False
            try:
                count = max(0, int(count))
            except (TypeError, ValueError):
                return False
            if self.runner is not None:
                count = min(count, len(self.runner.messages))
            session_mod.rewind_to(count)
            if self.runner is not None:
                self.runner.messages = self.runner.messages[:count]
                messages = self.runner.messages
            else:
                try:
                    messages = session_mod.load(session_mod.CURRENT)
                except OSError:
                    messages = []
            events.emit(
                events.SESSION,
                {"label": "rewound", "messages": messages, "id": session_mod.CURRENT},
            )
            return True

    def resubmit(self, count, text, mode="build", restore_files=True):
        with self._lock:
            if self.running:
                self._send(
                    {
                        "event": events.NOTE,
                        "payload": {"text": "already running - press Stop first"},
                    }
                )
                return False
            self.running = True
        self.cancel.clear()
        threading.Thread(
            target=self._resend, args=(count, text, mode, restore_files), daemon=True
        ).start()
        return True

    def _resend(self, count, text, mode, restore_files=True):
        try:
            from . import session as session_mod

            self._ensure_runner(mode)
            try:
                count = max(0, min(int(count), len(self.runner.messages)))
            except (TypeError, ValueError):
                count = len(self.runner.messages)
            if restore_files:
                checkpoints.restore_since(session_mod.CURRENT, count)
            session_mod.rewind_to(count)
            self.runner.messages = self.runner.messages[:count]
            events.emit(
                events.SESSION,
                {
                    "label": "rewound",
                    "messages": self.runner.messages,
                    "id": session_mod.CURRENT,
                },
            )
            self.runner.handle_input(text)
        finally:
            with self._lock:
                self.running = False
            self._send({"event": "done", "payload": {}})
            self._maybe_title()

    def stop(self):
        self.cancel.set()

    def shutdown(self):
        for box, gate in list(self.pending.values()):
            box["result"] = False
            gate.set()
        try:
            events.unsubscribe(self._forward)
        except Exception:
            pass
        events.set_responder(None)


def _git_run(root, *args, timeout=10):
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_status(root):
    if _git_run(root, "rev-parse", "--is-inside-work-tree") != "true":
        return {
            "has_git": False,
            "branch": "",
            "default_branch": "",
            "is_protected": False,
        }
    branch = _git_run(root, "branch", "--show-current") or ""
    default = ""
    head_ref = _git_run(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if head_ref and head_ref.startswith("refs/remotes/origin/"):
        default = head_ref.rsplit("/", 1)[-1]
    if not default:
        for candidate in ("main", "master"):
            if (
                _git_run(root, "show-ref", "--verify", f"refs/heads/{candidate}")
                is not None
            ):
                default = candidate
                break
    if not default:
        default = branch
    return {
        "has_git": True,
        "branch": branch,
        "default_branch": default,
        "is_protected": bool(branch) and branch == default,
    }


def git_create_branch(root, name):
    import re
    import subprocess

    name = (name or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", name)
        or name.startswith(("-", ".", "/"))
        or ".." in name
        or name.endswith(("/", ".", ".lock"))
    ):
        return False, "invalid branch name"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "checkout", "-b", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as failure:
        return False, f"{type(failure).__name__}: {failure}"
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "git failed"
    return True, name


def handle_op(service, obj):
    from . import paths as paths_mod
    from . import permissions
    from . import providers as providers_mod
    from . import session as session_mod

    op = obj.get("op")
    req_id = obj.get("req_id")
    if op in ("approval_reply", "pick_reply"):
        ok = service.reply(
            obj.get("id"),
            obj.get("allow") if op == "approval_reply" else obj.get("index"),
        )
        return {"reply": op, "req_id": req_id, "ok": ok}
    if op == "submit":
        ok = service.submit(
            obj.get("text", ""),
            obj.get("mode", "build"),
            submission_id=obj.get("submission_id"),
        )
        return {"reply": "submit", "req_id": req_id, "ok": ok}
    if op == "open":
        ok = service.open_session(obj.get("id", ""), obj.get("mode", "build"))
        return {"reply": "open", "req_id": req_id, "ok": ok}
    if op == "rewind_to":
        count = obj.get("count", 0)
        if service.running:
            return {
                "reply": op,
                "req_id": req_id,
                "ok": False,
                "error": "busy or invalid position",
            }
        restored = checkpoints.restore_since(session_mod.CURRENT, count)
        ok = service.rewind_to(count)
        if ok:
            return {"reply": op, "req_id": req_id, "ok": True, "data": restored}
        return {
            "reply": op,
            "req_id": req_id,
            "ok": False,
            "error": "busy or invalid position",
        }
    if op == "rewind_preview":
        count = obj.get("count", 0)
        changes = checkpoints.preview(session_mod.CURRENT, count)
        return {
            "reply": op,
            "req_id": req_id,
            "ok": True,
            "data": {"count": count, "changes": changes},
        }
    if op == "resend":
        ok = service.resubmit(
            obj.get("count", 0), obj.get("text", ""), obj.get("mode", "build")
        )
        return {"reply": "resend", "req_id": req_id, "ok": ok}
    if op == "retry":
        ok = service.resubmit(
            obj.get("count", 0),
            obj.get("text", ""),
            obj.get("mode", "build"),
            restore_files=False,
        )
        return {"reply": "retry", "req_id": req_id, "ok": ok}
    if op == "git_status":
        return {
            "reply": op,
            "req_id": req_id,
            "ok": True,
            "data": git_status(paths_mod.project_root()),
        }
    if op == "git_create_branch":
        ok, detail = git_create_branch(paths_mod.project_root(), obj.get("name", ""))
        if ok:
            return {"reply": op, "req_id": req_id, "ok": True, "data": detail}
        return {"reply": op, "req_id": req_id, "ok": False, "error": detail}
    if op == "stop":
        service.stop()
        return {"reply": "stop", "req_id": req_id, "ok": True}
    if op == "list_sessions":
        return {
            "reply": op,
            "req_id": req_id,
            "ok": True,
            "data": session_mod.all_sessions(),
        }
    if op == "get_transcript":
        try:
            messages = session_mod.load(obj.get("id", ""))
            return {"reply": op, "req_id": req_id, "ok": True, "data": messages}
        except OSError as failure:
            return {"reply": op, "req_id": req_id, "ok": False, "error": str(failure)}
    if op == "rename_session":
        session_mod.set_title(obj.get("id", ""), (obj.get("title", "") or "")[:60])
        return {"reply": op, "req_id": req_id, "ok": True}
    if op == "delete_session":
        sid = obj.get("id", "")
        session_mod.delete_session(sid)
        checkpoints.delete(sid)
        return {"reply": op, "req_id": req_id, "ok": True}
    if op == "list_providers":
        entries = providers_mod.list_providers()
        statuses = {e["id"]: providers_mod.provider_status(e["id"]) for e in entries}
        return {
            "reply": op,
            "req_id": req_id,
            "ok": True,
            "data": {
                "providers": entries,
                "active_id": providers_mod.get_active_id(),
                "statuses": statuses,
                "groups": providers_mod.list_groups(),
                "active_provider_id": providers_mod.get_active_provider_id(),
                "active_model_id": providers_mod.get_active_id(),
            },
        }
    if op == "set_active":
        return {
            "reply": op,
            "req_id": req_id,
            "ok": providers_mod.set_active(obj.get("id")),
        }
    if op == "set_effort":
        providers_mod.set_effort(obj.get("id"), obj.get("effort") or None)
        return {"reply": op, "req_id": req_id, "ok": True}
    if op == "set_permissions":
        permissions.set_mode(obj.get("mode", "default"))
        return {"reply": op, "req_id": req_id, "ok": True}
        if op == "save_provider":
            entry = obj.get("entry", {})
            stored = providers_mod.save_provider(entry)
            if obj.get("key"):
                providers_mod.set_key(stored["id"], obj["key"])
            if not providers_mod.get_active_id():
                providers_mod.set_active(stored["id"])
            return {
                "reply": op,
                "req_id": req_id,
                "ok": True,
                "data": providers_mod.get_provider(stored["id"]),
            }
    if op == "delete_provider":
        target = obj.get("id", "")
        ok = (
            providers_mod.delete_group(target)
            if obj.get("scope") == "provider"
            else providers_mod.delete_provider(target)
        )
        return {"reply": op, "req_id": req_id, "ok": True, "data": bool(ok)}
    if op == "test_provider":
        ok, detail = providers_mod.test_provider(obj.get("id", ""))
        return {"reply": op, "req_id": req_id, "ok": ok, "data": detail}
    if op == "migrate_keys":
        try:
            count = providers_mod.migrate_from_settings()
            return {"reply": op, "req_id": req_id, "ok": True, "data": count}
        except Exception as failure:
            return {
                "reply": op,
                "req_id": req_id,
                "ok": False,
                "error": f"{type(failure).__name__}: {failure}",
            }
    if op == "get_context":
        info = git_status(paths_mod.project_root())
        info.update(
            {
                "project": paths_mod.project_name(),
                "project_root": str(paths_mod.project_root()),
            }
        )
        return {"reply": op, "req_id": req_id, "ok": True, "data": info}
    return {"reply": op, "req_id": req_id, "ok": False, "error": f"unknown op: {op}"}


import contextlib

from . import paths as paths_mod
from . import permissions as permissions_mod
from . import providers as providers_mod
from . import session as session_mod
from . import todos as todos_mod

_BACKEND: "Harness | None" = None


class Session:
    """One logical chat: a workspace root + an active session on disk.

    A tab in the UI maps to one of these. It owns its Runner, its permission
    mode, its chosen model/effort, and its pending approval/pick requests. All
    the shared backend globals (events routing, current session id, project
    root, provider, permission mode) are scoped to this object while its worker
    thread runs, so many Sessions live in one process without clobbering each
    other.
    """

    def __init__(self, key, backend):
        self.key = key
        self.backend = backend
        self.root: Path | None = None
        self.session_id: str | None = None
        self.written = 0
        self.runner = None
        self.lock = threading.Lock()
        self.running = False
        self.cancel = threading.Event()
        self.pending: dict = {}
        self._seq = 0
        self._submissions: dict = {}
        self._workers: list = []
        self.mode = "build"  # workflow: build / ask / plan / plan-build
        self.perms = "default"  # permission policy: default/strict/...
        self._mode_ref = permissions_mod.mode_ref(
            self.perms
        )  # live, changeable mid-turn
        self.effort = "default"
        self.active_provider: str | None = None

    # ---- transport ----
    def _send(self, body):
        body = dict(body)
        body["session"] = self.key
        try:
            self.backend.send(body)
        except Exception:
            pass

    def on_event(self, type, payload):
        self._send({"event": type, "payload": dict(payload)})

    def on_interact(self, kind, payload):
        if kind not in (events.APPROVAL, events.PICK):
            return False if kind == events.APPROVAL else None
        self._seq += 1
        rid = f"{self.key}#{self._seq}"
        box: dict = {}
        gate = threading.Event()
        self.pending[rid] = (box, gate)
        try:
            body = {"request": kind, "id": rid}
            body.update(payload or {})
            self._send(body)
            gate.wait(timeout=300)
            return box.get("result", False if kind == events.APPROVAL else None)
        finally:
            self.pending.pop(rid, None)

    def reply(self, rid, result):
        entry = self.pending.get(rid)
        if entry is None:
            return False
        box, gate = entry
        box["result"] = result
        gate.set()
        return True

    # ---- per-session context for worker threads ----
    @contextlib.contextmanager
    def bound(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(events.attached(self))
            if self.root is not None:
                stack.enter_context(paths_mod.rooted(self.root))
            stack.enter_context(permissions_mod.moded(self._mode_ref))
            stack.enter_context(providers_mod.scoped(self.active_provider, self.effort))
            stack.enter_context(
                session_mod.scoped(self.root, self.session_id, self.written)
            )
            # The plan list is per session: inherit the ambient list at bind
            # time, then diverge, so concurrent tabs never cross writes.
            stack.enter_context(todos_mod.scoped())
            yield

    @contextlib.contextmanager
    def workspace(self):
        if self.root is None:
            yield
            return
        with paths_mod.rooted(self.root):
            yield

    def ensure_runner(self, mode):
        from .agent import Runner

        if self.runner is None:
            self.runner = Runner(stream=True, cancel_event=self.cancel, mode=mode)
        else:
            self.runner.mode = mode

    def _thread(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def track(self, thread):
        """Remember a background worker so ``join_background`` can wait."""
        with self.lock:
            self._workers.append(thread)

    def join_background(self, timeout=10):
        """Wait for this session's background workers (titles) to finish.

        The server keeps no other loose threads per session, so this is what a
        shutdown or a test teardown needs before touching the files under it.
        A worker registered a moment ago may not have started yet (track runs
        just before start), so wait for it to start instead of raising - and
        one broken entry must never abandon the rest.
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        with self.lock:
            workers = list(self._workers)
            self._workers.clear()
        for worker in workers:
            try:
                while worker.ident is None and _time.monotonic() < deadline:
                    _time.sleep(0.01)
                if worker.ident is not None:
                    worker.join(timeout=max(0.0, deadline - _time.monotonic()))
            except RuntimeError:
                continue

    # ---- actions ----
    def stop(self):
        self.cancel.set()

    def _seen_submission(self, submission_id):
        """Exactly-once guard, keyed by submission id and never by text."""
        if not submission_id:
            return False
        if submission_id in self._submissions:
            return True
        self._submissions[submission_id] = True
        if len(self._submissions) > 32:
            self._submissions.pop(next(iter(self._submissions)))
        return False

    def submit(self, text, mode, submission_id=None):
        with self.lock:
            if self._seen_submission(submission_id):
                self.on_event(events.NOTE, {"text": "that submission was already sent"})
                return False
            if self.running:
                self.on_event(
                    events.NOTE, {"text": "already running - press Stop first"}
                )
                return False
            self.running = True
        self.cancel.clear()
        self._thread(self._turn, text, mode)
        return True

    def _materialise(self):
        """Create this tab's session once, and hand back the new id.

        A brand-new tab has no session until the user actually sends a prompt,
        so opening it never writes a session file. Creating it here, under the
        session lock and through session.fresh_id (which reserves the file),
        means a repeated or raced first prompt attaches to the same session
        instead of starting a second one.
        """
        with self.lock:
            if self.session_id:
                return None
            if self.root is None:
                return None
            with paths_mod.rooted(self.root):
                new_id = session_mod.fresh_id()
                session_mod.blank(new_id)
            self.session_id = new_id
            self.written = 0
            return new_id

    def _turn(self, text, mode):
        try:
            # Materialise INSIDE the try: a failing first prompt (read-only
            # sessions dir, full disk, AV lock) must surface as an error card
            # and a done event, not a thread that dies before `running` is ever
            # cleared - that wedged the tab on "already running" forever.
            new_id = self._materialise()
            with self.bound():
                if new_id:
                    # Tell the frontend which session this tab now owns, so the
                    # tab, the sidebar and the stored metadata all agree.
                    self.on_event(
                        events.SESSION, {"label": "new", "messages": [], "id": new_id}
                    )
                self.ensure_runner(mode)
                self.runner.handle_input(text)
                # Keep the append cursor in step with what was just written.
                # Losing it made the next turn re-save the whole prefix, which
                # is how a transcript ended up holding its opening turn twice.
                self.written = session_mod.written()
        except Exception as failure:  # noqa: BLE001 - report, don't kill the session
            self.on_event(
                events.ERROR, {"text": f"{type(failure).__name__}: {failure}"}
            )
        finally:
            with self.lock:
                self.running = False
            self._send({"event": "done", "payload": {}})
            self._maybe_title()

    def open_chat(self, session_id, mode):
        with self.lock:
            if self.running:
                # Opening swaps session_id/written/runner; doing that under a
                # running turn desynced the append cursor and dropped turns.
                self.on_event(
                    events.NOTE,
                    {"text": "a turn is running - open the session after it finishes"},
                )
                return
        self._thread(self._open, session_id, mode)

    def _open(self, session_id, mode):
        from . import history

        with self.bound():
            if session_id and session_mod.path_for(session_id).exists():
                messages = session_mod.open_session(session_id)
                history.strip(messages)
                self.session_id = session_mod.current_id()
                self.written = session_mod.written()
                self.runner = self._new_runner()
                self.runner.messages = messages
                self.runner.mode = mode
                label = "opened"
            else:
                # Nothing to open: no id at all (a workspace with no sessions
                # yet), or an id whose file is gone (a session deleted while a
                # tab still remembered it). Stay a draft and write nothing - a
                # session file belongs to the first prompt. This branch used to
                # materialise an empty session, which is how unused "New
                # Session" rows appeared in the sidebar and came back on every
                # restart.
                self.session_id = None
                self.written = 0
                self.runner = self._new_runner(mode=mode or "build")
                label = "new"
            self.on_event(
                events.SESSION,
                {
                    "label": label,
                    "messages": self.runner.messages,
                    "id": self.session_id,
                },
            )
        self._maybe_title()

    def _new_runner(self, mode="build"):
        from .agent import Runner

        return Runner(stream=True, cancel_event=self.cancel, mode=mode)

    def resubmit(self, count, text, mode, restore_files=True):
        with self.lock:
            if self.running:
                self.on_event(
                    events.NOTE, {"text": "already running - press Stop first"}
                )
                return False
            self.running = True
        self.cancel.clear()
        self._thread(self._resend, count, text, mode, restore_files)
        return True

    def _resend(self, count, text, mode, restore_files=True):
        try:
            with self.bound():
                self.ensure_runner(mode)
                try:
                    count = max(0, min(int(count), len(self.runner.messages)))
                except (TypeError, ValueError):
                    count = len(self.runner.messages)
                if restore_files:
                    checkpoints.restore_since(self.session_id, count)
                session_mod.rewind_to(count)
                self.written = count
                self.runner.messages = self.runner.messages[:count]
                self.on_event(
                    events.SESSION,
                    {
                        "label": "rewound",
                        "messages": self.runner.messages,
                        "id": self.session_id,
                    },
                )
            with self.bound():
                self.runner.handle_input(text)
                self.written = session_mod.written()
        except Exception as failure:  # noqa: BLE001
            self.on_event(
                events.ERROR, {"text": f"{type(failure).__name__}: {failure}"}
            )
        finally:
            with self.lock:
                self.running = False
            self._send({"event": "done", "payload": {}})
            self._maybe_title()

    def rewind_to(self, count):
        with self.lock:
            if self.running:
                return False, None
            if not self.session_id:
                # A tab that never opened a session has nothing to rewind, and
                # the code below would otherwise touch path_for(None) and kill
                # the shared socket.
                return False, None
            # Claim the running flag for the whole mutation: a submit slipping
            # in between the old check and the rewind ran a turn concurrently
            # with the truncation.
            self.running = True
        try:
            count = max(0, int(count))
            if self.runner is not None:
                count = min(count, len(self.runner.messages))
            with self.bound():
                restored = checkpoints.restore_since(self.session_id, count)
                session_mod.rewind_to(count)
                self.written = count
                if self.runner is not None:
                    self.runner.messages = self.runner.messages[:count]
                    messages = self.runner.messages
                else:
                    messages = session_mod.load(self.session_id)
                self.on_event(
                    events.SESSION,
                    {"label": "rewound", "messages": messages, "id": self.session_id},
                )
            return True, restored
        except (TypeError, ValueError):
            return False, None
        finally:
            with self.lock:
                self.running = False

    def rewind_preview(self, count):
        if not self.session_id:
            return []
        try:
            count = max(0, int(count))
        except (TypeError, ValueError):
            count = 0
        with self.bound():
            return checkpoints.preview(self.session_id, count)

    def list_sessions(self):
        with self.bound():
            return session_mod.all_sessions()

    def _maybe_title(self):
        def work():
            try:
                if not self.session_id:
                    return
                with self.bound():
                    messages = session_mod.load(self.session_id)
                    if session_mod._titles().get(self.session_id):
                        return
                    first = session_mod.title(messages)
                    if first in ("(empty)", "New Session", ""):
                        return
                    name = providers_mod.suggest_title(first)
                    if name and not session_mod._titles().get(self.session_id):
                        session_mod.set_title(self.session_id, name)
                        self.on_event("sessions_updated", {})
            except Exception:  # noqa: BLE001 - titles are best effort
                pass

        worker = threading.Thread(target=work, daemon=True)
        self.track(worker)
        worker.start()


class Harness:
    """The single backend service: owns all Sessions and the transport.

    One WebSocket connection drives every tab. Ops carry the session key so they
    reach the right Session; events/requests/replies carry it back so Electron
    can route them to the right tab.
    """

    def __init__(self):
        self._send = lambda body: None
        self.sessions: dict = {}
        self.lock = threading.Lock()

    def attach(self, send):
        self._send = send

    def send(self, body):
        self._send(body)

    def session(self, key) -> Session:
        with self.lock:
            s = self.sessions.get(key)
            if s is None:
                s = Session(key, self)
                self.sessions[key] = s
            return s

    def dispatch(self, obj):
        op = obj.get("op")
        req_id = obj.get("req_id")
        key = obj.get("session")

        def R(result):
            result = dict(result)
            result["req_id"] = req_id
            if key is not None:
                result["session"] = key
            self.send(result)

        if op in ("approval_reply", "pick_reply"):
            s = self.sessions.get(key) if key else None
            ok = (
                s.reply(
                    obj.get("id"),
                    obj.get("allow") if op == "approval_reply" else obj.get("index"),
                )
                if s
                else False
            )
            return R({"reply": op, "ok": bool(ok)})

        if op in (
            "list_providers",
            "save_provider",
            "delete_provider",
            "test_provider",
            "migrate_keys",
        ):
            return R(self._provider_op(op, obj))

        if op == "ws_sessions":
            workspace_root = obj.get("root") or ""
            if not workspace_root:
                return R({"reply": op, "ok": False, "error": "no root"})
            with paths_mod.rooted(workspace_root):
                data = {
                    "root": str(workspace_root),
                    "sessions": session_mod.all_sessions(),
                }
            return R({"reply": op, "ok": True, "data": data})

        if op in ("get_agents", "set_agents"):
            return R(self._agents_op(op, obj))

        if not key:
            return R({"reply": op, "ok": False, "error": "missing session"})
        s = self.session(key)
        root = obj.get("root")
        if root:
            s.root = Path(str(root)).resolve()

        if op == "open":
            s.open_chat(obj.get("id", ""), obj.get("mode", s.mode))
            return R({"reply": "open", "ok": True})
        if op == "submit":
            return R(
                {
                    "reply": "submit",
                    "ok": s.submit(
                        obj.get("text", ""),
                        obj.get("mode", s.mode),
                        submission_id=obj.get("submission_id"),
                    ),
                }
            )
        if op == "stop":
            s.stop()
            return R({"reply": "stop", "ok": True})
        if op == "list_sessions":
            return R({"reply": op, "ok": True, "data": s.list_sessions()})
        if op == "get_transcript":
            try:
                with s.workspace():
                    data = session_mod.load(obj.get("id", ""))
                return R({"reply": op, "ok": True, "data": data})
            except OSError as failure:
                return R({"reply": op, "ok": False, "error": str(failure)})
        if op == "get_plan":
            from . import plan as plan_mod

            with s.workspace():
                data = plan_mod.load(obj.get("id") or s.session_id or "")
            return R({"reply": op, "ok": True, "data": data})
        if op == "rename_session":
            with s.workspace():
                session_mod.set_title(
                    obj.get("id", ""), (obj.get("title", "") or "")[:60]
                )
            s.on_event("sessions_updated", {})
            return R({"reply": op, "ok": True})
        if op == "delete_session":
            sid = obj.get("id", "")
            with s.workspace():
                session_mod.delete_session(sid)
            checkpoints.delete(sid)
            s.on_event("sessions_updated", {})
            return R({"reply": op, "ok": True})
        if op == "rewind_preview":
            return R(
                {
                    "reply": op,
                    "ok": True,
                    "data": {
                        "count": obj.get("count", 0),
                        "changes": s.rewind_preview(obj.get("count", 0)),
                    },
                }
            )
        if op == "rewind_to":
            if s.running:
                return R(
                    {"reply": op, "ok": False, "error": "busy or invalid position"}
                )
            ok, restored = s.rewind_to(obj.get("count", 0))
            if ok:
                return R({"reply": op, "ok": True, "data": restored or []})
            return R({"reply": op, "ok": False, "error": "invalid position"})
        if op == "resend":
            return R(
                {
                    "reply": "resend",
                    "ok": s.resubmit(
                        obj.get("count", 0),
                        obj.get("text", ""),
                        obj.get("mode", s.mode),
                    ),
                }
            )
        if op == "get_context":
            info = (
                git_status(s.root)
                if s.root
                else {
                    "has_git": False,
                    "branch": "",
                    "default_branch": "",
                    "is_protected": False,
                }
            )
            info.update(
                {
                    "project": s.root.name if s.root else "",
                    "project_root": str(s.root or ""),
                }
            )
            return R({"reply": op, "ok": True, "data": info})
        if op == "git_status":
            return R(
                {
                    "reply": op,
                    "ok": True,
                    "data": git_status(s.root) if s.root else {"has_git": False},
                }
            )
        if op == "git_create_branch":
            if not s.root:
                return R({"reply": op, "ok": False, "error": "no workspace"})
            ok, detail = git_create_branch(s.root, obj.get("name", ""))
            return R(
                {"reply": op, "ok": ok, "data": detail}
                if ok
                else {"reply": op, "ok": False, "error": detail}
            )
        if op == "set_permissions":
            mode = obj.get("mode", "default")
            if mode in permissions_mod.MODES:
                s.perms = mode
                # Live: a turn already running will honour this on its next
                # permission check, so the user can relax or tighten mid-work.
                s._mode_ref.value = mode
            return R({"reply": op, "ok": True})
        if op == "set_active":
            ok = providers_mod.set_active(obj.get("id"))
            if ok:
                if s.running:
                    # Abandon the generation in flight instead of letting the
                    # rest of the turn keep writing on a model the user left.
                    s.stop()
                    s.on_event(
                        events.NOTE,
                        {"text": "model switched - the request in flight was stopped"},
                    )
                s.active_provider = obj.get("id")
            return R({"reply": op, "ok": bool(ok)})
        if op == "set_effort":
            pid = s.active_provider or providers_mod.get_active_id() or obj.get("id")
            providers_mod.set_effort(pid, obj.get("effort") or None)
            s.effort = obj.get("effort") or "default"
            return R({"reply": op, "ok": True})
        if op == "retry":
            ok = s.resubmit(
                obj.get("count", 0),
                obj.get("text", ""),
                obj.get("mode", s.mode),
                restore_files=False,
            )
            return R({"reply": "retry", "ok": bool(ok)})
        return R({"reply": op, "ok": False, "error": f"unknown op: {op}"})

    def _agents_op(self, op, obj):
        from . import agents as agents_mod

        if op == "get_agents":
            return {"reply": op, "ok": True, "data": agents_mod.get()}
        if op == "set_agents":
            try:
                stored = agents_mod.save(obj.get("config") or {})
            except Exception as failure:  # noqa: BLE001 - report, never crash the UI
                return {
                    "reply": op,
                    "ok": False,
                    "error": f"{type(failure).__name__}: {failure}",
                }
            # Saved config is ambient: every session reads it fresh at its next
            # turn, so nothing needs to be pushed here. A running turn is left
            # alone - switching an agent mid-response would desync its plan.
            return {"reply": op, "ok": True, "data": stored}
        return {"reply": op, "ok": False, "error": "unknown op"}

    def _provider_op(self, op, obj):
        from . import providers as providers_mod

        if op == "list_providers":
            models = providers_mod.list_models()
            statuses = {m["id"]: providers_mod.provider_status(m["id"]) for m in models}
            return {
                "reply": op,
                "ok": True,
                "data": {
                    "groups": providers_mod.list_groups(),
                    "providers": models,
                    "active_provider_id": providers_mod.get_active_provider_id(),
                    "active_model_id": providers_mod.get_active_id(),
                    "statuses": statuses,
                    "migration": providers_mod.migration_report(),
                },
            }
        if op == "save_provider":
            entry = obj.get("entry", {})
            try:
                stored = providers_mod.save_provider(entry)
            except ValueError as conflict:
                # A duplicate model id is a user mistake, not a crash: report it
                # so the form can point at the field instead of dying.
                return {"reply": op, "ok": False, "error": str(conflict)}
            if obj.get("key"):
                providers_mod.set_key(stored["id"], obj["key"])
            if not providers_mod.get_active_id():
                providers_mod.set_active(stored["id"])
            return {
                "reply": op,
                "ok": True,
                "data": providers_mod.get_provider(stored["id"]),
            }
        if op == "delete_provider":
            target = obj.get("id", "")
            if obj.get("scope") == "provider":
                ok = providers_mod.delete_group(target)
            else:
                ok = providers_mod.delete_provider(target)
            return {"reply": op, "ok": True, "data": bool(ok)}
        if op == "test_provider":
            ok, detail = providers_mod.test_provider(obj.get("id", ""))
            return {"reply": op, "ok": ok, "data": detail}
        if op == "migrate_keys":
            try:
                count = providers_mod.migrate_from_settings()
                return {"reply": op, "ok": True, "data": count}
            except Exception as failure:  # noqa: BLE001
                return {
                    "reply": op,
                    "ok": False,
                    "error": f"{type(failure).__name__}: {failure}",
                }
        return {"reply": op, "ok": False, "error": "unknown op"}


def get_backend() -> Harness:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = Harness()
    return _BACKEND


async def serve(host="127.0.0.1", port=8765):
    import websockets

    backend = get_backend()

    async def handler(ws):
        loop = asyncio.get_running_loop()

        def send_json(obj):
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps(obj, ensure_ascii=False)), loop
            )

        backend.attach(send_json)
        try:
            await ws.send(
                json.dumps(
                    {
                        "ready": {
                            "project": paths_mod.project_name(),
                            "project_root": str(paths_mod.project_root()),
                        }
                    }
                )
            )
            async for raw in ws:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                try:
                    await loop.run_in_executor(None, backend.dispatch, obj)
                except Exception as failure:  # noqa: BLE001 - one bad op must
                    # never take down the shared socket: every tab loses its
                    # stream and any running turn's output with it.
                    try:
                        await ws.send(
                            json.dumps(
                                {
                                    "event": "error",
                                    "payload": {
                                        "text": f"{type(failure).__name__}: {failure}"
                                    },
                                }
                            )
                        )
                    except Exception:
                        pass
        finally:
            pass

    try:
        server = await websockets.serve(handler, host, port)
    except OSError as failure:
        raise SystemExit(
            f"cannot bind ws://{host}:{port} ({failure}). "
            "Another sidecar is already running - reuse it or stop it first."
        ) from None
    async with server:
        print(f"harness backend on ws://{host}:{port}", flush=True)
        await asyncio.Future()


def main(argv=None):

    parser = argparse.ArgumentParser(
        description="Harness backend service for Electron."
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--app-data-dir", default=None)
    args = parser.parse_args(argv)

    from . import config as config_mod
    from . import session as session_mod

    paths_mod.init(project_root=args.project_root, app_data_dir=args.app_data_dir)
    paths_mod.migrate_providers()
    paths_mod.migrate_sessions()
    config_mod.reload()
    session_mod.migrate_root_sessions()
    get_backend()
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":
    main()
