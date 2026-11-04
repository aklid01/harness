"""Headless sidecar: Python engine plus WebSocket transport for the desktop shell.

The desktop shell spawns `uv run python -m harness.server` and talks to it over
localhost. The engine itself is unchanged - this file puts the event bus
(harness/events.py) on a socket, one session per client connection.
"""

import argparse
import asyncio
import json
import logging
import threading
from pathlib import Path

from . import checkpoints, events
from .gitops import git_create_branch, git_status
from .pending import Mailbox
from . import attachments as attachments_mod

log = logging.getLogger(__name__)


import contextlib

from . import paths as paths_mod
from . import permissions as permissions_mod
from . import providers as providers_mod
from . import session as session_mod
from . import todos as todos_mod
from .review.command import ReviewArgs

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
        self._send_failures = 0
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
        self.mailbox = Mailbox()
        self.mailbox.on_change = self._notify_pending
        self.review_cancel = None  # set while a review is running

    def _notify_pending(self, items):
        self.on_event(events.PENDING, {"items": items})

    # ---- transport ----
    def _send(self, body):
        body = dict(body)
        body["session"] = self.key
        try:
            self.backend.send(body)
            self._send_failures = 0
        except Exception as failure:
            # The socket is the only channel, so a failing send cannot be
            # reported on it; the sidecar log is where this stays visible.
            self._send_failures += 1
            if self._send_failures == 1 or self._send_failures % 100 == 0:
                log.warning(
                    "session %s: send failed (%s in a row): %s",
                    self.key,
                    self._send_failures,
                    failure,
                )

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

    def reply(self, rid: str, result) -> bool:
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
        self.runner.review_hook = self._review_request

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

    def submit(
        self,
        text: str,
        mode: str,
        submission_id: str | None = None,
        attachments: list | None = None,
    ) -> bool:
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
        # A new run supersedes a review that is still going: the card says so.
        self.cancel_review()
        self._thread(self._turn, text, mode, None, self._keep_attachments(attachments))
        return True

    def _keep_attachments(self, items):
        """The files of one message that fit the limit; the rest are named."""
        kept, dropped = attachments_mod.within_limit(items)
        for item in dropped:
            self.on_event(
                events.NOTE,
                {
                    "text": (
                        f"{item.get('name') or 'file'} was not attached - the "
                        f"limit is {attachments_mod.max_files_per_message()} "
                        "files per message"
                    )
                },
            )
        return kept

    def enqueue(self, text, kind, submission_id=None, attachments=None):
        """File a message against the running turn.

        A steer joins the conversation at the next model-call boundary; a
        queue item auto-runs when the turn ends well. Only exists while a
        turn is running - the idle path is a plain submit.
        """
        text = str(text or "").strip()
        kind = kind if kind in ("steer", "queue") else None
        with self.lock:
            if self._seen_submission(submission_id):
                self.on_event(events.NOTE, {"text": "that submission was already sent"})
                return False
            if not self.running:
                self.on_event(
                    events.NOTE, {"text": "nothing is running - send it normally"}
                )
                return False
            if not text or kind is None:
                self.on_event(events.NOTE, {"text": "empty message or unknown kind"})
                return False
            kept = self._keep_attachments(attachments)
            if self.mailbox.add(text, kind, kept) is None:
                self.on_event(
                    events.NOTE,
                    {"text": "too many pending messages - remove one first"},
                )
                return False
        return True

    def edit_pending(self, item_id, text):
        """Edit a pending message before it is sent; fails once it is gone."""
        text = str(text or "").strip()
        if not text:
            return False
        return self.mailbox.edit(str(item_id), text)

    def remove_pending(self, item_id):
        return self.mailbox.remove(str(item_id))

    def remove_pending_attachment(self, item_id, attachment_id):
        """Take one file back from a pending message.

        The file has not been sent yet, so it is still a staged upload and
        goes away with the removal.
        """
        for item in self.mailbox.items():
            if item["id"] != str(item_id):
                continue
            kept = []
            taken = None
            for meta in item.get("attachments") or []:
                if str(meta.get("id")) == str(attachment_id):
                    taken = meta
                else:
                    kept.append(meta)
            if taken is None:
                return False
            attachments_mod.discard(taken)
            return self.mailbox.set_attachments(item["id"], kept)
        return False

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

    def _turn(self, text, mode, via=None, attachments=None):
        crashed = None
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
                        events.SESSION,
                        {
                            "label": "new",
                            "messages": [],
                            "id": new_id,
                            "pending": self.mailbox.items(),
                            "review": self.review_state(),
                        },
                    )
                self.ensure_runner(mode)
                self.runner.mailbox = self.mailbox
                self.runner.handle_input(text, via=via, attachments=attachments)
                # Keep the append cursor in step with what was just written.
                # Losing it made the next turn re-save the whole prefix, which
                # is how a transcript ended up holding its opening turn twice.
                self.written = session_mod.written()
        except Exception as failure:  # noqa: BLE001 - report, don't kill the session
            crashed = failure
            self.on_event(
                events.ERROR, {"text": f"{type(failure).__name__}: {failure}"}
            )
        self._end_turn(mode, crashed)

    def _end_turn(self, mode, crashed=None):
        """Shared tail of a worker turn: classify the outcome, then either
        chain the next pending message or go idle.

        A provider error discards queued messages (steers stay pending); a
        manual stop keeps everything; success hands the mailbox's oldest
        message a fresh turn. `done` fires only when nothing follows, so a
        queue chain looks like one continuous busy spell to the UI. The whole
        decision runs under the session lock so an enqueue racing the turn's
        last moment either chains or is refused - never stranded idle.
        """
        with self.lock:
            if crashed is not None or getattr(self.runner, "turn_failed", False):
                self.mailbox.discard_queues()
                self.running = False
                idle = True
            elif self.cancel.is_set():
                self.running = False
                idle = True
            else:
                follow = self.mailbox.take_next()
                if follow is not None:
                    self.cancel.clear()
                    self._thread(
                        self._turn,
                        follow["text"],
                        mode,
                        follow["kind"],
                        follow.get("attachments"),
                    )
                    idle = False
                else:
                    self.running = False
                    idle = True
        if idle:
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
                    "pending": self.mailbox.items(),
                    "review": self.review_state(),
                },
            )
        self._maybe_title()

    def _new_runner(self, mode="build"):
        from .agent import Runner

        runner = Runner(stream=True, cancel_event=self.cancel, mode=mode)
        runner.review_hook = self._review_request
        return runner

    def resubmit(self, count, text, mode, restore_files=True, attachments=None):
        with self.lock:
            if self.running:
                self.on_event(
                    events.NOTE, {"text": "already running - press Stop first"}
                )
                return False
            self.running = True
        self.cancel.clear()
        self.cancel_review()
        self._thread(self._resend, count, text, mode, restore_files, attachments)
        return True

    def _resend(self, count, text, mode, restore_files=True, attachments=None):
        crashed = None
        try:
            with self.bound():
                self.ensure_runner(mode)
                self.runner.mailbox = self.mailbox
                try:
                    count = max(0, min(int(count), len(self.runner.messages)))
                except (TypeError, ValueError):
                    count = len(self.runner.messages)
                # The message being replaced keeps its files: a resend of a
                # message that carried attachments carries them again. Files
                # attached to the resend itself join them.
                carried = []
                if 0 <= count < len(self.runner.messages):
                    carried = self.runner.messages[count].get("attachments") or []
                carried = carried + list(attachments or [])
                if restore_files:
                    checkpoints.restore_since(self.session_id, count)
                session_mod.rewind_to(count)
                self.written = count
                self._mark_reviews_stale(count)
                self.runner.messages = self.runner.messages[:count]
                self.on_event(
                    events.SESSION,
                    {
                        "label": "rewound",
                        "messages": self.runner.messages,
                        "id": self.session_id,
                        "pending": self.mailbox.items(),
                    },
                )
            with self.bound():
                self.runner.handle_input(text, attachments=carried)
                self.written = session_mod.written()
        except Exception as failure:  # noqa: BLE001
            crashed = failure
            self.on_event(
                events.ERROR, {"text": f"{type(failure).__name__}: {failure}"}
            )
        self._end_turn(mode, crashed)

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
                self._mark_reviews_stale(count)
                if self.runner is not None:
                    self.runner.messages = self.runner.messages[:count]
                    messages = self.runner.messages
                else:
                    messages = session_mod.load(self.session_id)
                self.on_event(
                    events.SESSION,
                    {
                        "label": "rewound",
                        "messages": messages,
                        "id": self.session_id,
                        "pending": self.mailbox.items(),
                    },
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

    # ---- multi-model review ----

    def _review_request(self, args):
        """The hook the agent calls to start, or offer, a review."""
        args = args or {}
        if args.get("offer"):
            return self.offer_review()
        return self.start_review(
            args, triggered_by="auto" if args.get("auto") else "manual"
        )

    def offer_review(self):
        """Show the card with a button rather than spending money at once."""
        self.on_event(
            events.REVIEW,
            {
                "stage": "offer",
                "message": "the agent changed some files - run a multi-model review?",
            },
        )
        return True

    def cancel_review(self):
        """Stop the model calls of a review that is still running."""
        cancel = self.review_cancel
        if cancel is None:
            return False
        cancel.set()
        self.on_event(events.REVIEW, {"stage": "cancelled"})
        return True

    def start_review(self, args: ReviewArgs, triggered_by: str = "manual") -> None:
        """Run a review in the background. The agent never waits for it.

        The thread runs inside this session's bound context, so the events
        it emits reach this tab and the files it reads are this workspace.
        """
        from . import agent_roles as agents_mod
        from .review import engine as review_engine
        from .review import scope as review_scope

        args = dict(args or {})
        settings = dict(agents_mod.review_settings())
        if args.get("severity"):
            settings["severity_threshold"] = args["severity"]

        reviewers = None
        models = [m for m in (args.get("models") or []) if m]
        if models:
            reviewers = [
                {
                    "model_id": model_id,
                    "label": "",
                    "enabled": True,
                    "focus": args.get("focus") or "",
                }
                for model_id in models
            ]

        mode = args.get("scope") or "run"
        base = args.get("base") or ""
        if mode == "base" and not base:
            mode, base = review_scope.parse_scope("base:main")

        # A new review supersedes the one that is running: the card says so.
        self.cancel_review()

        cancel = threading.Event()
        self.review_cancel = cancel
        # A brand-new tab has no session until something is sent. A review
        # needs one to save its report against, so create it now.
        if not self.session_id:
            self._materialise()
        session_id = self.session_id or ""
        root = self.root
        # The caller may name the files to review: the automatic review names
        # the files THIS turn changed, while a review the user asked for
        # covers everything the agent has changed in this session.
        named = args.get("paths")
        if named is not None:
            only_paths = [str(item) for item in named]
        else:
            only_paths = (
                list(self.runner.touched) if self.runner is not None else None
            )
        force = bool(args.get("force"))
        want_json = bool(args.get("json"))
        message_index = session_mod.written()

        def work():
            try:
                with self.bound():

                    def emit(stage, payload):
                        body = {"stage": stage}
                        body.update(payload or {})
                        self.on_event(events.REVIEW, body)

                    scope = review_scope.detect_for_settings(
                        root,
                        settings,
                        mode=mode,
                        base=base,
                        only_paths=only_paths,
                        session_id=session_id,
                    )
                    document = review_engine.run(
                        root,
                        session_id,
                        scope=scope,
                        reviewers=reviewers,
                        settings=settings,
                        emit=emit,
                        cancel=cancel,
                        force=force,
                        message_index=message_index,
                        triggered_by=triggered_by,
                    )
                    if want_json and document:
                        # The script mode: the structured report, as JSON.
                        from .review import report as review_report

                        self.on_event(
                            events.NOTE, {"text": review_report.as_json(document)}
                        )
            except Exception as failure:  # noqa: BLE001 - the agent must not see this
                self.on_event(
                    events.REVIEW,
                    {
                        "stage": "failed",
                        "error": f"{type(failure).__name__}: {failure}",
                    },
                )
            finally:
                if self.review_cancel is cancel:
                    self.review_cancel = None

        worker = threading.Thread(target=work, daemon=True, name="harness-review")
        self.track(worker)
        worker.start()
        return True

    def _mark_reviews_stale(self, count):
        """A rewind throws work away, so a review of it no longer describes it.

        The report is kept - it may still be worth reading - but it is
        marked, and the card says the review is out of date.
        """
        from .review import report as review_report

        try:
            with self.workspace():
                stale = review_report.mark_stale(self.session_id, count)
        except OSError:
            stale = []
        for document in stale:
            self.on_event(events.REVIEW, {"stage": "stale", "report": document})

    def review_state(self):
        """The saved report for this session, if there is one."""
        from .review import report as review_report

        if not self.session_id:
            return None
        with self.workspace():
            return review_report.latest(self.session_id)

    def dismiss_finding(self, review_id, finding_id):
        """Mark one finding as dismissed and tell the card about it."""
        from .review import report as review_report

        with self.workspace():
            document = review_report.dismiss(self.session_id, review_id, finding_id)
        if document is None:
            return None
        self.on_event(events.REVIEW, {"stage": "updated", "report": document})
        return document

    def send_review_to_agent(self, review_id, finding_ids, apply=False):
        """Put the chosen findings in front of the agent and let it fix them.

        The findings arrive as a tool result, so they enter the context
        budget the way any tool output does: stubbed once the turn is over,
        and summarised if the history is compacted. The agent then fixes
        them with its ordinary tools, which means the ordinary permission
        check and the ordinary checkpoint. The card edits nothing itself.
        """
        from .review import report as review_report

        with self.workspace():
            document = review_report.load(self.session_id, review_id)
        if document is None or self.runner is None:
            return False
        wanted = [str(item) for item in (finding_ids or [])]
        findings = [
            item
            for item in document.get("findings") or []
            if not wanted or item.get("id") in wanted
        ]
        if not findings:
            return False

        body = review_report.summary(
            {
                "verdict_label": document.get("verdict_label"),
                "findings": findings,
                "scope": document.get("scope"),
            }
        )
        path = review_report.path_for(self.session_id, review_id)
        if path:
            body += f"\n\nThe full report is at {path} - read it if you need more."
        if len(body) > 8000:
            body = body[:8000] + "\n... (the rest is in the saved report)"

        self.runner.inject_tool_result("review_findings", body)
        ids = ", ".join(item.get("id", "") for item in findings)
        if apply and len(findings) == 1:
            text = (
                f"Apply the suggested fix for review finding {ids}. Make that "
                "one change, keep it minimal, and say what you changed."
            )
        else:
            text = (
                f"Address the review findings {ids}. Use the suggested fixes "
                "where they fit, and say which ones you did not apply and why."
            )
        return self.submit(text, self.mode)

    def _maybe_title(self):
        def work():
            try:
                if not self.session_id:
                    return
                with self.bound():
                    from . import llm as llm_mod

                    messages = session_mod.load(self.session_id)
                    if session_mod.get_title(self.session_id):
                        return
                    first = session_mod.title(messages)
                    if first in ("(empty)", "New Session", ""):
                        return
                    name = llm_mod.suggest_title(first)
                    if name and not session_mod.get_title(self.session_id):
                        session_mod.set_title(self.session_id, name)
                        self.on_event("sessions_updated", {})
            except Exception as failure:  # noqa: BLE001 - titles are best effort
                # Best effort does not mean invisible: the failure goes to the
                # transcript as a note, like the other background paths report.
                self.on_event(
                    events.NOTE,
                    {
                        "text": "automatic titling failed - "
                        f"{type(failure).__name__}: {failure}",
                    },
                )

        worker = threading.Thread(target=work, daemon=True)
        self.track(worker)
        worker.start()


class Harness:
    """The single backend service: owns all Sessions and the transport.

    One WebSocket connection drives every tab. Ops carry the session key so they
    reach the right Session; events/requests/replies carry it back so the shell
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

    def dispatch(self, obj: dict) -> None:
        op = obj.get("op")
        req_id = obj.get("req_id")
        key = obj.get("session")

        def _reply(result):
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
            return _reply({"reply": op, "ok": bool(ok)})

        if op in (
            "list_providers",
            "save_provider",
            "delete_provider",
            "test_provider",
            "fetch_model_info",
            "migrate_keys",
        ):
            return _reply(self._provider_op(op, obj))

        if op == "ws_sessions":
            workspace_root = obj.get("root") or ""
            if not workspace_root:
                return _reply({"reply": op, "ok": False, "error": "no root"})
            with paths_mod.rooted(workspace_root):
                data = {
                    "root": str(workspace_root),
                    "sessions": session_mod.all_sessions(),
                }
            return _reply({"reply": op, "ok": True, "data": data})

        if op in ("get_agents", "set_agents"):
            return _reply(self._agents_op(op, obj))

        if not key:
            return _reply({"reply": op, "ok": False, "error": "missing session"})
        s = self.session(key)
        root = obj.get("root")
        if root:
            s.root = Path(str(root)).resolve()

        if op == "open":
            s.open_chat(obj.get("id", ""), obj.get("mode", s.mode))
            return _reply({"reply": "open", "ok": True})
        if op in ("attach_begin", "attach_part", "attach_finish", "attach_discard"):
            return _reply(self._attachment_op(op, obj, key))
        if op == "submit":
            return _reply(
                {
                    "reply": "submit",
                    "ok": s.submit(
                        obj.get("text", ""),
                        obj.get("mode", s.mode),
                        submission_id=obj.get("submission_id"),
                        attachments=obj.get("attachments"),
                    ),
                }
            )
        if op == "enqueue":
            return _reply(
                {
                    "reply": "enqueue",
                    "ok": s.enqueue(
                        obj.get("text", ""),
                        obj.get("kind", "queue"),
                        submission_id=obj.get("submission_id"),
                        attachments=obj.get("attachments"),
                    ),
                }
            )
        if op == "pending_edit":
            return _reply(
                {
                    "reply": "pending_edit",
                    "ok": s.edit_pending(obj.get("id", ""), obj.get("text", "")),
                }
            )
        if op == "pending_remove":
            return _reply(
                {"reply": "pending_remove", "ok": s.remove_pending(obj.get("id", ""))}
            )
        if op == "pending_remove_attachment":
            return _reply(
                {
                    "reply": "pending_remove_attachment",
                    "ok": s.remove_pending_attachment(
                        obj.get("id", ""), obj.get("attachment_id", "")
                    ),
                }
            )
        if op == "stop":
            s.stop()
            return _reply({"reply": "stop", "ok": True})
        if op == "list_sessions":
            return _reply({"reply": op, "ok": True, "data": s.list_sessions()})
        if op == "get_transcript":
            try:
                with s.workspace():
                    data = session_mod.load(obj.get("id", ""))
                return _reply({"reply": op, "ok": True, "data": data})
            except OSError as failure:
                return _reply({"reply": op, "ok": False, "error": str(failure)})
        if op == "get_plan":
            from . import plan as plan_mod

            with s.workspace():
                data = plan_mod.load(obj.get("id") or s.session_id or "")
            return _reply({"reply": op, "ok": True, "data": data})
        if op == "rename_session":
            with s.workspace():
                session_mod.set_title(
                    obj.get("id", ""), (obj.get("title", "") or "")[:60]
                )
            s.on_event("sessions_updated", {})
            return _reply({"reply": op, "ok": True})
        if op == "delete_session":
            sid = obj.get("id", "")
            with s.workspace():
                session_mod.delete_session(sid)
            checkpoints.delete(sid)
            # The staged files go with the session: they are its files, and
            # nothing else may reach them.
            attachments_mod.cleanup_session(sid, s.root)
            s.on_event("sessions_updated", {})
            return _reply({"reply": op, "ok": True})
        if op == "rewind_preview":
            return _reply(
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
                return _reply(
                    {"reply": op, "ok": False, "error": "busy or invalid position"}
                )
            ok, restored = s.rewind_to(obj.get("count", 0))
            if ok:
                return _reply({"reply": op, "ok": True, "data": restored or []})
            return _reply({"reply": op, "ok": False, "error": "invalid position"})
        if op == "resend":
            return _reply(
                {
                    "reply": "resend",
                    "ok": s.resubmit(
                        obj.get("count", 0),
                        obj.get("text", ""),
                        obj.get("mode", s.mode),
                        attachments=obj.get("attachments"),
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
            return _reply({"reply": op, "ok": True, "data": info})
        if op == "git_status":
            return _reply(
                {
                    "reply": op,
                    "ok": True,
                    "data": git_status(s.root) if s.root else {"has_git": False},
                }
            )
        if op == "git_create_branch":
            if not s.root:
                return _reply({"reply": op, "ok": False, "error": "no workspace"})
            ok, detail = git_create_branch(s.root, obj.get("name", ""))
            return _reply(
                {"reply": op, "ok": ok, "data": detail}
                if ok
                else {"reply": op, "ok": False, "error": detail}
            )
        if op == "set_permissions":
            mode = obj.get("mode", "default")
            if mode not in permissions_mod.MODES:
                return _reply(
                    {
                        "reply": op,
                        "ok": False,
                        "error": f"unknown permission mode: {mode} - use one of "
                        + ", ".join(permissions_mod.MODES),
                    }
                )
            s.perms = mode
            # Live: a turn already running will honour this on its next
            # permission check, so the user can relax or tighten mid-work.
            s._mode_ref.value = mode
            return _reply({"reply": op, "ok": True})
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
            return _reply({"reply": op, "ok": bool(ok)})
        if op == "set_effort":
            pid = s.active_provider or providers_mod.get_active_model_id() or obj.get("id")
            providers_mod.set_effort(pid, obj.get("effort") or None)
            s.effort = obj.get("effort") or "default"
            return _reply({"reply": op, "ok": True})
        if op == "retry":
            ok = s.resubmit(
                obj.get("count", 0),
                obj.get("text", ""),
                obj.get("mode", s.mode),
                restore_files=False,
            )
            return _reply({"reply": "retry", "ok": bool(ok)})
        if op in (
            "review_start",
            "review_cancel",
            "review_action",
            "review_report",
        ):
            return _reply(self._review_op(op, obj, s))
        return _reply({"reply": op, "ok": False, "error": f"unknown op: {op}"})

    def _review_op(self, op, obj, s):
        """The card's actions: start, cancel, dismiss, send to the agent.

        None of these edit a file. "Send to the agent" hands the findings to
        the agent, which then fixes them with its ordinary tools - so the
        permission check and the checkpoint both still apply.
        """
        if op == "review_report":
            with s.workspace():
                document = s.review_state()
            return {"reply": op, "ok": True, "data": document}

        if op == "review_cancel":
            cancelled = s.cancel_review()
            return {"reply": op, "ok": bool(cancelled)}

        if op == "review_start":
            from .review import command as review_command

            # The card sends its own preset plus top-level overrides; only
            # keys of the shared request shape survive, anything else the
            # renderer adds is dropped rather than passed on blind.
            preset = obj.get("args") or {}
            args = review_command.new_args(
                scope=preset.get("scope") or "run",
                base=preset.get("base") or "",
                json=bool(preset.get("json")),
                paths=preset.get("paths"),
                models=list(obj.get("models") or []),
                focus=obj.get("focus") or "",
                severity=obj.get("severity") or "",
                force=bool(obj.get("force")),
            )
            started = s.start_review(args, triggered_by="manual")
            return {"reply": op, "ok": bool(started)}

        action = obj.get("action") or ""
        review_id = obj.get("review_id") or ""
        finding_ids = obj.get("finding_ids") or []
        if action == "dismiss":
            document = s.dismiss_finding(review_id, obj.get("finding_id") or "")
            return {"reply": op, "ok": document is not None}
        if action == "send":
            ok = s.send_review_to_agent(
                review_id, finding_ids, apply=bool(obj.get("apply"))
            )
            return {"reply": op, "ok": bool(ok)}
        return {"reply": op, "ok": False, "error": f"unknown review action: {action}"}

    def _attachment_op(self, op, obj, key):
        """Stage one file the user attached.

        The bytes arrive in chunks, because a websocket frame is far smaller
        than a file can be. The per-file limit is checked before the first
        chunk, so an oversize file is refused with its name and the limit
        rather than uploaded and then dropped.
        """
        try:
            if op == "attach_begin":
                record = attachments_mod.begin(
                    key,
                    obj.get("name") or "file",
                    obj.get("size") or 0,
                    obj.get("type") or "application/octet-stream",
                )
                return {
                    "reply": op,
                    "ok": True,
                    "data": {
                        "id": record["id"],
                        "max_bytes": attachments_mod.max_file_bytes(),
                        "chunk_bytes": attachments_mod.CHUNK_BYTES,
                    },
                }
            if op == "attach_part":
                written = attachments_mod.part(obj.get("id"), obj.get("data"))
                return {"reply": op, "ok": True, "data": {"written": written}}
            if op == "attach_finish":
                return {
                    "reply": op,
                    "ok": True,
                    "data": {"attachment": attachments_mod.finish(obj.get("id"))},
                }
            attachments_mod.abandon(obj.get("id"))
            return {"reply": op, "ok": True}
        except attachments_mod.TooBig as failure:
            return {"reply": op, "ok": False, "error": str(failure)}
        except attachments_mod.Missing as failure:
            return {"reply": op, "ok": False, "error": str(failure)}

    def _agents_op(self, op, obj):
        from . import agent_roles as agents_mod

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
                    "active_model_id": providers_mod.get_active_model_id(),
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
            if not providers_mod.get_active_model_id():
                providers_mod.set_active(stored["id"])
            # Backstop auto-config: fill whatever the form left empty from the
            # provider's /models endpoint. Best effort; never blocks the save.
            info = providers_mod.autoconfigure_from_models(stored["id"])
            return {
                "reply": op,
                "ok": True,
                "data": providers_mod.get_provider(stored["id"]),
                "info": info,
            }
        if op == "fetch_model_info":
            info = providers_mod.fetch_model_config(
                model=obj.get("model", ""),
                provider_id=obj.get("provider_id") or None,
                base_url=obj.get("base_url") or None,
                key=obj.get("key") or None,
            )
            return {"reply": op, "ok": True, "data": info}
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


async def serve(host="127.0.0.1", port=8765, auth_token=None):
    import websockets

    backend = get_backend()

    async def handler(ws):
        loop = asyncio.get_running_loop()
        send_failures = 0

        if auth_token:
            # The loopback socket would otherwise answer any local process -
            # or any web page, which may open websockets to 127.0.0.1. The
            # shell that spawned us knows a one-time token; until a connection
            # presents it, nothing is sent and no op is dispatched. A wrong
            # token is closed silently: with 128 bits there is nothing to
            # guess and no oracle to help a guesser.
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            except (asyncio.TimeoutError, ValueError, OSError):
                return
            if not (isinstance(first, dict) and first.get("auth") == auth_token):
                return

        def send_json(obj):
            future = asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps(obj, ensure_ascii=False)), loop
            )

            def observe(done):
                nonlocal send_failures
                failure = done.exception()
                if failure is None:
                    send_failures = 0
                    return
                send_failures += 1
                # The socket is the only channel, so a broken send cannot be
                # reported on itself. The sidecar log is the one place a dead
                # transport is still visible; log the first failure and then
                # every hundredth so a dead socket cannot flood it.
                if send_failures == 1 or send_failures % 100 == 0:
                    log.warning(
                        "transport send failed (%s in a row): %s",
                        send_failures,
                        failure,
                    )

            future.add_done_callback(observe)

        backend.attach(send_json)
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
                except (websockets.exceptions.ConnectionClosed, OSError):
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
        description="Harness backend service for the desktop shell."
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--app-data-dir", default=None)
    parser.add_argument(
        "--auth-token",
        default=None,
        help="one-time token the shell passes; required as the first frame on "
        "every connection. Omitted only by dev/test runs.",
    )
    args = parser.parse_args(argv)

    from . import config as config_mod
    from . import session as session_mod

    paths_mod.init(project_root=args.project_root, app_data_dir=args.app_data_dir)
    paths_mod.migrate_providers()
    paths_mod.migrate_sessions()
    config_mod.reload()
    session_mod.migrate_root_sessions()
    # A half-finished upload from the last run is worthless: nobody can send
    # it any more, and it must not outlive the process that owned it.
    attachments_mod.clear_staging()
    get_backend()
    asyncio.run(serve(args.host, args.port, auth_token=args.auth_token))


if __name__ == "__main__":
    main()
