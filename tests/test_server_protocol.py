"""Sidecar protocol: ops dispatch and event forwarding, no socket needed."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import (
    events,
    paths,
    todos,
)
from harness.server import EngineService, handle_op


def fake_message(content=None, calls=()):
    tcs = [
        SimpleNamespace(
            id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a))
        )
        for i, (n, a) in enumerate(calls)
    ]
    dumped = {"role": "assistant", "content": content}
    return SimpleNamespace(
        content=content,
        tool_calls=tcs,
        model_dump=lambda exclude_none=True: dict(dumped),
    )


USAGE = {
    "prompt_tokens": 1,
    "completion_tokens": 1,
    "reasoning_tokens": None,
    "cached_tokens": None,
}


class ServerCase(unittest.TestCase):
    def setUp(self):
        events.reset()
        todos.TODOS[:] = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "appdata",
        )
        Path(self.tmp.name, "proj").mkdir(exist_ok=True)
        self.addCleanup(paths.reset)
        self.sent = []
        self.service = EngineService(self.sent.append)
        for name in ("save", "compacted", "rewind_to", "ensure_title"):
            mock.patch("harness.session." + name).start()
        mock.patch("harness.session.all_sessions", return_value=[]).start()
        mock.patch("harness.session.load", return_value=[]).start()
        self.llm = mock.patch("harness.agent.call_llm").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.service.shutdown()
        events.reset()
        todos.TODOS[:] = []

    def wait_done(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if any(m.get("event") == "done" for m in self.sent):
                return True
            time.sleep(0.05)
        return False

    def test_submit_streams_events_then_done(self):
        self.llm.return_value = (fake_message("hello-sidecar"), USAGE)
        reply = handle_op(
            self.service, {"op": "submit", "text": "hi", "mode": "build", "req_id": 1}
        )
        self.assertTrue(reply["ok"])
        self.assertTrue(self.wait_done())
        kinds = [m.get("event") for m in self.sent]
        self.assertIn(events.MESSAGE, kinds)
        self.assertIn("done", kinds)

    def test_second_submit_while_running_is_rejected(self):
        self.service.running = True
        reply = handle_op(self.service, {"op": "submit", "text": "hi", "req_id": 2})
        self.assertFalse(reply["ok"])

    def test_unknown_op_errors(self):
        reply = handle_op(self.service, {"op": "nope", "req_id": 3})
        self.assertFalse(reply["ok"])

    def test_get_context_reports_project_and_git(self):
        reply = handle_op(self.service, {"op": "get_context", "req_id": 4})
        self.assertTrue(reply["ok"])
        self.assertIn("project", reply["data"])
        for key in ("has_git", "branch", "default_branch", "is_protected"):
            self.assertIn(key, reply["data"])

    def test_git_status_without_repo(self):
        reply = handle_op(self.service, {"op": "git_status", "req_id": 5})
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["data"]["has_git"])
        self.assertFalse(reply["data"]["is_protected"])

    def test_rewind_to_truncates_runner(self):

        self.service.runner = SimpleNamespace(
            messages=[{"role": "user", "content": c} for c in "abcd"]
        )
        reply = handle_op(self.service, {"op": "rewind_to", "count": 2, "req_id": 6})
        self.assertTrue(reply["ok"])
        self.assertEqual(len(self.service.runner.messages), 2)
        kinds = [m.get("event") for m in self.sent]
        self.assertIn(events.SESSION, kinds)

    def test_rewind_to_rejected_while_running(self):
        self.service.running = True
        reply = handle_op(self.service, {"op": "rewind_to", "count": 1, "req_id": 7})
        self.assertFalse(reply["ok"])

    def test_resend_rewinds_then_turns(self):
        self.llm.return_value = (fake_message("redone"), USAGE)
        self.service.runner = SimpleNamespace(
            messages=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ],
            mode="build",
            handle_input=lambda text: self.service.runner.messages.append(
                {"role": "user", "content": text}
            ),
        )
        reply = handle_op(
            self.service, {"op": "resend", "count": 1, "text": "a2", "req_id": 8}
        )
        self.assertTrue(reply["ok"])
        self.assertTrue(self.wait_done())
        bodies = [
            m.get("content")
            for m in self.service.runner.messages
            if m.get("role") == "user"
        ]
        self.assertEqual(bodies, ["a", "a2"])

    def test_title_set_once_and_never_overwritten(self):
        from harness import session as session_mod

        with (
            mock.patch.object(session_mod, "_titles", return_value={}),
            mock.patch.object(
                session_mod,
                "load",
                return_value=[{"role": "user", "content": "do things"}],
            ),
            mock.patch(
                "harness.providers.suggest_title", return_value="First name"
            ) as suggest,
            mock.patch.object(session_mod, "set_title") as set_title,
        ):
            self.service._maybe_title()
            for _ in range(100):
                if set_title.called:
                    break
                time.sleep(0.05)
            self.assertTrue(set_title.called)
            self.assertEqual(suggest.call_count, 1)
        with (
            mock.patch.object(
                session_mod, "_titles", return_value={session_mod.CURRENT: "User name"}
            ),
            mock.patch("harness.providers.suggest_title") as suggest2,
            mock.patch.object(session_mod, "set_title") as set_title2,
        ):
            self.service._maybe_title()
            time.sleep(0.3)
            suggest2.assert_not_called()
            set_title2.assert_not_called()

    def test_approval_reply_unblocks_engine(self):
        import threading

        seen = []
        orig = self.service._send
        self.service._send = lambda obj: (seen.append(obj), orig(obj))

        outcome = {}

        def asker():
            outcome["value"] = self.service._respond(
                events.APPROVAL, {"reason": "run: echo hi"}
            )

        waiter = threading.Thread(target=asker, daemon=True)
        waiter.start()
        end = time.time() + 5
        req_id = None
        while time.time() < end and req_id is None:
            for obj in seen:
                if obj.get("request") == events.APPROVAL:
                    req_id = obj["id"]
                    break
            time.sleep(0.05)
        self.assertIsNotNone(req_id)
        reply = handle_op(
            self.service,
            {"op": "approval_reply", "id": req_id, "allow": False, "req_id": 9},
        )
        self.assertTrue(reply["ok"])
        waiter.join(timeout=5)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(outcome["value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
