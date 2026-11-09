"""Per-turn intent: commands, sticky mode, and the model's own plan decision.

The selector is gone; how a turn runs is resolved by the engine, in order:
an explicit /plan, /ask or /build in the text, the session's sticky mode, the
model's structured plan_decision signal, then the build default. Everything
here runs a real Runner with a mocked call_llm (and, for the op-level tests,
the real Harness backend), so what is asserted is the resolution order, the
read-only fence around the decision, and the handoff-once guarantee - not the
wording a live model returns.
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

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import agent_roles, commands, events, server
from harness import paths as paths_mod
from harness import plan as plan_mod
from harness import session as session_mod
from harness.agent import Runner
from harness.commands import parse_mode_command


def fake_message(content=None, calls=()):
    tool_calls = [
        SimpleNamespace(
            id=f"call_{i}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(calls)
    ]
    dumped = {"role": "assistant", "content": content}
    if tool_calls:
        dumped["tool_calls"] = [
            {
                "id": c.id,
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments,
                },
            }
            for c in tool_calls
        ]
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda exclude_none=True: dict(dumped),
    )


USAGE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "reasoning_tokens": None,
    "cached_tokens": None,
}


class CommandParsing(unittest.TestCase):
    """The mode commands split off cleanly; lookalikes stay unknown."""

    def test_command_with_text(self):
        self.assertEqual(
            parse_mode_command("/plan fix the bug"), ("plan", "fix the bug")
        )
        self.assertEqual(parse_mode_command("/ask  why?  "), ("ask", "why?"))

    def test_command_without_text(self):
        self.assertEqual(parse_mode_command("/plan"), ("plan", ""))
        self.assertEqual(parse_mode_command("  /build"), ("build", ""))

    def test_lookalikes_are_not_commands(self):
        self.assertEqual(parse_mode_command("/planx y"), (None, None))
        self.assertEqual(parse_mode_command("plan this"), (None, None))
        self.assertEqual(parse_mode_command(""), (None, None))

    def test_commands_are_listed_in_the_help(self):
        for name in ("/plan", "/ask", "/build"):
            self.assertIn(name, commands.COMMANDS)


class IsolatedCase(unittest.TestCase):
    """Shared setup: an isolated app data dir, so agent-role saves never leak."""

    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))


class IntentCase(IsolatedCase):
    """Resolution order and the events that say how a turn was chosen."""

    def setUp(self):
        super().setUp()
        self.seen = []
        events.subscribe(lambda t, p: self.seen.append((t, p)))
        events.set_responder(lambda kind, payload: True)
        for name in ("save", "compacted", "rewind_to", "ensure_title"):
            mock.patch("harness.session." + name).start()
        mock.patch("harness.session.all_sessions", return_value=[]).start()
        self.llm = mock.patch("harness.agent.call_llm").start()
        self.addCleanup(mock.patch.stopall)
        self.llm.return_value = (fake_message("done"), USAGE)

    def tearDown(self):
        events.reset()

    def intents(self):
        return [(p["intent"], p["source"]) for t, p in self.seen if t == events.INTENT]

    def test_default_intent_is_build(self):
        Runner().handle_input("rename the foo function to bar")
        self.assertEqual(
            self.intents(),
            [("build", "default"), ("build", "fallback")],
            "build is the default; the model ignored the decision gate, so the "
            "fallback says build too",
        )

    def test_command_with_text_binds_one_turn(self):
        runner = Runner()
        self.llm.return_value = (fake_message("the plan"), USAGE)
        runner.handle_input("/plan fix the bug")
        self.assertEqual(self.intents(), [("plan", "command")])
        self.assertEqual(runner.sticky_mode, None, "one turn only: nothing sticks")

    def test_command_without_text_is_sticky(self):
        runner = Runner()
        runner.handle_input("/plan")
        self.assertEqual(runner.sticky_mode, "plan")
        self.assertEqual(
            self.intents(), [("plan", "command")], "the sticky change is announced"
        )
        self.assertEqual(self.llm.call_count, 0, "a bare command runs no model turn")

    def test_sticky_mode_applies_to_following_messages(self):
        runner = Runner()
        runner.handle_input("/ask")
        runner.handle_input("why does the retry schedule exist?")
        self.assertEqual(
            self.intents(),
            [("ask", "command"), ("ask", "sticky")],
        )

    def test_build_command_leaves_the_sticky_mode(self):
        runner = Runner()
        runner.handle_input("/plan")
        runner.handle_input("/build")
        self.assertEqual(runner.sticky_mode, None)
        runner.handle_input("now build it")
        self.assertEqual(
            self.intents(),
            [
                ("plan", "command"),
                ("build", "command"),
                ("build", "default"),
                ("build", "fallback"),
            ],
        )

    def test_command_with_text_wins_over_sticky_for_that_turn(self):
        runner = Runner()
        runner.handle_input("/ask")
        self.llm.return_value = (fake_message("the plan"), USAGE)
        runner.handle_input("/plan draw it")
        self.assertEqual(
            self.intents(),
            [("ask", "command"), ("plan", "command")],
        )
        self.assertEqual(runner.sticky_mode, "ask", "the sticky ask is untouched")

    def test_unknown_slash_command_stays_unknown(self):
        runner = Runner()
        runner.handle_input("/bogus")
        self.assertEqual(self.llm.call_count, 0)
        notes = [p for t, p in self.seen if t == events.NOTE]
        self.assertTrue(notes, "the help note lists the commands")
        self.assertTrue(any("/plan" in n["text"] for n in notes))

    def test_read_only_enforcement_asks_through_the_shared_path(self):
        """A one-turn /plan is fenced exactly like the old plan mode."""
        runner = Runner()
        self.llm.side_effect = [
            (
                fake_message(
                    None,
                    [
                        (
                            "write_file",
                            {"path": str(self.root / "x.txt"), "content": "y"},
                        )
                    ],
                ),
                USAGE,
            ),
            (fake_message("described it instead"), USAGE),
        ]
        with mock.patch("harness.agent.execute") as exe:
            exe.side_effect = AssertionError("plan turns never execute writes")
            runner.handle_input("/plan write it")
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(results and "Plan mode" in results[0], results)
        offered = [s["function"]["name"] for s in self.llm.call_args[1]["tools"] or []]
        for name in (
            "write_file",
            "str_replace",
            "task",
            "plan_decision",
        ):
            self.assertNotIn(name, offered, f"{name} offered in plan mode")
        self.assertIn("read_file", offered)
        self.assertIn(
            "write_todos",
            offered,
            "the plan turn keeps the checklist: it is the plan's own step list",
        )

    def test_plan_mode_refuses_mutating_bash_without_executing(self):
        """The plan fence covers bash: reads run, workspace changes refuse."""
        runner = Runner()
        self.llm.side_effect = [
            (fake_message(None, [("bash", {"command": "mkdir -p sub"})]), USAGE),
            (fake_message("mapped the layout from the listing"), USAGE),
        ]
        with mock.patch("harness.agent.execute") as exe:
            exe.side_effect = AssertionError("plan turns never execute writes")
            runner.handle_input("/plan map the layout")
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(results and "read-only" in results[0].lower(), results)

    def test_plan_mode_still_runs_readonly_bash(self):
        runner = Runner()
        self.llm.side_effect = [
            (fake_message(None, [("bash", {"command": "git status"})]), USAGE),
            (fake_message("the tree is clean"), USAGE),
        ]
        with mock.patch(
            "harness.agent.execute", return_value=({"command": "git status"}, "clean")
        ) as exe:
            runner.handle_input("/plan check the tree")
        self.assertEqual(exe.call_count, 1)
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(results and "clean" in results[0], results)


class StickyPersistence(unittest.TestCase):
    """The sticky mode survives a restart, through the sidecar and the op."""

    def setUp(self):
        events.reset()
        self._counts = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.patch = mock.patch("harness.agent.call_llm")
        self.llm = self.patch.start()
        self.llm.return_value = (fake_message("done"), USAGE)
        self.addCleanup(self.patch.stop)

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def dispatch(self, **obj):
        obj.setdefault("req_id", time.time_ns() % 1000000)
        self.backend.dispatch(obj)

    def done(self, session="s", timeout=20, kind="done"):

        target = self._counts.get((session, kind), 0) + 1
        limit = time.time() + timeout
        while time.time() < limit:
            n = sum(
                1
                for m in self.sent
                if m.get("event") == kind and m.get("session") == session
            )
            if n >= target:
                self._counts[(session, kind)] = target
                return True
            time.sleep(0.02)
        return False

    def sid(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                return m["payload"]["id"]
        return None

    def set_mode_op(self, mode):
        self.dispatch(op="set_mode", session="s", mode=mode)
        replies = [m for m in self.sent if m.get("reply") == "set_mode"]
        self.assertTrue(replies and replies[-1].get("ok"), f"set_mode {mode} refused")

    def test_set_mode_persists_and_reopens_with_it(self):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        sid = self.sid()
        self.set_mode_op("plan")
        self.dispatch(op="open", session="s", root=str(self.root), id=sid)
        self.assertTrue(self.done(kind="session"))
        payloads = [m["payload"] for m in self.sent if m.get("event") == events.SESSION]
        self.assertEqual(
            payloads[-1].get("mode"), "plan", "the session reopens in plan"
        )
        self.assertEqual(
            self.backend.sessions["s"].mode, "plan", "the engine state follows"
        )
        s = self.backend.sessions["s"]
        self.assertEqual(s.runner.sticky_mode, "plan")

    def test_set_mode_build_clears_it(self):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        self.set_mode_op("ask")
        self.set_mode_op("build")
        self.assertEqual(self.backend.sessions["s"].mode, "build")
        self.assertIsNone(self.backend.sessions["s"].runner.sticky_mode)

    def test_set_mode_rejects_unknown_and_legacy_values(self):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        for bad in ("plan-build", "autopilot", "nonsense"):
            self.dispatch(op="set_mode", session="s", mode=bad)
            replies = [m for m in self.sent if m.get("reply") == "set_mode"]
            self.assertFalse(replies[-1].get("ok"), bad)
        self.assertEqual(self.backend.sessions["s"].mode, "build")

    def test_engine_command_persists_through_the_hook(self):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        self.dispatch(op="submit", session="s", text="/ask")
        self.assertTrue(self.done())
        self.assertEqual(self.backend.sessions["s"].mode, "ask")
        sid = self.sid()
        self.assertEqual(
            session_mod.sticky_mode(sid),
            "ask",
            "the bare command persisted through the session hook",
        )


class TodoChecklist(unittest.TestCase):
    """The write_todos list is the plan's step list: it persists across turns
    (so the build reconciles it) and a plan turn may write it."""

    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.patch = mock.patch("harness.agent.call_llm")
        self.llm = self.patch.start()
        self.addCleanup(self.patch.stop)
        from harness import todos as todos_mod

        todos_mod.TODOS.clear()
        self.addCleanup(todos_mod.TODOS.clear)

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def dispatch(self, **obj):
        obj.setdefault("req_id", time.time_ns() % 1000000)
        self.backend.dispatch(obj)

    def done(self, session="s", timeout=30, kind="done"):
        target = self._counts.get((session, kind), 0) + 1
        limit = time.time() + timeout
        while time.time() < limit:
            n = sum(
                1
                for m in self.sent
                if m.get("event") == kind and m.get("session") == session
            )
            if n >= target:
                self._counts[(session, kind)] = target
                return True
            time.sleep(0.02)
        return False

    def setUp_data(self):
        self._counts = {}

    def test_checklist_persists_across_turns_and_reconciles(self):
        self.setUp_data()
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        answers = [
            (
                fake_message(
                    None,
                    [
                        (
                            "write_todos",
                            {
                                "todos": [
                                    {
                                        "content": "write PLAN-RESULT.md",
                                        "activeForm": "writing the result",
                                        "status": "in_progress",
                                    },
                                    {
                                        "content": "run pytest",
                                        "activeForm": "running the tests",
                                        "status": "pending",
                                    },
                                ]
                            },
                        )
                    ],
                ),
                USAGE,
            ),
            (
                fake_message(
                    None,
                    [
                        (
                            "write_file",
                            {
                                "path": str(self.root / "PLAN-RESULT.md"),
                                "content": "done",
                            },
                        )
                    ],
                ),
                USAGE,
            ),
            (fake_message("finished"), USAGE),
        ]
        self.llm.side_effect = iter(answers)
        self.dispatch(op="submit", session="s", text="do the steps")
        self.assertTrue(self.done())
        # The build turn's reconcile crossed the file's step off, from
        # evidence, and said so on the wire.
        todos_events = [
            m["payload"]["todos"] for m in self.sent if m.get("event") == events.TODOS
        ]
        self.assertTrue(todos_events, "the checklist reached the UI")
        self.assertEqual(
            todos_events[-1][0]["status"], "done", "the written file completed its step"
        )
        self.assertEqual(todos_events[-1][1]["status"], "pending")
        # The next turn inherits the list: the re-injected <todos> block
        # carries it, so reconcile can keep crossing steps off.
        self.sent.clear()
        self._counts.clear()
        self.llm.side_effect = None
        self.llm.return_value = (fake_message("more"), USAGE)
        self.dispatch(op="submit", session="s", text="keep going")
        self.assertTrue(self.done())
        request = self.llm.call_args[0][0]
        injections = [
            m
            for m in request
            if str(m.get("content") or "").startswith("<env>")
            and "<todos>" in str(m.get("content") or "")
        ]
        self.assertTrue(injections, "the checklist is re-injected into the next turn")
        self.assertIn("[ ] run pytest", injections[0]["content"])

    def test_a_plan_turn_can_write_the_checklist(self):
        self.setUp_data()
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        steps = {
            "todos": [
                {
                    "content": "measure first",
                    "activeForm": "measuring",
                    "status": "in_progress",
                },
                {
                    "content": "cache the layout",
                    "activeForm": "caching",
                    "status": "pending",
                },
            ]
        }
        self.llm.side_effect = [
            (fake_message(None, [("write_todos", steps)]), USAGE),
            (fake_message("PLAN: measure, then cache."), USAGE),
        ]
        self.dispatch(op="submit", session="s", text="/plan plan the work")
        self.assertTrue(self.done())
        todos_events = [
            m["payload"]["todos"] for m in self.sent if m.get("event") == events.TODOS
        ]
        self.assertTrue(todos_events, "the plan turn's steps reached the checklist")
        self.assertEqual(
            [t["content"] for t in todos_events[0]],
            ["measure first", "cache the layout"],
        )
        writes = [
            p["name"]
            for m in self.sent
            if m.get("event") == events.TOOL_CALL
            for p in [m["payload"]]
        ]
        self.assertEqual(
            writes, ["write_todos"], "no workspace write happened in the plan turn"
        )


class AutoPlanCase(IsolatedCase):
    """The model's plan-or-proceed decision: structured signal, no writes first."""

    def setUp(self):
        super().setUp()
        self.seen = []
        events.subscribe(lambda t, p: self.seen.append((t, p)))
        self.responder = lambda kind, payload: True
        events.set_responder(lambda kind, payload: self.responder(kind, payload))
        for name in ("save", "compacted", "rewind_to", "ensure_title"):
            mock.patch("harness.session." + name).start()
        mock.patch("harness.session.all_sessions", return_value=[]).start()
        self.llm = mock.patch("harness.agent.call_llm").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        events.reset()

    def payloads(self, kind):
        return [p for t, p in self.seen if t == kind]

    def payloads(self, kind):
        return [p for t, p in self.seen if t == kind]

    def intents(self):
        return [(p["intent"], p["source"]) for t, p in self.seen if t == events.INTENT]

    def test_ambiguous_request_enters_plan_mode(self):
        self.responder = lambda kind, payload: False
        self.llm.side_effect = [
            (
                fake_message(
                    None, [("plan_decision", {"decision": "plan", "reason": "scope?"})]
                ),
                USAGE,
            ),
            (fake_message("PLAN: do it in three steps"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("make the app faster")
        auto = self.payloads(events.AUTO_PLAN)
        self.assertTrue(auto and auto[0].get("reason") == "scope?")
        self.assertEqual(
            self.intents(),
            [("build", "default"), ("plan", "auto")],
            "the model's own decision is the intent source",
        )
        self.assertEqual(runner.mode, "plan", "the turn converted to planning")
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(any("Entering plan mode" in r for r in results))
        plan = [p for p in self.payloads(events.PLAN) if p.get("status") == "ready"]
        self.assertTrue(plan and plan[0].get("auto"), "the plan is tagged automatic")

    def test_no_write_tool_is_offered_before_the_decision(self):
        self.llm.side_effect = [
            (
                fake_message(
                    None, [("plan_decision", {"decision": "plan", "reason": "scope?"})]
                ),
                USAGE,
            ),
            (fake_message("PLAN: do it"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("make the app faster")
        first_tools = [
            s["function"]["name"] for s in self.llm.call_args_list[0][1]["tools"] or []
        ]
        self.assertNotIn("write_file", first_tools)
        self.assertNotIn("str_replace", first_tools)
        self.assertNotIn("task", first_tools, "no subagent before the decision")
        self.assertIn("plan_decision", first_tools)

    def test_a_write_in_the_converted_turn_is_refused(self):
        self.llm.side_effect = [
            (
                fake_message(
                    None,
                    [
                        ("plan_decision", {"decision": "plan", "reason": "scope?"}),
                        (
                            "write_file",
                            {"path": str(self.root / "x.txt"), "content": "nope"},
                        ),
                    ],
                ),
                USAGE,
            ),
            (fake_message("PLAN: do it"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("make the app faster")
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(
            any("Plan mode" in r for r in results),
            "the second fence refuses the write the same turn",
        )

    def test_directive_request_proceeds_when_the_model_says_build(self):
        self.llm.side_effect = [
            (fake_message(None, [("plan_decision", {"decision": "build"})]), USAGE),
            (
                fake_message(
                    None,
                    [
                        (
                            "write_file",
                            {"path": str(self.root / "x.txt"), "content": "y"},
                        )
                    ],
                ),
                USAGE,
            ),
            (fake_message("built"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("rename the foo function to bar everywhere")
        self.assertEqual(runner.mode, "build")
        self.assertEqual(
            [(p["intent"], p["source"]) for t, p in self.seen if t == events.INTENT][
                -1
            ],
            ("build", "model"),
        )
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(
            any("Wrote" in r and str(self.root / "x.txt") in r for r in results),
            "the write ran in the workspace",
        )

        second_tools = self.llm.call_args_list[1][1]["tools"]
        self.assertIsNone(second_tools, "writes flow after the decision")

    def test_a_model_that_ignores_the_decision_proceeds_as_build(self):
        self.llm.side_effect = [
            (
                fake_message(
                    None,
                    [
                        (
                            "write_file",
                            {"path": str(self.root / "x.txt"), "content": "y"},
                        )
                    ],
                ),
                USAGE,
            ),
            (fake_message("built"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("just do it: write the file")
        self.assertEqual(runner.mode, "build", "never silently read-only")
        self.assertEqual(
            [(p["intent"], p["source"]) for t, p in self.seen if t == events.INTENT][
                -1
            ],
            ("build", "fallback"),
        )
        results = [p["result"] for t, p in self.seen if t == events.TOOL_RESULT]
        self.assertTrue(
            any("Wrote" in r and str(self.root / "x.txt") in r for r in results),
            "the write still ran in the workspace",
        )

    def test_turning_auto_plan_off_makes_ambiguous_requests_build(self):
        from harness import tools as tools_mod

        agent_roles.save({"plan": {"auto": False}})
        self.addCleanup(agent_roles.reset_cache)
        self.llm.return_value = (fake_message("built"), USAGE)
        runner = Runner()
        runner.handle_input("make the app faster")
        self.assertEqual(runner.mode, "build")

        first_tools = [
            s["function"]["name"]
            for s in (self.llm.call_args_list[0][1]["tools"] or tools_mod.TOOL_SCHEMAS)
        ]
        self.assertNotIn("plan_decision", first_tools, "no decision gate when off")
        self.assertIn("write_file", first_tools)
        self.assertFalse(self.payloads(events.AUTO_PLAN))

    def test_planning_once_suppresses_the_next_auto_plan(self):
        self.llm.side_effect = [
            (
                fake_message(
                    None, [("plan_decision", {"decision": "plan", "reason": "scope?"})]
                ),
                USAGE,
            ),
            (fake_message("PLAN: do it"), USAGE),
            (fake_message("built"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("make the app faster")
        runner.handle_input("make the app faster")
        auto = self.payloads(events.AUTO_PLAN)
        self.assertEqual(len(auto), 1, "no planning loop for the same request")
        self.assertEqual(runner.mode, "build")

    def test_plan_command_still_works_with_auto_plan_off(self):
        agent_roles.save({"plan": {"auto": False}})
        self.addCleanup(agent_roles.reset_cache)
        self.llm.return_value = (fake_message("PLAN: do it"), USAGE)
        runner = Runner()
        runner.handle_input("/plan make the app faster")
        self.assertEqual(runner.mode, "plan")
        ready = [p for p in self.payloads(events.PLAN) if p.get("status") == "ready"]
        self.assertTrue(ready)

    def test_headless_auto_plan_returns_the_plan_and_stops(self):
        """No interaction channel: the plan is the deliverable, nothing builds."""
        events.set_responder(None)
        self.llm.side_effect = [
            (
                fake_message(
                    None, [("plan_decision", {"decision": "plan", "reason": "scope?"})]
                ),
                USAGE,
            ),
            (fake_message("PLAN: do it"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("make the app faster")
        self.assertEqual(self.llm.call_count, 2, "no build call after the plan")
        awaiting = [
            p for p in self.payloads(events.NOTE) if p.get("kind") == "plan_awaiting"
        ]
        self.assertTrue(awaiting, "the stop is surfaced, not silent")
        record = plan_mod.load(session_mod.current_id())
        self.assertIsNotNone(record)
        self.addCleanup(lambda: plan_mod.forget(record.get("session_id") or ""))


class SteerQueueIntent(IsolatedCase):
    """Steers and queued messages resolve their own intent when they run."""

    def setUp(self):
        super().setUp()
        self.seen = []
        self.responder = lambda kind, payload: True
        events.subscribe(lambda t, p: self.seen.append((t, p)))
        events.set_responder(lambda kind, payload: self.responder(kind, payload))
        for name in ("save", "compacted", "rewind_to", "ensure_title"):
            mock.patch("harness.session." + name).start()
        mock.patch("harness.session.all_sessions", return_value=[]).start()
        self.llm = mock.patch("harness.agent.call_llm").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        events.reset()

    def intents(self):
        return [(p["intent"], p["source"]) for t, p in self.seen if t == events.INTENT]

    def test_a_steer_without_text_sets_the_sticky_mode(self):
        runner = Runner()
        from harness.pending import Mailbox

        runner.mailbox = Mailbox()
        self.llm.side_effect = [
            (fake_message(None, [("read_file", {"path": "a"})]), USAGE),
            (fake_message("answering"), USAGE),
            (fake_message("planning"), USAGE),
        ]
        runner.mailbox.add("/plan", "steer")
        runner.handle_input("keep going")
        self.assertEqual(runner.sticky_mode, "plan", "the steer set the sticky mode")

    def test_a_steer_with_text_converts_the_running_build_turn(self):
        runner = Runner()
        from harness.pending import Mailbox

        runner.mailbox = Mailbox()
        self.responder = lambda kind, payload: False
        self.llm.side_effect = [(fake_message("PLAN: focused version"), USAGE)]
        runner.mailbox.add("/plan focus on the parser", "steer")
        runner.handle_input("make it better")
        self.assertEqual(
            self.intents(),
            [
                ("build", "default"),
                ("plan", "command"),
            ],
            "the steer converted the running build turn",
        )
        notes = [p["text"] for t, p in self.seen if t == events.NOTE]
        self.assertTrue(any("Switched to plan mode" in n for n in notes))
        user_texts = [
            p["content"]
            for t, p in self.seen
            if t == events.MESSAGE and p.get("role") == "user"
        ]
        self.assertIn("/plan focus on the parser", user_texts)
        self.assertEqual(runner.mode, "plan", "the turn ended as a plan turn")

    def test_a_steer_plan_after_planning_defers_to_the_next_turn(self):
        runner = Runner()
        from harness.pending import Mailbox

        runner.mailbox = Mailbox()
        self.llm.side_effect = [
            (fake_message(None, [("read_file", {"path": "a"})]), USAGE),
            (fake_message("answering"), USAGE),
        ]
        runner.handle_input("/ask")
        runner.mailbox.add("/plan and the tests", "steer")
        runner.handle_input("what about the tests?")

        self.assertEqual(runner._pending_one_turn, "plan")
        deferred = [p for t, p in self.seen if t == events.NOTE]
        self.assertTrue(
            any(p.get("kind") == "mode_deferred" for p in deferred),
            "the deferral is said out loud",
        )
        user_texts = [
            p["content"]
            for t, p in self.seen
            if t == events.MESSAGE and p.get("role") == "user"
        ]
        self.assertIn("and the tests", user_texts, "the steered text still landed")


class SubagentExclusion(IsolatedCase):
    """Subagents never see the decision gate and never auto-plan."""

    def setUp(self):
        super().setUp()
        self.seen = []
        events.subscribe(lambda t, p: self.seen.append((t, p)))
        events.set_responder(lambda kind, payload: True)
        for name in ("save", "compacted", "rewind_to", "ensure_title"):
            mock.patch("harness.session." + name).start()
        mock.patch("harness.session.all_sessions", return_value=[]).start()
        self.llm = mock.patch("harness.agent.call_llm").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        events.reset()

    def test_the_decision_tool_is_never_in_the_subagent_toolset(self):
        from harness import subagent, tools

        registry = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
        self.assertNotIn(
            "plan_decision",
            registry,
            "the decision tool is not a registry tool, so no guest loop can get it",
        )
        self.assertTrue(
            {"write_file", "str_replace", "task"} <= subagent.WITHHELD,
            "a guest cannot write either",
        )

    def test_task_is_withheld_while_the_decision_is_pending(self):
        self.llm.side_effect = [
            (
                fake_message(
                    None, [("plan_decision", {"decision": "plan", "reason": "scope?"})]
                ),
                USAGE,
            ),
            (fake_message("PLAN: do it"), USAGE),
        ]
        runner = Runner()
        runner.handle_input("make the app faster")
        first_tools = [
            s["function"]["name"] for s in self.llm.call_args_list[0][1]["tools"] or []
        ]
        self.assertNotIn("task", first_tools, "no subagent can act before the decision")


class HandoffOnce(unittest.TestCase):
    """One approved plan, one build - through either approval path."""

    def setUp(self):
        events.reset()
        self._counts = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.patch = mock.patch("harness.agent.call_llm")
        self.llm = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.approval_answer = True
        self._answered: set = set()
        self._stop = threading.Event()
        threading.Thread(target=self._approver, daemon=True).start()
        self.addCleanup(self._stop.set)

    def _approver(self):
        while not self._stop.wait(0.02):
            for m in list(self.sent):
                rid, req = m.get("id"), m.get("request")
                if rid and req == "approval" and rid not in self._answered:
                    self._answered.add(rid)
                    s = self.backend.sessions.get(m.get("session"))
                    if s:
                        s.reply(rid, self.approval_answer)

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def dispatch(self, **obj):
        obj.setdefault("req_id", time.time_ns() % 1000000)
        self.backend.dispatch(obj)

    def done(self, session="s", timeout=30, kind="done"):

        target = self._counts.get((session, kind), 0) + 1
        limit = time.time() + timeout
        while time.time() < limit:
            n = sum(
                1
                for m in self.sent
                if m.get("event") == kind and m.get("session") == session
            )
            if n >= target:
                self._counts[(session, kind)] = target
                return True
            time.sleep(0.02)
        return False

    def sid(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                return m["payload"]["id"]
        return None

    def test_card_approval_builds_once_and_replays_are_refused(self):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        self.llm.return_value = (fake_message("PLAN: do it in steps"), USAGE)
        self.dispatch(op="submit", session="s", text="/plan make it better")
        self.assertTrue(self.done())
        sid = self.sid()
        self.sent.clear()
        self._counts.clear()
        self.dispatch(op="plan_action", session="s", action="approve")
        self.assertTrue(self.done())
        self.dispatch(op="plan_action", session="s", action="approve")
        replies = [m for m in self.sent if m.get("reply") == "plan_action"]
        self.assertFalse(replies[-1].get("ok"), "a replayed approval is refused")
        handoffs = 0
        for line in (
            (paths_mod.session_dir() / f"{sid}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            entry = json.loads(line) if line.strip() else {}
            if str(entry.get("content") or "").startswith("<plan>"):
                handoffs += 1
        self.assertEqual(handoffs, 1, "exactly one handoff for one approval")
        self.assertEqual(self.llm.call_count, 2, "the plan call plus one build call")

    def test_cancel_action_dismisses_without_building(self):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        self.llm.return_value = (fake_message("PLAN: do it in steps"), USAGE)
        self.dispatch(op="submit", session="s", text="/plan make it better")
        self.assertTrue(self.done())
        sid = self.sid()
        calls_before = self.llm.call_count
        self.sent.clear()
        self.dispatch(op="plan_action", session="s", action="cancel")
        limit = time.time() + 20
        while time.time() < limit and not any(
            m.get("event") == events.PLAN_SKIPPED for m in self.sent
        ):
            time.sleep(0.02)
        self.assertTrue(
            any(m.get("event") == events.PLAN_SKIPPED for m in self.sent),
            "the dismissal is announced",
        )
        record = plan_mod.load(sid)
        self.assertEqual(record.get("phase"), "cancelled")
        self.assertEqual(self.llm.call_count, calls_before, "nothing built")

    def test_a_queued_plan_command_carries_its_command(self):
        """Queue items resolve their own intent when the session runs them."""
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done(kind="session"))
        answers = [(fake_message("building"), USAGE), (fake_message("the plan"), USAGE)]
        calls = {"n": 0}

        def scripted(*args, **kwargs):
            answer = answers[min(calls["n"], len(answers) - 1)]
            calls["n"] += 1
            if calls["n"] == 1:
                self.dispatch(
                    op="enqueue", session="s", text="/plan draft it", kind="queue"
                )
            return answer

        self.llm.side_effect = scripted
        self.dispatch(op="submit", session="s", text="start something")
        self.assertTrue(self.done())

        limit = time.time() + 20
        while time.time() < limit and self.backend.sessions["s"].running:
            time.sleep(0.02)
        self.assertFalse(self.backend.sessions["s"].running, "the chain went idle")
        intents = [m["payload"] for m in self.sent if m.get("event") == events.INTENT]
        self.assertEqual(
            [(p["intent"], p["source"]) for p in intents],
            [
                ("build", "default"),
                ("build", "fallback"),
                ("plan", "command"),
            ],
            "the queued /plan ran as its own plan turn",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
