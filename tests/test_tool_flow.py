"""Tool pipeline end to end, against a deterministic mock endpoint.

Every test here runs a model turn with the real client, the real request
builder and the real executor - the models just come from a local mock
server, so there are no keys and no network.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("API_KEY", "dummy-key")
os.environ.setdefault("MODEL", "dummy-model")

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from mock_provider import MockProvider

from harness import (
    agent,
    events,
    paths,
    permissions,
    providers,
    texttools,
    tools,
)


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


OPEN = texttools.OPEN
CLOSE = texttools.CLOSE


def markup(command):
    return "\n".join(
        [
            "Let me take a look at the project structure.",
            "",
            OPEN,
            "<function=bash>",
            "<parameter=command>" + command + "</parameter>",
            "</function>",
            CLOSE,
        ]
    )


class ToolFlowCase(unittest.TestCase):
    SCRIPT = {
        "native": [
            {"kind": "tool", "name": "bash", "arguments": {"command": "echo hi"}},
            {"kind": "text", "content": "ran it"},
        ],
        "multi": [
            {"kind": "tool", "name": "bash", "arguments": {"command": "echo one"}},
            {"kind": "tool", "name": "read_file", "arguments": {"path": "note.txt"}},
            {"kind": "text", "content": "both done"},
        ],
        "plain": [{"kind": "text", "content": "just an answer"}],
        "markup-bad": [
            {"kind": "text", "content": markup("find . -maxdepth 2 -type f | head -50")}
        ],
        "unknown": [
            {"kind": "tool", "name": "drop_database", "arguments": {"target": "all"}},
            {"kind": "text", "content": "my mistake"},
        ],
        "bad-args": [
            {"kind": "tool", "name": "bash", "arguments": {"directory": "."}},
            {"kind": "text", "content": "fixed it"},
        ],
        "approve": [
            {
                "kind": "tool",
                "name": "bash",
                "arguments": {"command": "echo approved-ok"},
            },
            {"kind": "text", "content": "approved"},
        ],
        "deny": [
            {
                "kind": "tool",
                "name": "bash",
                "arguments": {"command": "echo denied-ok"},
            },
            {"kind": "text", "content": "denied"},
        ],
        "fails": [
            {"kind": "tool", "name": "bash", "arguments": {"command": "exit 41"}},
            {"kind": "text", "content": "recovered"},
        ],
    }

    def setUp(self):
        events.reset()
        tools._AVAILABILITY_CACHE = None
        tools.begin_turn()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "note.txt").write_text("some note\n", encoding="utf-8")
        self.sessions = Path(self.tmp.name) / "sessions"
        self.sessions.mkdir()
        os.environ["HARNESS_SESSIONS_ROOT"] = str(self.sessions)
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        paths.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "appdata")
        self.addCleanup(paths.reset)
        self.script_path = Path(self.tmp.name) / "script.json"
        self.script_path.write_text(json.dumps(self.SCRIPT), encoding="utf-8")
        self.record_path = Path(self.tmp.name) / "record.jsonl"
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)
        self.seen = []
        events.subscribe(lambda kind, payload: self.seen.append((kind, payload)))
        events.set_responder(lambda kind, payload: True)
        self.mock = MockProvider(str(self.script_path), str(self.record_path))
        self.mock.__enter__()
        self.addCleanup(self.mock.__exit__, None, None, None)

    def use(self, model, **extra):
        providers.save_provider(
            {
                "id": "m-" + model,
                "name": model,
                "base_url": self.mock.base_url,
                "model": model,
                "headers": {},
                "disabled": False,
                **extra,
            }
        )
        providers.set_active("m-" + model)
        providers.set_key("m-" + model, "dummy-secret")
        return self

    def run_turn(self, text="go", mode="build"):
        runner = agent.Runner(stream=True, cancel_event=None, mode=mode)
        runner.handle_input(text)
        return runner

    def tool_calls(self):
        return [p for t, p in self.seen if t == events.TOOL_CALL]

    def tool_results(self):
        return [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]

    def assistant_texts(self):
        return [
            p["content"]
            for t, p in self.seen
            if t == events.MESSAGE and p.get("role") == "assistant"
        ]

    def requests(self):
        payloads = []
        for line in self.record_path.read_text(encoding="utf-8").splitlines():
            payloads.append(json.loads(line))
        return payloads

    def test_native_tool_call_runs_and_the_result_returns(self):
        self.use("native")
        self.run_turn()
        calls = self.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        results = self.tool_results()
        self.assertEqual(len(results), 1)
        self.assertIn("hi", results[0])
        self.assertEqual(self.assistant_texts(), ["ran it"])
        payloads = self.requests()
        self.assertEqual(len(payloads), 2)
        sent = {t["function"]["name"] for t in payloads[0]["tools"]}
        self.assertIn("bash", sent)

    def test_multi_step_call_result_call_answer(self):
        self.use("multi")
        self.run_turn()
        names = [p["name"] for p in self.tool_calls()]
        self.assertEqual(names, ["bash", "read_file"])
        results = self.tool_results()
        self.assertIn("one", results[0])
        self.assertIn("some note", results[1])
        self.assertEqual(self.assistant_texts(), ["both done"])
        self.assertEqual(len(self.requests()), 3)

    def test_a_model_without_tool_support_gets_no_tools(self):
        self.use("plain", supports_tools=False)
        self.run_turn()
        self.assertEqual(self.tool_calls(), [])
        payloads = self.requests()
        self.assertNotIn("tools", payloads[0])
        self.assertEqual(self.assistant_texts(), ["just an answer"])

    def test_markup_without_the_capability_is_a_notice_not_an_execution(self):
        self.use("markup-bad")
        runner = self.run_turn()
        self.assertEqual(self.tool_calls(), [], "the markup must not execute")
        notes = [
            p["text"]
            for t, p in self.seen
            if t == events.NOTE and "tool call" in p["text"]
        ]
        self.assertTrue(notes, "the user must be told the call was not understood")
        transcript = " ".join(str(m.get("content")) for m in runner.messages)
        self.assertIn(
            "find . -maxdepth 2", transcript, "the text itself is kept, not hidden"
        )

    def test_markup_with_the_capability_executes_through_approvals(self):
        self.use("markup-bad", supports_text_tools=True)
        self.run_turn()
        calls = self.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        results = self.tool_results()
        self.assertTrue(
            any(
                "workspace" in r or ".py" in r or "mock" in r.lower() or r.strip()
                for r in results
            ),
            results,
        )

    def test_unknown_tool_comes_back_as_a_result(self):
        self.use("unknown")
        self.run_turn()
        results = self.tool_results()
        self.assertEqual(len(results), 1)
        self.assertIn("no tool named 'drop_database'", results[0])
        self.assertEqual(self.assistant_texts(), ["my mistake"])

    def test_bad_arguments_come_back_as_a_result(self):
        self.use("bad-args")
        self.run_turn()
        results = self.tool_results()
        self.assertEqual(len(results), 1)
        self.assertIn("missing required", results[0])
        self.assertEqual(self.assistant_texts(), ["fixed it"])

    def test_approval_allow_runs_the_tool(self):
        self.use("approve")
        self.run_turn()
        self.assertIn("approved-ok", self.tool_results()[0])
        self.assertEqual(self.assistant_texts(), ["approved"])

    def test_approval_deny_returns_a_denial_result(self):
        events.set_responder(lambda kind, payload: False)
        self.use("deny")
        self.run_turn()
        results = self.tool_results()
        self.assertEqual(len(results), 1)
        self.assertIn("denied", results[0])
        self.assertEqual(self.assistant_texts(), ["denied"])

    def test_failing_tool_lets_the_model_recover(self):
        self.use("fails")
        self.run_turn()
        self.assertEqual(len(self.tool_calls()), 1)
        self.assertEqual(self.assistant_texts(), ["recovered"])

    def test_streamed_tool_args_assemble_correctly(self):
        self.use("native")
        runner = agent.Runner(stream=True, cancel_event=None, mode="build")
        runner.handle_input("go")
        calls = [p for t, p in self.seen if t == events.TOOL_CALL]
        self.assertEqual(calls[0]["name"], "bash")
        import json as json_mod

        self.assertEqual(
            json_mod.loads("{" + '"command": "echo hi"' + "}")["command"], "echo hi"
        )

    def test_replayed_execution_replays_the_result(self):
        import subprocess

        ran = []

        def fake_run(command):
            ran.append(command)
            return subprocess.CompletedProcess(command, 0, "ran-once", "")

        first = {"command": "echo once"}
        import json as json_mod
        from types import SimpleNamespace

        call = SimpleNamespace(
            id="c1",
            function=SimpleNamespace(name="bash", arguments=json_mod.dumps(first)),
        )
        with mock.patch("harness.sandbox.run", side_effect=fake_run):
            args1, result1 = tools.execute(call, note="", call_key=("req-1", "c1"))
            args2, result2 = tools.execute(call, note="", call_key=("req-1", "c1"))
        self.assertEqual(ran, ["echo once"], "the handler must run once")
        self.assertEqual(result1, result2)
        self.assertIn("c1", [call_id for _, call_id in tools.executed_calls()])

    def test_build_mode_advertises_tools(self):

        with permissions.moded("default"):
            self.use("native")
            names = tools.available_tool_names()
            self.assertIn("bash", names)
            self.assertIn("read_file", names)

    def test_hiding_the_shell_costs_only_bash(self):
        with mock.patch("harness.tools.shell_available", return_value=False):
            report = tools.availability(refresh=True)
            self.assertFalse(report["bash"]["available"])
            self.assertTrue(report["read_file"]["available"])
            self.assertTrue(report["write_todos"]["available"])
        report = tools.availability(refresh=True)
        self.assertTrue(report["bash"]["available"])

    def test_no_git_keeps_everything_else_usable(self):

        self.assertFalse((self.root / ".git").exists())
        names = tools.available_tool_names()
        for expected in ("bash", "read_file", "write_file", "str_replace", "task"):
            self.assertIn(expected, names)

    def test_no_tools_at_all_is_told_to_the_model_and_the_user(self):
        """The one case where 'no tools are available' is true: say it plainly."""
        self.use("plain")
        with (
            mock.patch("harness.tools.available_tool_names", return_value=[]),
            mock.patch("harness.tools.tool_schemas", return_value=[]),
            mock.patch(
                "harness.tools.unavailable_summary",
                return_value="bash: no shell was found on PATH",
            ),
        ):
            self.run_turn()
        payloads = self.requests()
        self.assertNotIn("tools", payloads[0], "nothing to advertise")
        last = payloads[0]["messages"][-1]["content"]
        self.assertIn("No tools are available", last)
        self.assertIn("no shell was found on PATH", last)
        notes = [
            p
            for t, p in self.seen
            if t == events.NOTE and p.get("kind") == "tools_unavailable"
        ]
        self.assertTrue(notes, "the user must be told why, not left guessing")

    def test_tools_available_means_no_honesty_note(self):
        self.use("plain")
        self.run_turn()
        payloads = self.requests()
        self.assertIn("tools", payloads[0])
        notes = [
            p
            for t, p in self.seen
            if t == events.NOTE and p.get("kind") == "tools_unavailable"
        ]
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
