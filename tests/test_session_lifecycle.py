"""Session lifecycle: one new session, one turn, one model request.

These are the regressions behind the ghost tabs and the doubled first turn:
a session id allocated per second is not unique when two tabs start a session
at the same moment, and a session that already exists gets a fresh runner
instead of being attached to.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("API_KEY", "dummy-key")
os.environ.setdefault("MODEL", "dummy-model")

from harness import events, paths
from harness import server as server_mod
from harness import session as session_mod

USAGE = {
    "prompt_tokens": 1,
    "completion_tokens": 1,
    "reasoning_tokens": None,
    "cached_tokens": None,
}


def reply(content):
    return SimpleNamespace(
        content=content,
        tool_calls=[],
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": content},
    )


class SessionCase(unittest.TestCase):
    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir(parents=True)
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()
        os.environ["HARNESS_SESSIONS_ROOT"] = str(self.sessions)
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        paths.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "appdata")
        self.addCleanup(paths.reset)
        self.backend = server_mod.Harness()
        self.sent = []
        self.seen = {}
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.calls = []
        patch = mock.patch("harness.agent.call_llm")
        self.llm = patch.start()
        self.addCleanup(patch.stop)
        self.llm.side_effect = self.answer

    def quiesce(self):
        """Wait out background title workers before the temp dir is removed."""
        for session in list(self.backend.sessions.values()):
            session.join_background(timeout=10)

    def answer(
        self, messages, tools=None, on_delta=None, on_reasoning=None, cancel=None
    ):
        self.calls.append([m for m in messages if m.get("role") == "user"])
        return reply("Hi! How can I help you today?"), USAGE

    # ---- helpers -------------------------------------------------------

    def dispatch(self, **obj):
        obj.setdefault("req_id", time.time_ns() % 1000000)
        self.backend.dispatch(obj)

    def dispatch_reply(self, **obj):
        """Dispatch and return the reply envelope the transport would see."""
        obj.setdefault("req_id", time.time_ns() % 1000000)
        reply: dict = {}
        original = self.backend.send
        self.backend.send = lambda body: (
            reply.update(body) if body.get("reply") else original(body)
        )
        try:
            self.backend.dispatch(obj)
        finally:
            self.backend.send = original
        return reply

    def seen_events(self, session, kind):
        return sum(
            1
            for m in self.sent
            if m.get("event") == kind and m.get("session") == session
        )

    def done(self, session, timeout=20, kind="done"):
        """Wait for the NEXT event of this kind for this session."""
        target = self.seen.get((session, kind), 0) + 1
        limit = time.time() + timeout
        while time.time() < limit:
            if self.seen_events(session, kind) >= target:
                self.seen[(session, kind)] = target
                return True
            time.sleep(0.02)
        return False

    def wait_for(self, condition, timeout=10, description="condition"):
        """Poll a condition to a deadline instead of betting on a sleep."""
        limit = time.time() + timeout
        while time.time() < limit:
            if condition():
                return True
            time.sleep(0.02)
        self.fail(f"timed out waiting for {description}")

    def files(self):
        return sorted(p.name for p in (paths.session_dir()).glob("*.jsonl"))

    def transcript(self, name):
        """Replay the on-disk log the way session.load does."""
        messages = []
        for line in (
            (paths.session_dir() / name).read_text(encoding="utf-8").splitlines()
        ):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "rewind_to" in entry:
                del messages[entry["rewind_to"] :]
            elif "compacted" in entry:
                messages = list(entry["compacted"])
            else:
                messages.append(entry)
        return messages

    def user_messages(self, name):
        return [m["content"] for m in self.transcript(name) if m.get("role") == "user"]

    def assistant_messages(self, name):
        return [
            m.get("content")
            for m in self.transcript(name)
            if m.get("role") == "assistant"
        ]

    def system_prompts(self, name):
        return [
            m
            for m in self.transcript(name)
            if m.get("role") == "system" and "coding agent" in str(m.get("content"))
        ]

    # ---- new session, first prompt -------------------------------------

    def test_opening_a_workspace_with_no_sessions_writes_nothing(self):
        """A tab opened before any prompt is a draft, not an empty session."""
        self.dispatch(op="open", session="t1", root=str(self.root))
        self.assertTrue(self.done("t1", kind="session"))
        self.assertEqual(
            self.files(), [], "opening a tab must not create a session file"
        )
        self.assertEqual(session_mod.all_sessions(), [])

    def test_opening_a_stale_session_id_writes_nothing(self):
        """A remembered session that was deleted must not come back empty."""
        self.dispatch(
            op="open", session="t1", id="20200101-000000", root=str(self.root)
        )
        self.assertTrue(self.done("t1", kind="session"))
        self.assertEqual(self.files(), [])

    def test_a_message_less_session_file_is_never_listed(self):
        target = paths.session_dir()
        target.mkdir(parents=True, exist_ok=True)
        (target / "20200101-000000.jsonl").write_text("", encoding="utf-8")
        self.assertEqual(
            session_mod.all_sessions(), [], "an unused file is not a session"
        )

    def test_first_prompt_makes_one_session_one_turn_one_request(self):
        self.dispatch(op="open", session="t1", root=str(self.root))
        self.assertTrue(self.done("t1", kind="session"))
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.assertTrue(self.done("t1"))

        self.assertEqual(
            len(self.files()), 1, f"expected one session file, got {self.files()}"
        )
        name = self.files()[0]
        self.assertEqual(self.user_messages(name), ["Hi"])
        self.assertEqual(
            self.assistant_messages(name), ["Hi! How can I help you today?"]
        )
        self.assertEqual(
            len(self.system_prompts(name)), 1, "one system prompt per session"
        )
        self.assertEqual(len(self.calls), 1, "one model request")

    def test_two_draft_tabs_starting_a_session_at_once_get_distinct_sessions(self):
        """The regression behind the app data: two first prompts in the same
        second both landed in one session file, doubling the first turn.

        A brand-new tab has no backend session until its first prompt, so both
        sessions are created by submit, at the same moment.
        """
        self.dispatch(
            op="submit", session="t1", root=str(self.root), text="Hi", mode="build"
        )
        self.dispatch(
            op="submit", session="t2", root=str(self.root), text="Hi", mode="build"
        )
        self.assertTrue(self.done("t1"))
        self.assertTrue(self.done("t2"))
        self.wait_for(lambda: len(self.files()) == 2, description="both session files")

        self.assertEqual(
            len(self.files()), 2, f"sessions must not share an id: {self.files()}"
        )
        for name in self.files():
            self.assertEqual(self.user_messages(name), ["Hi"], name)
            self.assertEqual(len(self.system_prompts(name)), 1, name)
        self.assertEqual(len(self.calls), 2)

    def test_rapid_double_submit_makes_one_turn(self):
        self.dispatch(op="open", session="t1", root=str(self.root))
        self.assertTrue(self.done("t1", kind="session"))
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.assertTrue(self.done("t1"))
        self.wait_for(lambda: len(self.files()) == 1, description="the session file")

        name = self.files()[0]
        self.assertEqual(
            self.user_messages(name), ["Hi"], "the message must be stored once"
        )
        self.assertEqual(len(self.calls), 1, "one model request for one message")

    def test_opening_an_existing_session_twice_keeps_one_runner(self):
        self.dispatch(op="open", session="t1", root=str(self.root))
        self.assertTrue(self.done("t1", kind="session"))
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.assertTrue(self.done("t1"))
        name = self.files()[0]
        sid = Path(name).stem

        self.dispatch(op="open", session="t2", id=sid, root=str(self.root))
        self.assertTrue(self.done("t2", kind="session"))
        self.dispatch(op="open", session="t3", id=sid, root=str(self.root))
        self.assertTrue(self.done("t3", kind="session"))

        self.dispatch(op="submit", session="t3", text="again", mode="build")
        self.assertTrue(self.done("t3"))
        self.assertEqual(len(self.files()), 1, "no second session file")
        self.assertEqual(self.user_messages(name), ["Hi", "again"])
        self.assertEqual(
            len(self.system_prompts(name)),
            1,
            "the resumed session keeps one system prompt",
        )

    def test_title_update_never_creates_a_session(self):
        self.dispatch(op="open", session="t1", root=str(self.root))
        self.assertTrue(self.done("t1", kind="session"))
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.assertTrue(self.done("t1"))
        name = self.files()[0]
        sid = Path(name).stem
        before = len(self.files())

        session_mod.set_title(sid, "Basic Greeting Response")

        self.assertEqual(
            len(self.files()), before, "a title is an update, not a create"
        )
        self.assertIn(
            sid, json.loads((paths.titles_file()).read_text(encoding="utf-8"))
        )

    def test_retry_after_a_failure_keeps_the_session(self):
        def boom(messages, tools=None, on_delta=None, on_reasoning=None, cancel=None):
            self.calls.append("attempt")
            raise RuntimeError("upstream exploded")

        self.llm.side_effect = boom
        self.dispatch(op="open", session="t1", root=str(self.root))
        self.assertTrue(self.done("t1", kind="session"))
        self.dispatch(op="submit", session="t1", text="Hi", mode="build")
        self.assertTrue(self.done("t1"))
        name = self.files()[0]

        self.assertEqual(self.user_messages(name), ["Hi"])
        self.assertEqual(self.assistant_messages(name), [])

        self.llm.side_effect = self.answer
        self.dispatch(op="retry", session="t1", count=1, text="Hi", mode="build")
        self.assertTrue(self.done("t1"))

        self.assertEqual(len(self.files()), 1, "no new session after a retry")
        self.assertEqual(
            self.user_messages(name), ["Hi"], "the message is not duplicated"
        )
        self.assertEqual(
            self.assistant_messages(name), ["Hi! How can I help you today?"]
        )

    def test_submission_id_makes_a_replayed_submit_a_no_op(self):
        self.dispatch(
            op="submit",
            session="t1",
            root=str(self.root),
            text="Hi",
            mode="build",
            submission_id="sub-1",
        )
        self.assertTrue(self.done("t1"))
        before = len(self.calls)

        # a replayed or duplicated frame for the same submission does nothing
        reply = self.dispatch_reply(
            op="submit",
            session="t1",
            root=str(self.root),
            text="Hi",
            mode="build",
            submission_id="sub-1",
        )
        self.assertFalse(reply["ok"], "a replayed submission is refused")
        self.assertTrue(
            any(
                "already sent" in str(m.get("payload", {}).get("text", ""))
                for m in self.sent
                if m.get("event") == "note"
            )
        )
        self.quiesce()
        self.assertEqual(
            len(self.calls), before, "no second turn for a replayed submit"
        )

        name = self.files()[0]
        self.assertEqual(self.user_messages(name), ["Hi"])

        # sending the same words again is a different submission, and runs
        self.dispatch(
            op="submit",
            session="t1",
            root=str(self.root),
            text="Hi",
            mode="build",
            submission_id="sub-2",
        )
        self.assertTrue(self.done("t1"))
        self.assertEqual(
            self.user_messages(name), ["Hi", "Hi"], "a repeat send is legitimate"
        )
        self.assertEqual(len(self.calls), before + 1)

    def test_first_submit_announces_the_new_session_to_the_tab(self):
        self.dispatch(
            op="submit", session="t1", root=str(self.root), text="Hi", mode="build"
        )
        self.assertTrue(self.done("t1"))
        announced = [
            m["payload"].get("id")
            for m in self.sent
            if m.get("event") == "session" and m.get("session") == "t1"
        ]
        self.assertEqual(
            announced,
            [Path(self.files()[0]).stem],
            "the tab must learn its session id from the first submit",
        )
        session = self.backend.sessions["t1"]
        self.assertEqual(session.session_id, announced[0])
        # a second submit attaches to that same session
        self.dispatch(
            op="submit", session="t1", root=str(self.root), text="more", mode="build"
        )
        self.assertTrue(self.done("t1"))
        self.assertEqual(session.session_id, announced[0])
        self.assertEqual(len(self.files()), 1)

    def test_interrupted_generation_leaves_the_session_intact(self):
        release = threading.Event()

        def slow(messages, tools=None, on_delta=None, on_reasoning=None, cancel=None):
            release.wait(5)
            return reply("late"), USAGE

        with mock.patch("harness.agent.call_llm", side_effect=slow):
            self.dispatch(
                op="submit", session="t1", root=str(self.root), text="Hi", mode="build"
            )
            self.wait_for(
                lambda: self.backend.sessions["t1"].running,
                description="the turn to start",
            )
            self.dispatch(op="stop", session="t1")
            release.set()
            self.assertTrue(self.done("t1"))

        name = self.files()[0]
        self.assertEqual(self.user_messages(name), ["Hi"])
        self.assertEqual(
            self.assistant_messages(name), [], "an abandoned turn is not stored"
        )
        # the session still accepts a fresh turn, with no duplicate
        self.llm.side_effect = self.answer
        self.dispatch(
            op="submit", session="t1", root=str(self.root), text="again", mode="build"
        )
        self.assertTrue(self.done("t1"))
        self.assertEqual(self.user_messages(name), ["Hi", "again"])
        self.assertEqual(len(self.files()), 1)

    if __name__ == "__main__":
        unittest.main(verbosity=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
