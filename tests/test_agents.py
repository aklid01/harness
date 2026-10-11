"""Optional plan/build agents: role routing around the existing agent.

Everything here runs with the real backend but a mocked call_llm, so what is
asserted is which *configured model* each phase used and what context the build
phase received - not the wording a live model returns. The spine is the same
one test_model_switch uses: providers for two models, and the recorded
provider.active_config()[2] of each call proves the routing.

The load-bearing guarantee: with the feature disabled, one submit is still one
model request against the session's own model, with no planning handoff in the
transcript - the existing agent, unchanged.
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

from harness import agents, events, paths as paths_mod, providers, server  # noqa: E402
from harness import session as session_mod  # noqa: E402


def reply(content, calls=()):
    tool_calls = [
        SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
        for i, (n, a) in enumerate(calls)
    ]
    dumped = {"role": "assistant", "content": content}
    if tool_calls:
        dumped["tool_calls"] = [
            {"id": c.id, "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls
        ]
    return SimpleNamespace(content=content, tool_calls=tool_calls,
                           model_dump=lambda exclude_none=True: dict(dumped))


USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": None, "cached_tokens": None}


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
        self.addCleanup(agents.reset_cache)

    def test_no_file_means_everything_off(self):
        config = agents.get()
        self.assertFalse(config["plan"]["enabled"])
        self.assertFalse(config["build"]["enabled"])
        self.assertFalse(config["oversight"]["enabled"])
        self.assertEqual(config["oversight"]["intensity"], "medium")
        self.assertEqual(config["oversight"]["max_rounds"], 2)

    def test_round_trip_and_normalisation(self):
        stored = agents.save({
            "plan": {"enabled": True, "model_id": "mdl-plan", "instructions": "Be brief."},
            "build": {"enabled": True, "model_id": "mdl-build"},
            "oversight": {"enabled": True, "model_id": "mdl-rev",
                          "intensity": "nonsense", "max_rounds": 99},
        })
        agents.reset_cache()
        got = agents.get()
        self.assertTrue(got["plan"]["enabled"])
        self.assertEqual(got["plan"]["model_id"], "mdl-plan")
        self.assertEqual(got["plan"]["instructions"], "Be brief.")
        self.assertEqual(got["build"]["model_id"], "mdl-build")
        self.assertEqual(got["oversight"]["intensity"], "medium", "a bad intensity falls back")
        self.assertLessEqual(got["oversight"]["max_rounds"], agents.MAX_ROUNDS_LIMIT, "clamped")

    def test_resolve_model_uses_own_or_default_or_none(self):
        self.assertIsNone(agents.resolve_model({"enabled": False, "model_id": "mdl-plan"}, "def"))
        self.assertEqual(agents.resolve_model({"enabled": True, "model_id": "mdl-plan"}, "def"), "mdl-plan")
        self.assertEqual(agents.resolve_model({"enabled": True, "model_id": ""}, "def"), "def",
                         "enabled but unset uses the session default")

    def test_garbage_file_becomes_defaults(self):
        agents._file().write_text("{not json", encoding="utf-8")
        agents.reset_cache()
        self.assertFalse(agents.get()["plan"]["enabled"])


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
        self.addCleanup(agents.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))

        providers.save_group({"id": "grp-1", "name": "P", "base_url": "https://x.example/v1"})
        providers.save_model({"id": "mdl-default", "name": "Default", "model": "default-model"}, "grp-1")
        providers.save_model({"id": "mdl-plan", "name": "Planner", "model": "plan-model"}, "grp-1")
        providers.save_model({"id": "mdl-build", "name": "Builder", "model": "build-model"}, "grp-1")
        providers.set_active("mdl-default")

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.seen = {}

        self.calls = []  # (model, has_plan_in_context)
        self.patch = mock.patch("harness.agent.call_llm")
        self.llm = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.llm.side_effect = self._answer

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def _answer(self, messages, tools=None, on_delta=None, cancel=None):
        model = providers.active_config()[2]
        has_plan = any("<plan>" in str(m.get("content") or "") for m in messages)
        self.calls.append((model, has_plan))
        # The planner answers with a plan; the builder answers with a summary.
        if model == "plan-model":
            return reply("PLAN_BODY: step one, step two."), USAGE
        return reply("built it"), USAGE

    def dispatch(self, **obj):
        obj.setdefault("req_id", time.time_ns() % 1000000)
        self.backend.dispatch(obj)

    def done(self, session, timeout=20, kind="done"):
        # Count-based so a done from an earlier turn in self.sent can never be
        # mistaken for this one; each call waits for the NEXT such event.
        target = self.seen.get((session, kind), 0) + 1
        limit = time.time() + timeout
        while time.time() < limit:
            n = sum(1 for m in self.sent
                    if m.get("event") == kind and m.get("session") == session)
            if n >= target:
                self.seen[(session, kind)] = target
                return True
            time.sleep(0.02)
        return False

    def sid(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m.get("session") == "s" and m["payload"].get("id"):
                return m["payload"]["id"]
        return None

    def submit(self, text="fix the bug", mode="build"):
        self.dispatch(op="open", session="s", root=str(self.root))
        self.assertTrue(self.done("s", kind="session"))
        self.calls.clear()
        self.dispatch(op="submit", session="s", text=text, mode=mode)
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
                del out[entry["rewind_to"]:]
            elif "compacted" not in entry:
                out.append(entry)
        return [m["content"] for m in out if m.get("role") == "user"]


class RouteCase(RouteBase):
    # ---- the spine: disabled == today ---------------------------------

    def test_all_disabled_is_one_request_on_the_default_model(self):
        self.submit()
        self.assertEqual(self.calls, [("default-model", False)],
                         "no agents: one request, session model, no plan context")

    def test_disabled_leaves_no_plan_in_the_transcript(self):
        self.submit()
        self.assertEqual(self.transcript_users(), ["fix the bug"],
                         "no plan handoff message is injected when the feature is off")

    # ---- plan + build ---------------------------------------------------

    def test_plan_and_build_use_their_own_models_in_order(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        self.assertEqual([m for m, _ in self.calls], ["plan-model", "build-model"],
                         "the plan runs on the plan model, the build on the build model")

    def test_plan_is_handed_to_the_build_as_context(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        plan_call = next(c for c in self.calls if c[0] == "plan-model")
        build_call = next(c for c in self.calls if c[0] == "build-model")
        self.assertFalse(plan_call[1], "the plan call does not carry a plan yet")
        self.assertTrue(build_call[1], "the build call sees the plan as structured context")

    def test_plan_persists_in_the_transcript_as_inspectable_context(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        users = self.transcript_users()
        self.assertEqual(users[0], "fix the bug", "the real request is first")
        self.assertTrue(any(u.startswith("<plan>") and "step one" in u for u in users),
                        "the plan is stored, not just shown and dropped")

    def test_plan_event_is_emitted_then_ready(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        statuses = [m["payload"].get("status") for m in self.sent
                    if m.get("event") == events.PLAN]
        self.assertIn("generating", statuses)
        self.assertIn("ready", statuses)

    def test_build_mode_never_invokes_the_planner(self):
        # Build mode executes directly: a configured plan agent does not sneak
        # planning in. Planning only runs when Plan or Plan->Build is selected.
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="build")
        self.assertEqual([m for m, _ in self.calls], ["build-model"],
                         "Build mode never calls the planner")
        self.assertFalse(any(u.startswith("<plan>") for u in self.transcript_users()))

    def test_plan_build_hands_off_and_builds_exactly_once(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        self.assertEqual([m for m, _ in self.calls], ["plan-model", "build-model"])
        handoffs = [u for u in self.transcript_users() if u.startswith("<plan>")]
        self.assertEqual(len(handoffs), 1, "exactly one plan handoff")

    def test_phase_events_track_plan_then_build(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        phases = [m["payload"].get("phase") for m in self.sent if m.get("event") == events.PHASE]
        self.assertIn("planning", phases)
        self.assertIn("building", phases)

    def test_a_configured_role_model_that_is_gone_is_reported_not_swapped(self):
        agents.save({"build": {"enabled": True, "model_id": "mdl-build"}})
        providers.delete_model("mdl-build")
        self.submit(mode="build")
        self.assertEqual(self.calls, [], "no build runs on a substituted model")
        errors = [m for m in self.sent if m.get("event") == events.ERROR]
        self.assertTrue(errors, "the missing model is surfaced")
        self.assertIn("no longer exist", errors[-1]["payload"].get("text", ""))

    # ---- partial enablement falls back to the default -------------------

    def test_build_only_uses_build_model_no_planning(self):
        agents.save({"build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit()
        self.assertEqual(self.calls, [("build-model", False)],
                         "build-only + Build mode: no planning pass, build model, no plan context")

    def test_plan_only_plans_on_plan_model_then_builds_on_default(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"}})
        self.submit(mode="plan-build")
        self.assertEqual([m for m, _ in self.calls], ["plan-model", "default-model"],
                         "plan->build: build falls back to the session default model")
        build_call = next(c for c in self.calls if c[0] == "default-model")
        self.assertTrue(build_call[1], "the plan still reaches the default build agent")

    def test_enabled_but_unset_model_uses_default_model(self):
        agents.save({"plan": {"enabled": True}, "build": {"enabled": True}})
        self.submit(mode="plan-build")
        # Planning still runs (a dedicated pass) but on the default model, and
        # building continues on the default model too.
        self.assertTrue(all(m == "default-model" for m, _ in self.calls))
        self.assertEqual(len(self.calls), 2)

    def test_one_request_and_one_plan_phase_across_two_turns_no_dup_user_msg(self):
        # A second turn re-plans, but the transcript still holds each user
        # message exactly once and both turn phases run per submit.
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit("first task", mode="plan-build")
        self.assertEqual(self.transcript_users()[0], "first task")
        self.assertEqual(len(self.transcript_users()), 2, "user + one plan handoff")
        self.dispatch(op="submit", session="s", text="second task", mode="plan-build")
        self.assertTrue(self.done("s"))
        users = self.transcript_users()
        self.assertEqual([u for u in users if not u.startswith("<plan>")],
                         ["first task", "second task"], "user messages are not duplicated")


class PlanFailure(RouteBase):
    """A planner that never emits a finished plan must not hand a fake plan to
    the build agent or start building on its own."""

    def test_narration_is_not_a_plan_and_failure_builds_nothing(self):
        from harness.agent import PLAN_MAX_TURNS
        # The plan model keeps "exploring" (a tool call every turn) and never
        # produces a clean final answer, so it exhausts its turns.
        def explore(messages, tools=None, on_delta=None, cancel=None):
            model = providers.active_config()[2]
            if model == "plan-model":
                self.calls.append((model, True))
                return reply("Let me try a different approach for this exploration:",
                             [("read_file", {"path": "nope_missing.py"})]), USAGE
            self.calls.append((model, False))
            return reply("built without a real plan"), USAGE
        self.llm.side_effect = explore
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")

        models = [m for m, _ in self.calls]
        # The planner keeps replaying the same step: the no-progress guard (and
        # then the bounded replan) cut it off, so it never burns a full turn and
        # never finalises.
        self.assertGreaterEqual(models.count("plan-model"), 1, "the planner ran")
        self.assertLess(models.count("plan-model"), PLAN_MAX_TURNS * 3,
                        "the planner is bounded by its budget and no-progress guard")
        self.assertNotIn("build-model", models, "build must not start on a failed plan")
        failed = [m for m in self.sent if m.get("event") == events.PLAN
                  and (m.get("payload", {}) or {}).get("status") == "failed"]
        self.assertTrue(failed, "planning failure is surfaced, not hidden")
        # the incidental narration is never stored as a plan
        self.assertFalse(any(u.startswith("<plan>") for u in self.transcript_users()),
                          "no fabricated plan handoff is persisted")


class PlanApproval(RouteBase):
    """The optional review-before-building pause (plan.approve).

    Reuses RouteCase's providers, mocked coder, and backend, and adds an
    approval responder so the runner can actually pause and be answered. With
    the toggle off (RouteCase's other tests) nothing changes; with it on, the
    plan is shown, the user decides, and only an approval builds.
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

    def submit_with_plan(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan", "approve": True},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")

    def test_off_by_default_means_no_pause(self):
        # approve is not set -> the toggle defaults off, so no approval is
        # requested and plan flows straight into build (today's behaviour).
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": True, "model_id": "mdl-build"}})
        self.submit(mode="plan-build")
        self.assertEqual(self.approvals(), [], "no approval request without the toggle")
        self.assertEqual([m for m, _ in self.calls], ["plan-model", "build-model"])

    def test_approval_prompts_with_the_plan_visible(self):
        self.submit_with_plan()
        asks = self.approvals()
        self.assertTrue(asks, "the plan review asks before building")
        self.assertEqual(asks[0].get("tool"), "plan")
        self.assertIn("PLAN_BODY", asks[0].get("why", ""), "the plan is shown to the user")

    def test_building_only_happens_on_approval(self):
        self.approval_answer = True
        self.submit_with_plan()
        self.assertEqual([m for m, _ in self.calls], ["plan-model", "build-model"])
        build_call = next(c for c in self.calls if c[0] == "build-model")
        self.assertTrue(build_call[1], "the approved plan reaches the build agent")
        # the plan handoff is persisted for inspectability
        users = self.transcript_users()
        self.assertTrue(any(u.startswith("<plan>") for u in users))

    def test_skipping_builds_nothing_but_keeps_the_plan(self):
        self.approval_answer = False
        self.submit_with_plan()
        self.assertEqual([m for m, _ in self.calls], ["plan-model"],
                         "a skipped plan never reaches the build agent")
        # the plan is kept as an assistant message so the user can still read it
        contents = []
        sid = self.sid()
        for line in (paths_mod.session_dir() / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("role") == "assistant":
                contents.append(entry.get("content") or "")
        self.assertTrue(any("PLAN_BODY" in c for c in contents), "the plan is retained on skip")
        skipped = [m for m in self.sent if m.get("event") == events.NOTE
                   and (m.get("payload", {}) or {}).get("kind") == "plan_skipped"]
        self.assertTrue(skipped, "a skip is surfaced to the user")


class PlanArtifact(RouteBase):
    """The plan is a session-scoped artifact: persisted, reloadable, isolated."""

    def test_plan_is_persisted_for_its_own_session_only(self):
        from harness import plan as plan_mod
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": False}})
        self.submit(mode="plan-build")
        sid = self.sid()
        record = plan_mod.load(sid)
        self.assertIsNotNone(record, "the plan is persisted for its session")
        self.assertEqual(record.get("completion"), plan_mod.COMPLETE)
        self.assertEqual(record.get("session_id"), sid)
        self.assertIn("step one", record.get("plan", ""), "the plan text is stored")
        # No leak: an unrelated session id has nothing.
        self.assertIsNone(plan_mod.load("20990101-000000"))

    def test_get_plan_op_returns_the_persisted_record(self):
        from harness import plan as plan_mod
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": False}})
        self.submit(mode="plan-build")
        sid = self.sid()
        self.sent.clear()
        self.backend.dispatch({"op": "get_plan", "session": "s", "id": sid,
                               "req_id": time.time_ns()})
        replies = [m for m in self.sent if m.get("reply") == "get_plan"]
        self.assertTrue(replies and replies[-1].get("ok"), "the op succeeds")
        data = replies[-1].get("data") or {}
        self.assertEqual(data.get("completion"), plan_mod.COMPLETE)
        self.assertEqual(data.get("session_id"), sid)


if __name__ == "__main__":
    unittest.main(verbosity=2)