"""Optional plan/build role models around the per-turn intent.

Everything here runs with the real backend but a mocked call_llm, so what is
asserted is which *configured model* each phase used and what context the build
turn received - not the wording a live model returns. The spine is the same
one test_model_switch uses: providers for two models, and the recorded
provider.active_config()[2] of each call proves the routing.

The load-bearing guarantees: with the roles disabled, every turn runs on the
session's model with no handoff in the transcript; a plan turn (explicit
/plan or an auto-plan conversion) runs on the plan role's model when one is
configured, and the approved build runs on the build role's model - exactly
once per approved plan.
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

from harness import agent_roles, events, providers, server
from harness import paths as paths_mod


def reply(content, calls=()):
    tool_calls = [
        SimpleNamespace(
            id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a))
        )
        for i, (n, a) in enumerate(calls)
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
    "prompt_tokens": 1,
    "completion_tokens": 1,
    "reasoning_tokens": None,
    "cached_tokens": None,
}


class ConfigCase(unittest.TestCase):
    """The config file itself: defaults, normalisation, model resolution."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        appdata = Path(self.tmp.name) / "appdata"
        appdata.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=appdata)
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)

    def test_no_file_means_everything_off(self):
        config = agent_roles.get()
        self.assertFalse(config["plan"]["enabled"])
        self.assertFalse(config["build"]["enabled"])
        self.assertFalse(config["oversight"]["enabled"])
        self.assertEqual(config["oversight"]["intensity"], "medium")
        self.assertEqual(config["oversight"]["max_rounds"], 2)

    def test_round_trip_and_normalisation(self):
        agent_roles.save(
            {
                "plan": {
                    "enabled": True,
                    "model_id": "mdl-plan",
                    "instructions": "Be brief.",
                },
                "build": {"enabled": True, "model_id": "mdl-build"},
                "oversight": {
                    "enabled": True,
                    "model_id": "mdl-rev",
                    "intensity": "nonsense",
                    "max_rounds": 99,
                },
            }
        )
        agent_roles.reset_cache()
        got = agent_roles.get()
        self.assertTrue(got["plan"]["enabled"])
        self.assertEqual(got["plan"]["model_id"], "mdl-plan")
        self.assertEqual(got["plan"]["instructions"], "Be brief.")
        self.assertEqual(got["build"]["model_id"], "mdl-build")
        self.assertEqual(
            got["oversight"]["intensity"], "medium", "a bad intensity falls back"
        )
        self.assertLessEqual(
            got["oversight"]["max_rounds"], agent_roles.MAX_ROUNDS_LIMIT, "clamped"
        )

    def test_resolve_model_uses_own_or_default_or_none(self):
        self.assertIsNone(
            agent_roles.resolve_model({"enabled": False, "model_id": "mdl-plan"}, "def")
        )
        self.assertEqual(
            agent_roles.resolve_model({"enabled": True, "model_id": "mdl-plan"}, "def"),
            "mdl-plan",
        )
        self.assertEqual(
            agent_roles.resolve_model({"enabled": True, "model_id": ""}, "def"),
            "def",
            "enabled but unset uses the session default",
        )

    def test_garbage_file_becomes_defaults(self):
        agent_roles._file().write_text("{not json", encoding="utf-8")
        agent_roles.reset_cache()
        self.assertFalse(agent_roles.get()["plan"]["enabled"])


class RouteBase(unittest.TestCase):
    """Shared setup for routing and plan-approval tests: real providers, the
    real backend, a mocked coder that records which model each call used."""

    """Plan then build, end to end, with a mocked call_llm."""

    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        appdata = Path(self.tmp.name) / "appdata"
        appdata.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=appdata)
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))

        providers.save_group(
            {"id": "grp-1", "name": "P", "base_url": "https://x.example/v1"}
        )
        providers.save_model(
            {"id": "mdl-default", "name": "Default", "model": "default-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-plan", "name": "Planner", "model": "plan-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-build", "name": "Builder", "model": "build-model"}, "grp-1"
        )
        providers.set_active("mdl-default")

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.seen = {}

        self.calls = []
        self.plan_first = False
        self.patch = mock.patch("harness.agent.call_llm")
        self.llm = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.llm.side_effect = self._answer

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def _answer(
        self, messages, tools=None, on_delta=None, on_reasoning=None, cancel=None
    ):
        model = providers.active_config()[2]
        has_plan = any("<plan>" in str(m.get("content") or "") for m in messages)
        self.calls.append((model, has_plan))
        names = [
            c["function"]["name"] for m in messages for c in (m.get("tool_calls") or [])
        ]
        decided = any(
            "Entering plan mode" in str(m.get("content") or "") for m in messages
        )
        if self.plan_first and "plan_decision" not in names and not decided:
            return reply(
                None, [("plan_decision", {"decision": "plan", "reason": "ambiguous"})]
            ), USAGE
        if decided or model == "plan-model":
            return reply("PLAN_BODY: step one, step two."), USAGE
        return reply("built it"), USAGE

    def dispatch(self, **obj):
        obj.setdefault("req_id", time.time_ns() % 1000000)
        self.backend.dispatch(obj)

    def done(self, session, timeout=20, kind="done"):

        target = self.seen.get((session, kind), 0) + 1
        limit = time.time() + timeout
        while time.time() < limit:
            n = sum(
                1
                for m in self.sent
                if m.get("event") == kind and m.get("session") == session
            )
            if n >= target:
                self.seen[(session, kind)] = target
                return True
            time.sleep(0.02)
        return False

    def sid(self):
        for m in self.sent:
            if (
                m.get("event") == events.SESSION
                and m.get("session") == "s"
                and m["payload"].get("id")
            ):
                return m["payload"]["id"]
        return None

    def submit(self, text="fix the bug"):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done("s", kind="session"))
        self.calls.clear()
        self.dispatch(op="submit", session="s", text=text)
        self.assertTrue(self.done("s"))

    def transcript_users(self):
        sid = self.sid()
        path = paths_mod.session_dir() / f"{sid}.jsonl"
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "rewind_to" in entry:
                del out[entry["rewind_to"] :]
            elif "compacted" not in entry:
                out.append(entry)
        return [
            m["content"]
            for m in out
            if m.get("role") == "user"
            and not str(m.get("content") or "").startswith("<system-reminder")
        ]


class RouteCase(RouteBase):
    def test_all_disabled_is_one_request_on_the_default_model(self):
        self.submit()
        self.assertEqual(
            self.calls,
            [("default-model", False)],
            "no agents: one request, session model, no plan context",
        )

    def test_disabled_leaves_no_plan_in_the_transcript(self):
        self.submit()
        self.assertEqual(
            self.transcript_users(),
            ["fix the bug"],
            "no plan handoff message is injected when the feature is off",
        )

    def approve_plan(self):
        """Approve the ended plan through the plan card's action."""
        self.dispatch(op="plan_action", session="s", action="approve")
        self.assertTrue(self.done("s"))

    def test_plan_and_build_use_their_own_models_in_order(self):
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit("/plan fix the bug")
        self.approve_plan()
        self.assertEqual(
            [m for m, _ in self.calls],
            ["plan-model", "build-model"],
            "the plan runs on the plan model, the approved build on the build model",
        )

    def test_plan_is_handed_to_the_build_as_context(self):
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit("/plan fix the bug")
        self.approve_plan()
        plan_call = next(c for c in self.calls if c[0] == "plan-model")
        build_call = next(c for c in self.calls if c[0] == "build-model")
        self.assertFalse(plan_call[1], "the plan call does not carry a plan yet")
        self.assertTrue(
            build_call[1], "the build call sees the plan as structured context"
        )

    def test_plan_persists_in_the_transcript_as_inspectable_context(self):
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit("/plan fix the bug")
        self.approve_plan()
        users = self.transcript_users()
        self.assertEqual(users[0], "/plan fix the bug", "the request as typed is first")
        self.assertTrue(
            any(u.startswith("<plan>") and "step one" in u for u in users),
            "the plan is stored, not just shown and dropped",
        )

    def test_plan_event_is_ready_and_the_plan_is_approvable(self):
        agent_roles.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        self.submit("/plan fix the bug")
        plan_events = [m["payload"] for m in self.sent if m.get("event") == events.PLAN]
        ready = [p for p in plan_events if p.get("status") == "ready"]
        self.assertTrue(
            ready and ready[-1].get("inline"), "the plan is approvable inline"
        )
        self.assertEqual(
            [m for m, _ in self.calls],
            ["plan-model"],
            "an explicit plan never builds by itself",
        )

    def test_build_mode_ignores_agent_settings_and_uses_the_session_model(self):

        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit()
        self.assertEqual(
            [m for m, _ in self.calls],
            ["default-model"],
            "a plain build uses the session model, never the build agent",
        )
        self.assertFalse(any(u.startswith("<plan>") for u in self.transcript_users()))

    def test_plan_turn_runs_on_the_plan_model_when_configured(self):
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit("/plan fix the bug")
        self.assertEqual(
            [m for m, _ in self.calls],
            ["plan-model"],
            "a plan turn runs on the plan role's model",
        )

    def test_plan_turn_uses_the_session_model_without_a_plan_role(self):

        self.submit("/plan fix the bug")
        self.assertEqual([m for m, _ in self.calls], ["default-model"])

    def test_plan_action_approve_builds_exactly_once(self):
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit("/plan fix the bug")
        self.approve_plan()

        self.dispatch(op="plan_action", session="s", action="approve")
        replies = [m for m in self.sent if m.get("reply") == "plan_action"]
        self.assertFalse(replies[-1].get("ok"), "a replayed approval is refused")
        self.assertEqual([m for m, _ in self.calls], ["plan-model", "build-model"])
        handoffs = [u for u in self.transcript_users() if u.startswith("<plan>")]
        self.assertEqual(len(handoffs), 1, "exactly one plan handoff")

    def test_phase_events_track_plan_then_build(self):
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.submit("/plan fix the bug")
        self.approve_plan()
        phases = [
            m["payload"].get("phase")
            for m in self.sent
            if m.get("event") == events.PHASE
        ]
        self.assertIn("planning", phases)
        self.assertIn("handing_off", phases)
        self.assertIn("building", phases)

    def test_a_configured_role_model_that_is_gone_is_reported_not_swapped(self):
        agent_roles.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        providers.delete_model("mdl-plan")
        self.submit("/plan fix the bug")
        self.assertEqual(self.calls, [], "no plan runs on a substituted model")
        errors = [m for m in self.sent if m.get("event") == events.ERROR]
        self.assertTrue(errors, "the missing model is surfaced")
        self.assertIn("no longer exist", errors[-1]["payload"].get("text", ""))

    def test_build_mode_ignores_a_build_agent(self):

        agent_roles.save({"build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit()
        self.assertEqual(
            self.calls,
            [("default-model", False)],
            "a plain build: session model, no planning, no build agent",
        )

    def test_plan_only_plans_on_plan_model_then_builds_on_default(self):
        agent_roles.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        self.submit("/plan fix the bug")
        self.approve_plan()
        self.assertEqual(
            [m for m, _ in self.calls],
            ["plan-model", "default-model"],
            "the approved build falls back to the session default model",
        )
        build_call = next(c for c in self.calls if c[0] == "default-model")
        self.assertTrue(build_call[1], "the plan still reaches the build")

    def test_enabled_but_unset_model_uses_default_model(self):
        agent_roles.save({"plan": {"enabled": True}, "build": {"enabled": True}})
        self.submit("/plan fix the bug")
        self.approve_plan()

        self.assertTrue(all(m == "default-model" for m, _ in self.calls))
        self.assertEqual(len(self.calls), 2)

    def test_user_messages_are_never_duplicated_across_turns(self):
        agent_roles.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        self.submit("/plan first task")
        self.approve_plan()
        self.dispatch(op="submit", session="s", text="second task")
        self.assertTrue(self.done("s"))
        users = self.transcript_users()
        self.assertEqual(
            [u for u in users if not u.startswith("<plan>")],
            ["/plan first task", "second task"],
            "user messages are not duplicated",
        )


class AutoPlanApproval(RouteBase):
    """The automatic plan's gate: approve (build with the plan), skip (build
    without it), or cancel (nothing built).

    Reuses RouteCase's providers, mocked coder, and backend, and adds an
    approval responder so the runner can actually pause and be answered. The
    model enters plan mode on its own (plan_decision), the plan is shown, and
    the user decides with one action.
    """

    def setUp(self):
        super().setUp()
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

    def approvals(self):
        return [m for m in self.sent if m.get("request") == "approval"]

    def run_auto_plan(self):
        agent_roles.save(
            {
                "plan": {
                    "enabled": True,
                    "model_id": "mdl-plan",
                    "approve": True,
                    "auto": True,
                },
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.plan_first = True
        self.submit("make the app faster")

    def test_approval_prompts_with_the_plan_visible(self):
        self.run_auto_plan()
        asks = self.approvals()
        self.assertTrue(asks, "the automatic plan asks before building")
        self.assertEqual(asks[0].get("tool"), "plan")
        self.assertIn(
            "PLAN_BODY", asks[0].get("why", ""), "the plan is shown to the user"
        )
        entered = [m for m in self.sent if m.get("event") == events.AUTO_PLAN]
        self.assertTrue(entered and entered[0]["payload"].get("reason"))

    def test_approval_builds_exactly_once_with_the_plan(self):
        self.run_auto_plan()
        self.assertEqual(
            [m for m, _ in self.calls],
            ["default-model", "plan-model", "build-model"],
            "decide, plan on the plan model, then one approved build",
        )
        build_call = next(c for c in self.calls if c[0] == "build-model")
        self.assertTrue(build_call[1], "the approved plan reaches the build")
        self.assertEqual(
            len([u for u in self.transcript_users() if u.startswith("<plan>")]),
            1,
            "exactly one handoff",
        )
        self.assertTrue(
            [m for m in self.sent if m.get("event") == events.PLAN_APPROVED]
        )

    def test_skip_builds_now_without_the_plan(self):
        self.approval_answer = "skip"
        self.run_auto_plan()
        self.assertEqual(
            [m for m, _ in self.calls],
            ["default-model", "plan-model", "build-model"],
            "skip: the build runs at once (build role) without the plan handoff",
        )

        sid = self.sid()
        raw = (paths_mod.session_dir() / f"{sid}.jsonl").read_text(encoding="utf-8")
        self.assertIn(
            "Your operational mode has changed to BUILD",
            raw,
            "the skip build runs as build, not as a fenced plan turn",
        )
        self.assertFalse(
            any(u.startswith("<plan>") for u in self.transcript_users()),
            "no handoff message is injected on skip",
        )
        self.assertTrue(
            [
                m
                for m in self.sent
                if m.get("event") == events.PLAN_SKIPPED
                and m.get("payload", {}).get("via") == "user"
            ]
        )

    def test_cancel_builds_nothing_and_keeps_the_plan(self):
        self.approval_answer = False
        self.run_auto_plan()
        self.assertEqual(
            [m for m, _ in self.calls],
            ["default-model", "plan-model"],
            "a cancelled plan never reaches the build",
        )
        contents = []
        sid = self.sid()
        for line in (
            (paths_mod.session_dir() / f"{sid}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("role") == "assistant":
                contents.append(entry.get("content") or "")
        self.assertTrue(
            any("PLAN_BODY" in c for c in contents), "the plan is retained on cancel"
        )
        skipped = [
            m
            for m in self.sent
            if m.get("event") == events.NOTE
            and (m.get("payload", {}) or {}).get("kind") == "plan_skipped"
        ]
        self.assertTrue(skipped, "a cancel is surfaced to the user")

    def test_approval_off_hands_off_automatically(self):
        agent_roles.save(
            {
                "plan": {
                    "enabled": True,
                    "model_id": "mdl-plan",
                    "approve": False,
                    "auto": True,
                },
                "build": {"enabled": True, "model_id": "mdl-build"},
            }
        )
        self.plan_first = True
        self.submit("make the app faster")
        self.assertEqual(
            self.approvals(), [], "no approval request with the toggle off"
        )
        self.assertEqual(
            [m for m, _ in self.calls],
            ["default-model", "plan-model", "build-model"],
            "the plan hands off to build without pausing",
        )


class PlanArtifact(RouteBase):
    """The plan is a session-scoped artifact: persisted, reloadable, isolated."""

    def test_plan_is_persisted_for_its_own_session_only(self):
        from harness import plan as plan_mod

        agent_roles.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        self.submit("/plan fix the bug")
        sid = self.sid()
        record = plan_mod.load(sid)
        self.assertIsNotNone(record, "the plan is persisted for its session")
        self.assertEqual(record.get("completion"), plan_mod.COMPLETE)
        self.assertEqual(record.get("session_id"), sid)
        self.assertEqual(record.get("task"), "fix the bug", "the command is stripped")
        self.assertIn("step one", record.get("plan", ""), "the plan text is stored")

        self.assertIsNone(plan_mod.load("20990101-000000"))

    def test_get_plan_op_returns_the_persisted_record(self):
        from harness import plan as plan_mod

        agent_roles.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        self.submit("/plan fix the bug")
        sid = self.sid()
        self.sent.clear()
        self.backend.dispatch(
            {"op": "get_plan", "session": "s", "id": sid, "req_id": time.time_ns()}
        )
        replies = [m for m in self.sent if m.get("reply") == "get_plan"]
        self.assertTrue(replies and replies[-1].get("ok"), "the op succeeds")
        data = replies[-1].get("data") or {}
        self.assertEqual(data.get("completion"), plan_mod.COMPLETE)
        self.assertEqual(data.get("session_id"), sid)


class TodoReconcile(unittest.TestCase):
    """Plan steps cross out from real tool/test evidence, not LLM self-report."""

    def setUp(self):
        from harness import todos

        todos.TODOS[:] = []
        self.addCleanup(lambda: todos.TODOS.clear())
        self.todos = todos

    def test_writing_a_named_file_completes_its_step(self):
        self.todos.TODOS[:] = [
            {
                "content": "Create src/text_analyzer.py",
                "activeForm": "x",
                "status": "pending",
            }
        ]
        self.assertTrue(
            self.todos.reconcile(
                "write_file",
                {"path": "C:/x/src/text_analyzer.py"},
                "Wrote C:/x/src/text_analyzer.py",
            )
        )
        self.assertEqual(self.todos.TODOS[0]["status"], "done")

    def test_running_the_named_verification_completes_its_step(self):
        self.todos.TODOS[:] = [
            {
                "content": "Validate syntax with py_compile",
                "activeForm": "x",
                "status": "pending",
            }
        ]
        self.assertTrue(
            self.todos.reconcile(
                "bash", {"command": "python -m py_compile src/text_analyzer.py"}, "ok"
            )
        )
        self.assertEqual(self.todos.TODOS[0]["status"], "done")

    def test_a_failed_tool_completes_nothing(self):
        self.todos.TODOS[:] = [
            {
                "content": "Run pytest on the tests",
                "activeForm": "x",
                "status": "pending",
            }
        ]
        self.assertFalse(
            self.todos.reconcile("bash", {"command": "pytest"}, "Error: no tests found")
        )
        self.assertEqual(self.todos.TODOS[0]["status"], "pending")

    def test_an_unrelated_verification_does_not_complete_the_step(self):

        self.todos.TODOS[:] = [
            {"content": "Lint with flake8", "activeForm": "x", "status": "pending"}
        ]
        self.assertFalse(
            self.todos.reconcile("bash", {"command": "pytest tests"}, "1 passed")
        )
        self.assertEqual(self.todos.TODOS[0]["status"], "pending")

    def test_a_done_step_is_left_alone(self):
        self.todos.TODOS[:] = [
            {
                "content": "Create src/text_analyzer.py",
                "activeForm": "x",
                "status": "done",
            }
        ]
        self.assertFalse(
            self.todos.reconcile(
                "write_file", {"path": "src/text_analyzer.py"}, "Wrote"
            )
        )


class CancelMidBatch(RouteBase):
    """A stopped or budget-capped turn must leave NO unanswered tool_call.

    The assistant message is saved with all its tool_calls before any run; a
    cancel that landed mid-batch used to abandon the rest with no result, and
    every later request then 400s on the broken call/result pairing - Retry
    could not heal it, only a manual rewind. The fix synthesizes a stub result
    per unanswered call; this pins that invariant on the real transcript.
    """

    def test_cancel_mid_batch_answers_every_call(self):
        from harness import agent as agent_mod

        batch = reply(
            "working",
            calls=[
                ("read_file", {"path": "a.txt"}),
                ("read_file", {"path": "b.txt"}),
                ("read_file", {"path": "c.txt"}),
            ],
        )
        first = {"n": 0}
        real_execute = agent_mod.execute

        def fake_call(
            messages, tools=None, on_delta=None, on_reasoning=None, cancel=None
        ):
            first["n"] += 1
            if first["n"] == 1:
                return batch, USAGE
            return reply("done"), USAGE

        def spy(tool_call, **kwargs):
            result = real_execute(tool_call, **kwargs)

            self.backend.sessions["s"].cancel.set()
            return result

        self.llm.side_effect = fake_call
        with mock.patch("harness.agent.execute", side_effect=spy):
            self.submit("read those files")

        entries = []
        sid = self.sid()
        path = paths_mod.session_dir() / f"{sid}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if "rewind_to" in entry:
                del entries[entry["rewind_to"] :]
            elif "compacted" not in entry:
                entries.append(entry)

        asked, answered = set(), set()
        for m in entries:
            for call in m.get("tool_calls") or []:
                asked.add(call["id"])
            if m.get("role") == "tool":
                answered.add(m.get("tool_call_id"))
        self.assertTrue(asked, "the batch was recorded")
        self.assertEqual(
            asked, answered, "every tool_call id needs exactly one tool result"
        )

        self.assertFalse(self.backend.sessions["s"].running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
