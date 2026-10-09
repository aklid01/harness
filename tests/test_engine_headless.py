"""Phase 1 proof: the engine runs with no TUI attached.

No harness.ui / harness.prompt / prompt_toolkit import, events carry the
turn, approval pauses and resumes via the responder, picks resolve via the
responder. stdlib unittest only: `uv run python tests/test_engine_headless.py`.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import commands, events  # noqa: E402
from harness.agent import Runner  # noqa: E402
from harness import todos  # noqa: E402


def fake_message(content=None, calls=()):
    tool_calls = [
        SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(calls)
    ]
    dumped = {"role": "assistant", "content": content}
    if tool_calls:
        dumped["tool_calls"] = [
            {"id": c.id, "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls
        ]
    return SimpleNamespace(content=content, tool_calls=tool_calls,
                           model_dump=lambda exclude_none=True: dict(dumped))


USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": None, "cached_tokens": None}


class EngineCase(unittest.TestCase):
    def setUp(self):
        events.reset()
        todos.TODOS[:] = []
        self.seen = []
        events.subscribe(lambda t, p: self.seen.append((t, p)))
        self.responder = None
        events.set_responder(lambda kind, payload: self.responder(kind, payload) if self.responder else None)
        for name in ("save", "compacted", "rewind_to", "ensure_title"):
            mock.patch("harness.session." + name).start()
        mock.patch("harness.session.all_sessions", return_value=[]).start()
        self.llm = mock.patch("harness.agent.call_llm").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        events.reset()
        todos.TODOS[:] = []

    def types(self):
        return [t for t, _ in self.seen]

    def test_no_tui_imports(self):
        runner = Runner()
        self.llm.return_value = (fake_message("hi"), USAGE)
        runner.handle_input("hello")
        for mod in ("harness.ui", "harness.prompt", "prompt_toolkit"):
            self.assertNotIn(mod, sys.modules, mod)
        self.assertIn(events.MESSAGE, self.types())

    def test_text_turn(self):
        runner = Runner()
        self.llm.return_value = (fake_message("done"), USAGE)
        runner.handle_input("hello")
        bodies = [p["content"] for t, p in self.seen if t == events.MESSAGE]
        self.assertEqual(bodies, ["hello", "done"])
        self.assertNotIn(events.TOOL_CALL, self.types())

    def test_tool_flow(self):
        runner = Runner()
        self.llm.side_effect = [
            (fake_message(None, [("bash", {"command": "echo hi"})]), USAGE),
            (fake_message("ran it"), USAGE),
        ]
        runner.handle_input("run echo")
        calls = [p for t, p in self.seen if t == events.TOOL_CALL]
        results = [p for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertEqual(len(calls), 1)
        self.assertIn("hi", results[0]["result"])
        tool_msgs = [m for m in runner.messages if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)

    def test_approval_deny_and_allow(self):
        ask = [("bash", {"command": 'python -c "print(1)"'})]
        answers = []
        runner = Runner()
        self.responder = lambda kind, payload: answers.append((kind, payload)) or False
        self.llm.side_effect = [(fake_message(None, ask), USAGE), (fake_message("ok"), USAGE)]
        runner.handle_input("ask me")
        kinds = [k for k, _ in answers]
        self.assertIn(events.APPROVAL, kinds)
        denied = [p for t, p in self.seen if t == events.TOOL_RESULT][0]["result"]
        self.assertIn("denied", denied)

        events.reset()
        events.subscribe(lambda t, p: self.seen.append((t, p)))
        events.set_responder(lambda kind, payload: True)
        mark = len(self.seen)
        runner2 = Runner()
        self.llm.side_effect = [(fake_message(None, ask), USAGE), (fake_message("ok"), USAGE)]
        runner2.handle_input("ask me")
        allowed = [p for t, p in self.seen[mark:] if t == events.TOOL_RESULT][0]["result"]
        self.assertIn("1", allowed)

    def test_no_responder_denies(self):
        events.set_responder(None)
        runner = Runner()
        self.llm.side_effect = [
            (fake_message(None, [("bash", {"command": 'python -c "print(1)"'})]), USAGE),
            (fake_message("ok"), USAGE),
        ]
        runner.handle_input("ask me")
        result = [p for t, p in self.seen if t == events.TOOL_RESULT][0]["result"]
        self.assertIn("denied", result)

    def test_pick_none_keeps_messages(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        self.responder = lambda kind, payload: None
        self.assertEqual(commands.handle("/rewind", list(msgs)), msgs)

    def test_unknown_command_help(self):
        msgs = [{"role": "system", "content": "s"}]
        self.assertEqual(commands.handle("/bogus", list(msgs)), msgs)
        notes = [p["text"] for t, p in self.seen if t == events.NOTE]
        self.assertTrue(any("/compact" in n for n in notes))

    def test_todos_event(self):
        plan = [{"content": "Do it", "activeForm": "Doing it", "status": "in_progress"}]
        runner = Runner()
        self.llm.side_effect = [
            (fake_message(None, [("write_todos", {"todos": plan})]), USAGE),
            (fake_message("planned"), USAGE),
        ]
        runner.handle_input("plan")
        plans = [p["todos"] for t, p in self.seen if t == events.TODOS]
        self.assertEqual(plans, [plan])

    def test_plan_mode_blocks_writes_before_execute(self):
        import tempfile
        from pathlib import Path

        target = str(Path(tempfile.gettempdir()) / "harness-plan-probe.txt")
        if Path(target).exists():
            Path(target).unlink()
        runner = Runner(mode="plan")
        self.llm.side_effect = [
            (fake_message(None, [("write_file", {"path": target, "content": "x"})]), USAGE),
            (fake_message("ok"), USAGE),
        ]
        with mock.patch("harness.agent.execute") as exe:
            exe.side_effect = AssertionError("must not execute writes in plan mode")
            runner.handle_input("write it")
        self.assertFalse(Path(target).exists())
        result = [p for t, p in self.seen if t == events.TOOL_RESULT][0]["result"]
        self.assertIn("Plan mode", result)

    def test_build_mode_still_executes(self):
        runner = Runner(mode="build")
        self.llm.side_effect = [
            (fake_message(None, [("bash", {"command": "echo build-ok"})]), USAGE),
            (fake_message("ok"), USAGE),
        ]
        runner.handle_input("run it")
        result = [p for t, p in self.seen if t == events.TOOL_RESULT][0]["result"]
        self.assertIn("build-ok", result)

    def test_model_is_told_plan_mode_upfront(self):
        runner = Runner(mode="plan")
        self.llm.side_effect = [(fake_message("here is my plan"), USAGE)]
        runner.handle_input("think")
        sent = self.llm.call_args[0][0]
        self.assertIn("Plan mode", sent[-1]["content"])

    def test_build_mode_sends_no_mode_tag(self):
        runner = Runner(mode="build")
        self.llm.side_effect = [(fake_message("ok"), USAGE)]
        runner.handle_input("hi")
        sent = self.llm.call_args[0][0]
        self.assertNotIn("<mode>", sent[-1]["content"])
        self.assertFalse(any("<system-reminder>" in (m.get("content") or "") for m in runner.messages))

    def test_fresh_runners_get_mode_identity(self):
        plan = Runner(mode="plan")
        build = Runner(mode="build")
        self.assertIn("planning assistant", plan.messages[0]["content"])
        self.assertIn("plan mode", plan.messages[0]["content"])
        self.assertIn("coding agent", build.messages[0]["content"])
        self.assertNotIn("plan mode", build.messages[0]["content"])

    def test_plan_notice_lands_in_transcript(self):
        from harness.agent import PLAN_NOTICE

        runner = Runner(mode="plan")
        self.llm.side_effect = [(fake_message("noted"), USAGE)]
        runner.handle_input("hi")
        self.assertIn({"role": "user", "content": PLAN_NOTICE}, runner.messages)
        sent = self.llm.call_args[0][0]
        contents = [m.get("content") for m in sent]
        self.assertLess(contents.index(PLAN_NOTICE), len(contents) - 1)

    def test_mode_switch_announces_both_ways(self):
        from harness.agent import BUILD_NOTICE, PLAN_NOTICE

        runner = Runner(mode="plan")
        self.llm.side_effect = [(fake_message("a"), USAGE), (fake_message("b"), USAGE)]
        runner.handle_input("one")
        runner.mode = "build"
        runner.handle_input("two")
        bodies = [m.get("content") for m in runner.messages]
        self.assertIn(PLAN_NOTICE, bodies)
        self.assertIn(BUILD_NOTICE, bodies)
        self.assertLess(bodies.index(PLAN_NOTICE), bodies.index(BUILD_NOTICE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
