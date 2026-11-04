"""The subagent guest loop: what comes back, and what is announced.

task() is the engine a `task` tool call runs. The scripted fake LLM drives
the four shapes that matter - immediate answer, tool turn then answer, turn
budget exhausted, provider error - and each asserts the returned report and
the events a user would see.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import events, subagent


def llm_message(content, calls=()):
    call_list = [
        SimpleNamespace(
            id=f"call-{i}",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for i, (name, arguments) in enumerate(calls)
    ]
    message = SimpleNamespace(content=content, tool_calls=call_list)
    message.model_dump = lambda exclude_none=True: {
        "role": "assistant",
        "content": content,
    }
    return message


class SubagentCase(unittest.TestCase):
    def setUp(self):
        events.reset()
        self.seen = []
        events.subscribe(lambda kind, payload: self.seen.append((kind, payload)))
        self.addCleanup(events.reset)
        self.executed = []

        def execute(tool_call):
            name = tool_call.function.name
            self.executed.append(name)
            return {}, f"result of {name}"

        self.execute = execute
        self.schemas = [{"function": {"name": "bash"}}]

    def kinds(self):
        return [kind for kind, _ in self.seen]

    def test_an_immediate_answer_comes_back_verbatim(self):
        with mock.patch.object(
            subagent, "call_llm", return_value=(llm_message("the findings"), {"t": 1})
        ):
            report = subagent.task("find the bug", self.execute, self.schemas)
        self.assertEqual(report, "the findings")
        self.assertEqual(self.executed, [])
        self.assertIn(events.SUBAGENT, self.kinds())
        self.assertIn(events.USAGE, self.kinds())
        self.assertNotIn(events.TOOL_CALL, self.kinds())

    def test_a_tool_turn_runs_then_answers(self):
        replies = [
            (
                llm_message("looking...", calls=[("bash", '{"command": "ls"}')]),
                {"t": 1},
            ),
            (llm_message("found it"), {"t": 1}),
        ]
        with mock.patch.object(subagent, "call_llm", side_effect=replies):
            report = subagent.task("find the bug", self.execute, self.schemas)
        self.assertEqual(report, "found it")
        self.assertEqual(self.executed, ["bash"])
        self.assertEqual(self.kinds().count(events.TOOL_CALL), 1)
        self.assertEqual(self.kinds().count(events.TOOL_RESULT), 1)

    def test_the_turn_budget_stops_a_runner_with_a_partial_report(self):
        replies = [
            (
                llm_message(f"progress {i}", calls=[("bash", '{"command": "ls"}')]),
                {"t": 1},
            )
            for i in range(6)
        ]
        with (
            mock.patch.object(subagent, "call_llm", side_effect=replies),
            mock.patch.object(subagent, "MAX_TURNS", 2),
        ):
            report = subagent.task("find the bug", self.execute, self.schemas)
        self.assertIn("stopped after 2 turns", report)
        self.assertIn("progress 1", report)
        self.assertEqual(self.executed, ["bash", "bash"])

    def test_a_provider_error_is_reported_not_raised(self):
        with mock.patch.object(
            subagent, "call_llm", side_effect=ConnectionError("provider down")
        ):
            report = subagent.task("find the bug", self.execute, self.schemas)
        self.assertIn("error", report)
        self.assertIn(events.ERROR, self.kinds())


if __name__ == "__main__":
    unittest.main(verbosity=2)
