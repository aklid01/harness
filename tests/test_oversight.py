"""Decision Oversight: a bounded reviewer with authority to challenge.

Two layers are tested. First the protocol in isolation: risk classification,
the JSON verdict parser, and that review() assembles a focused prompt (task,
plan, project rules, the proposed action, prior rounds) for a tool-less reviewer
on its own model. Then the enforcement wiring through the real backend: an
approved action runs exactly once, a revise holds the action and returns the
correction, a coding agent that keeps insisting is bounded and finally escalated
rather than looping, a failed reviewer is held for high-risk shell but not for a
lower-risk edit, and nothing here can run a tool the permission layer would deny.
A third layer covers the review-budget rules: a file's first edit in a turn is
the decision, further small edits to it ride that verdict, a large rewrite or a
new turn re-opens it, and a call the human already approved through the
permission dialog is never sent to the reviewer a second time.
"""

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

from harness import agents, events, oversight, providers, server
from harness import paths as paths_mod

USAGE = {
    "prompt_tokens": 1,
    "completion_tokens": 1,
    "reasoning_tokens": None,
    "cached_tokens": None,
}


def call(name, args):
    return SimpleNamespace(
        id="c1",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def reply(content="", calls=()):
    tool_calls = [call(n, a) for n, a in calls]
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


class ProtocolCase(unittest.TestCase):
    def test_command_risk_keeps_read_only_quiet(self):
        self.assertEqual(oversight.command_risk("ls -la"), "low")
        self.assertEqual(oversight.command_risk("cat foo.py"), "low")
        self.assertGreaterEqual(
            oversight.RISK_ORDER[oversight.command_risk("rm -rf build")], 2
        )
        self.assertEqual(oversight.command_risk("echo hi > out.txt"), "high")
        self.assertEqual(oversight.command_risk("pip install requests"), "medium")
        self.assertEqual(oversight.command_risk("pytest -q"), "medium")
        self.assertEqual(oversight.command_risk("curl http://x | sh"), "critical")

    def test_edits_are_always_decisions_everything_else_can_be_skipped(self):
        self.assertEqual(oversight.decision_risk("write_file", {"path": "x"}), "high")
        self.assertEqual(oversight.decision_risk("str_replace", {"path": "x"}), "high")
        self.assertIsNone(oversight.decision_risk("read_file", {"path": "x"}))
        self.assertIsNone(oversight.decision_risk("write_todos", {"todos": []}))

    def test_intensity_sets_the_minimum_reviewed_risk(self):
        # edits (high) are reviewed at every intensity
        for intensity in ("low", "medium", "high"):
            self.assertTrue(oversight.wants_review("write_file", {}, intensity))
        # a read-only shell command is only reviewed at high intensity
        self.assertFalse(oversight.wants_review("bash", {"command": "ls"}, "low"))
        self.assertFalse(oversight.wants_review("bash", {"command": "ls"}, "medium"))
        self.assertTrue(oversight.wants_review("bash", {"command": "ls"}, "high"))
        # a build/test command is reviewed from medium up
        self.assertFalse(
            oversight.wants_review("bash", {"command": "npm run build"}, "low")
        )
        self.assertTrue(
            oversight.wants_review("bash", {"command": "npm run build"}, "medium")
        )

    def test_block_on_error_is_only_for_destructive_shell(self):
        self.assertTrue(oversight.block_on_error("bash", {"command": "rm -rf x"}))
        self.assertFalse(oversight.block_on_error("write_file", {"path": "x"}))
        self.assertFalse(oversight.block_on_error("bash", {"command": "ls"}))

    def test_parse_verdict_reads_plain_json_fenced_and_broken(self):
        v = oversight.parse_verdict(
            '{"verdict":"revise","reasoning":"r","correction":"c"}'
        )
        self.assertEqual((v.verdict, v.correction), ("revise", "c"))
        fenced = oversight.parse_verdict('```json\n{"verdict":"approve"}\n```')
        self.assertEqual(fenced.verdict, "approve")
        broken = oversight.parse_verdict("I think this is fine, approve it.")
        self.assertEqual(broken.verdict, "clarify")  # never silently approve
        self.assertTrue(broken.error)
        bad = oversight.parse_verdict('{"verdict":"banana"}')
        self.assertEqual(bad.verdict, "clarify")

    def test_parse_verdict_normalises_risks(self):
        v = oversight.parse_verdict('{"verdict":"escalate","risks":["a","b"]}')
        self.assertEqual(v.risks, ["a", "b"])


class ReviewerContextCase(unittest.TestCase):
    """review() builds the right prompt for the right model, tool-less."""

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
        (self.root / "AGENTS.md").write_text(
            "- Project rule: single quotes.", encoding="utf-8"
        )
        providers.save_group(
            {"id": "grp-1", "name": "P", "base_url": "https://x.example/v1"}
        )
        providers.save_model(
            {"id": "mdl-default", "name": "D", "model": "default-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-rev", "name": "Reviewer", "model": "reviewer-model"}, "grp-1"
        )
        providers.set_active("mdl-default")

    def _capture(self):
        captured = {}

        def fake(messages, tools=None, on_delta=None, on_reasoning=None, cancel=None):
            captured["model"] = providers.active_config()[2]
            captured["messages"] = messages
            captured["tools"] = tools
            return reply('{"verdict":"approve"}'), USAGE

        return captured, fake

    def test_prompt_carries_task_plan_rules_action_and_reviewer_notes(self):
        captured, fake = self._capture()
        with mock.patch("harness.oversight.call_llm", side_effect=fake):
            verdict, _ = oversight.review(
                "mdl-rev",
                "be strict about security",
                task="Refactor the parser",
                plan="1. touch parser.py",
                name="str_replace",
                args={
                    "path": "parser.py",
                    "old_str": "OLD_MARKER",
                    "new_str": "NEW_MARKER",
                },
                note="because it is cleaner",
            )
        self.assertEqual(
            captured["model"], "reviewer-model", "the reviewer uses its own model"
        )
        self.assertEqual(captured["tools"], [], "the reviewer holds no tools")
        body = captured["messages"][1]["content"]
        self.assertIn("Refactor the parser", body)  # task
        self.assertIn("1. touch parser.py", body)  # plan
        self.assertIn("Project rule: single quotes", body)  # project instructions
        self.assertIn("be strict about security", body)  # reviewer-specific notes
        self.assertIn("parser.py", body)  # the action
        self.assertIn("OLD_MARKER", body)  # what changes
        self.assertIn("NEW_MARKER", body)  # what it becomes
        self.assertIn("because it is cleaner", body)  # the agent's stated reason
        self.assertEqual(verdict.verdict, "approve")

    def test_prior_rounds_travel_with_a_challenge(self):
        captured, fake = self._capture()
        with mock.patch("harness.oversight.call_llm", side_effect=fake):
            oversight.review(
                "mdl-rev",
                "",
                task="t",
                plan="",
                name="write_file",
                args={"path": "a", "content": "c"},
                note="I insist, the test needs it",
                history=[{"verdict": "revise", "correction": "back it up first"}],
            )
        body = captured["messages"][1]["content"]
        self.assertIn("Prior review rounds", body)
        self.assertIn("back it up first", body)
        self.assertIn(
            "I insist, the test needs it",
            body,
            "the counterargument reaches the reviewer",
        )

    def test_a_failed_review_is_a_verdict_not_a_crash(self):
        def boom(messages, tools=None, on_delta=None, on_reasoning=None, cancel=None):
            raise RuntimeError("reviewer timed out")

        with mock.patch("harness.oversight.call_llm", side_effect=boom):
            verdict, usage = oversight.review(
                "mdl-rev", "", task="t", plan="", name="write_file", args={"path": "a"}
            )
        self.assertEqual(verdict.verdict, "error")
        self.assertIn("timed out", verdict.error)

    def test_no_checker_configured_is_an_error_verdict(self):
        verdict, _ = oversight.review(
            "", "", task="t", plan="", name="write_file", args={}
        )
        self.assertEqual(verdict.verdict, "error")


class GateCase(unittest.TestCase):
    """Enforcement through the real backend, with the coder and reviewer mocked."""

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

        providers.save_group(
            {"id": "grp-1", "name": "P", "base_url": "https://x.example/v1"}
        )
        providers.save_model(
            {"id": "mdl-default", "name": "Coder", "model": "coder-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-plan", "name": "Planner", "model": "plan-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-rev", "name": "Rev", "model": "reviewer-model"}, "grp-1"
        )
        providers.set_active("mdl-default")

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)

        self.coding = []  # queued coding-agent replies
        self.reviews = []  # queued reviewer verdicts (dicts or ("error", msg))
        self.review_calls = []  # history kwarg of each review()
        self._ci = 0
        self._ri = 0
        self.coder_patch = mock.patch("harness.agent.call_llm", side_effect=self._code)
        self.rev_patch = mock.patch(
            "harness.oversight.review", side_effect=self._review
        )
        self.coder_patch.start()
        self.rev_patch.start()
        self.addCleanup(mock.patch.stopall)
        # Answer approval/pick requests the way a frontend would, so a call that
        # needs the human never blocks the test. Denied by default.
        import threading

        self._stop_responder = threading.Event()
        self._handled = set()
        self._responder = threading.Thread(target=self._auto_responder, daemon=True)
        self._responder.start()
        self.addCleanup(self._stop_responder.set)

    def _auto_responder(self):
        while not self._stop_responder.wait(0.02):
            for m in list(self.sent):
                rid, req = m.get("id"), m.get("request")
                if rid and req in ("approval", "pick") and rid not in self._handled:
                    self._handled.add(rid)
                    s = self.backend.sessions.get(m.get("session"))
                    if s:
                        s.reply(rid, False if req == "approval" else None)

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def _code(self, messages, tools=None, on_delta=None, on_reasoning=None, cancel=None):
        if self._ci < len(self.coding):
            msg = self.coding[self._ci]
        else:
            msg = reply("done")
        self._ci += 1
        return msg, USAGE

    def _review(
        self,
        checker_id,
        instructions,
        *,
        task,
        plan,
        name,
        args,
        note="",
        history=None,
        cancel=None,
    ):
        self.review_calls.append(
            {"history": list(history or []), "name": name, "args": args, "task": task}
        )
        if self._ri < len(self.reviews):
            spec = self.reviews[self._ri]
        else:
            spec = {"verdict": "approve"}
        self._ri += 1
        if isinstance(spec, tuple) and spec[0] == "error":
            return oversight.Verdict("error", error=spec[1]), USAGE
        return oversight.Verdict(
            verdict=spec["verdict"],
            reasoning=spec.get("reasoning", ""),
            risks=spec.get("risks", []),
            correction=spec.get("correction", ""),
            question=spec.get("question", ""),
        ), USAGE

    def enable(self, **over_cfg):
        # Oversight is independent of the workflow, so a plain Build-mode session
        # is all it needs; the coder is the session default model.
        cfg = {
            "plan": {"enabled": False},
            "build": {"enabled": False},
            "oversight": {
                "enabled": True,
                "model_id": "mdl-rev",
                "intensity": "medium",
                "max_rounds": 2,
            },
        }
        cfg["oversight"].update(over_cfg)
        agents.save(cfg)

    def done(self, session, timeout=20, kind="done"):
        limit = time.time() + timeout
        while time.time() < limit:
            if any(
                m.get("event") == kind and m.get("session") == session
                for m in self.sent
            ):
                return True
            time.sleep(0.02)
        return False

    def sid(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                return m["payload"]["id"]
        return None

    def run_submit(self):
        self.backend.dispatch(
            {
                "op": "open",
                "session": "s",
                "root": str(self.root),
                "req_id": time.time_ns(),
            }
        )
        self.assertTrue(self.done("s", kind="session"))
        self.sent.clear()
        self.backend.dispatch(
            {
                "op": "submit",
                "session": "s",
                "text": "do the thing",
                "mode": "build",
                "req_id": time.time_ns(),
            }
        )
        self.assertTrue(self.done("s"))
        time.sleep(0.1)

    def decisions(self):
        return [m["payload"] for m in self.sent if m.get("event") == events.DECISION]

    def decision_statuses(self):
        return [d.get("status") for d in self.decisions()]

    def target(self, name="foo.txt"):
        return self.root / name

    # ---- approve --------------------------------------------------------

    def test_approve_runs_the_action_once(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("a.txt")), "content": "X"})],
            )
        ]
        self.reviews = [{"verdict": "approve"}]
        self.run_submit()
        self.assertTrue((self.target("a.txt")).exists(), "an approved edit runs")
        self.assertEqual(self.target("a.txt").read_text(), "X")
        self.assertIn("approved", self.decision_statuses())
        self.assertEqual(len(self.review_calls), 1)

    # ---- revise then adapt ---------------------------------------------

    def test_revise_holds_the_action_and_a_fresh_approach_runs(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [
                    (
                        "write_file",
                        {"path": str(self.target("bad.txt")), "content": "bad"},
                    )
                ],
            ),
            reply(
                "",
                [
                    (
                        "write_file",
                        {"path": str(self.target("good.txt")), "content": "good"},
                    )
                ],
            ),
        ]
        self.reviews = [
            {
                "verdict": "revise",
                "reasoning": "do not write bad.txt",
                "correction": "write good.txt",
            },
            {"verdict": "approve"},
        ]
        self.run_submit()
        self.assertFalse(self.target("bad.txt").exists(), "a revised action is not run")
        self.assertTrue(self.target("good.txt").exists(), "the adapted action runs")
        self.assertIn("revised", self.decision_statuses())
        self.assertIn("approved", self.decision_statuses())
        # the held feedback carried the correction back to the coding agent
        transcript = (paths_mod.session_dir() / f"{self.sid()}.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("write good.txt", transcript)

    # ---- challenge, resolved --------------------------------------------

    def test_coding_agent_can_challenge_and_the_reviewer_reconsiders(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [
                    (
                        "write_file",
                        {"path": str(self.target("f.txt")), "content": "keep"},
                    )
                ],
            ),
            reply(
                "I insist the test needs this",
                [
                    (
                        "write_file",
                        {
                            "path": str(
                                self.target("f.txt"),
                            ),
                            "content": "keep",
                        },
                    )
                ],
            ),
        ]
        self.reviews = [
            {"verdict": "revise", "reasoning": "risky", "correction": "nope"},
            {"verdict": "approve", "reasoning": "the justification holds"},
        ]
        self.run_submit()
        self.assertTrue(self.target("f.txt").exists(), "the challenge was accepted")
        self.assertEqual(
            len(self.review_calls), 2, "two review rounds on the same decision"
        )
        # the second review saw the first revision as history
        self.assertEqual(self.review_calls[1]["history"][0]["verdict"], "revise")

    # ---- bounded escalation ---------------------------------------------

    def test_repeated_disagreement_is_bounded_and_escalated(self):
        self.enable(max_rounds=2)
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
        ]
        self.reviews = [{"verdict": "revise", "correction": "no"} for _ in range(10)]
        self.run_submit()
        self.assertFalse(
            self.target("f.txt").exists(), "an unresolved action is never run"
        )
        self.assertEqual(
            len(self.review_calls),
            2,
            "the reviewer is consulted only max_rounds times, then it escalates",
        )
        self.assertEqual(self.decision_statuses()[-1], "escalated")
        stopped = [
            m
            for m in self.sent
            if m.get("event") == events.NOTE
            and (m.get("payload", {}) or {}).get("kind") == "oversight_stopped"
        ]
        self.assertTrue(stopped, "the turn stops instead of looping")

    def test_a_reviewer_escalation_stops_the_turn(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
        ]
        self.reviews = [{"verdict": "escalate", "reasoning": "needs the user"}]
        self.run_submit()
        self.assertFalse(self.target("f.txt").exists())
        self.assertIn("escalated", self.decision_statuses())
        self.assertTrue(
            any(
                m.get("event") == events.NOTE
                and (m.get("payload", {}) or {}).get("kind") == "oversight_stopped"
                for m in self.sent
            )
        )

    # ---- failure policy --------------------------------------------------

    def test_reviewer_failure_holds_high_risk_shell(self):
        self.enable()
        self.coding = [reply("", [("bash", {"command": "rm -rf build"})]), reply("ok")]
        self.reviews = [("error", "rate limited")]
        self.run_submit()
        self.assertNotIn("approved", self.decision_statuses())
        self.assertIn("error", self.decision_statuses())
        held = [d for d in self.decisions() if d.get("status") == "error"]
        self.assertTrue(held)

    def test_reviewer_failure_lets_a_low_risk_edit_proceed_with_visible_note(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("e.txt")), "content": "y"})],
            )
        ]
        self.reviews = [("error", "timeout")]
        self.run_submit()
        self.assertTrue(
            self.target("e.txt").exists(),
            "a lower-risk action proceeds, still under the permission layer",
        )
        self.assertIn("skipped", self.decision_statuses())
        self.assertTrue(
            any(
                m.get("event") == events.NOTE
                and (m.get("payload", {}) or {}).get("kind") == "oversight_failed"
                for m in self.sent
            )
        )

    # ---- approvals and permissions are never bypassed --------------------

    def test_an_approved_edit_outside_the_project_is_still_denied(self):
        self.enable()
        outside = Path(self.tmp.name) / "elsewhere" / "secret.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        self.coding = [
            reply("", [("write_file", {"path": str(outside), "content": "z"})]),
            reply("ok"),
        ]
        self.reviews = [{"verdict": "approve"}]  # reviewer says yes...
        self.run_submit()
        # ...but the permission layer still owns the final word, and with no
        # approver connected the write is refused. Oversight cannot override it.
        self.assertFalse(outside.exists(), "approval is not bypassed by the reviewer")

    def test_oversight_without_a_configured_model_is_inert(self):
        # Independent feature: enabled with no reviewer model => inert; the write
        # proceeds unreviewed (in a normal Build session).
        agents.save({"oversight": {"enabled": True, "model_id": ""}})
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("g.txt")), "content": "g"})],
            )
        ]
        self.run_submit()
        self.assertEqual(len(self.review_calls), 0, "no reviewer model means no review")
        self.assertTrue(self.target("g.txt").exists(), "the write proceeds normally")
        self.assertEqual(self.decision_statuses(), [])

    def test_an_enabled_role_whose_model_was_deleted_is_inert(self):
        self.enable()
        providers.delete_model("mdl-rev")
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("h.txt")), "content": "h"})],
            )
        ]
        self.run_submit()
        self.assertEqual(
            len(self.review_calls), 0, "no silent fallback to another model"
        )
        self.assertTrue(self.target("h.txt").exists())

    def test_readonly_shell_is_not_reviewed_but_still_runs(self):
        # A stderr redirect and a plain echo are read-only: not a decision, so no
        # review - but they still execute under the permission layer.
        self.enable()
        self.coding = [reply("", [("bash", {"command": "echo hello"})]), reply("ok")]
        self.reviews = [{"verdict": "approve"}]
        self.run_submit()
        self.assertEqual(
            self.review_calls, [], "a harmless shell command is not a decision"
        )
        results = [
            m["payload"].get("result", "")
            for m in self.sent
            if m.get("event") == "tool_result"
        ]
        self.assertTrue(any("hello" in str(r) for r in results), "and it still ran")

    def test_a_file_write_via_shell_is_reviewed(self):
        self.enable()
        self.coding = [
            reply("", [("bash", {"command": "echo hi > out.txt"})]),
            reply("ok"),
        ]
        self.reviews = [
            {
                "verdict": "revise",
                "reasoning": "redirect",
                "correction": "use write_file",
            }
        ]
        self.run_submit()
        self.assertEqual(
            len(self.review_calls), 1, "a file-writing shell command is reviewed"
        )

    def test_a_held_revision_is_not_re_reviewed_as_a_new_decision(self):
        # The reviewer's own correction comes back as a tool result, not as a
        # tool call, so it must not recursively trigger another review; only the
        # coding agent re-proposing the same action does.
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "x"})],
            ),
            reply("I will adjust per the reviewer"),
        ]
        self.reviews = [{"verdict": "revise", "reasoning": "r", "correction": "c"}]
        self.run_submit()
        self.assertEqual(
            len(self.review_calls), 1, "the checker does not review its own verdict"
        )

    # ---- the review budget: one decision per file per turn ---------------

    def test_a_second_small_edit_to_a_reviewed_file_rides_the_verdict(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "one"})],
            ),
            reply(
                "",
                [
                    (
                        "str_replace",
                        {
                            "path": str(self.target("f.txt")),
                            "old_str": "one",
                            "new_str": "two",
                        },
                    )
                ],
            ),
            reply("ok"),
        ]
        self.reviews = [{"verdict": "approve"}]
        self.run_submit()
        self.assertEqual(
            len(self.review_calls),
            1,
            "the follow-up edit on the same file rides the approved verdict",
        )
        self.assertEqual(
            self.target("f.txt").read_text(), "two", "and the edit still ran"
        )
        self.assertEqual(
            [s for s in self.decision_statuses() if s != "reviewing"], ["approved"]
        )

    def test_a_large_rewrite_of_a_reviewed_file_is_re_reviewed(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "one"})],
            ),
            reply(
                "",
                [
                    (
                        "write_file",
                        {"path": str(self.target("f.txt")), "content": "x" * 5000},
                    )
                ],
            ),
            reply("ok"),
        ]
        self.reviews = [{"verdict": "approve"}, {"verdict": "approve"}]
        self.run_submit()
        self.assertEqual(
            len(self.review_calls),
            2,
            "a rewrite far larger than a follow-up is its own decision",
        )

    def resubmit(self, text):
        self.sent.clear()
        self.backend.dispatch(
            {
                "op": "submit",
                "session": "s",
                "text": text,
                "mode": "build",
                "req_id": time.time_ns(),
            }
        )
        self.assertTrue(self.done("s"))
        time.sleep(0.1)

    def test_a_new_turn_reviews_the_file_again(self):
        self.enable()
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "one"})],
            ),
            reply("first turn done"),
            reply(
                "",
                [
                    (
                        "str_replace",
                        {
                            "path": str(self.target("f.txt")),
                            "old_str": "one",
                            "new_str": "two",
                        },
                    )
                ],
            ),
            reply("second turn done"),
        ]
        self.reviews = [{"verdict": "approve"}, {"verdict": "approve"}]
        self.run_submit()
        self.resubmit("continue")
        self.assertEqual(
            len(self.review_calls),
            2,
            "each turn's first edit to a file is its own decision",
        )

    def test_low_intensity_skips_follow_up_edits_on_touched_files(self):
        self.enable(intensity="low")
        self.coding = [
            reply(
                "",
                [("write_file", {"path": str(self.target("f.txt")), "content": "one"})],
            ),
            reply("first turn done"),
            reply(
                "",
                [
                    (
                        "str_replace",
                        {
                            "path": str(self.target("f.txt")),
                            "old_str": "one",
                            "new_str": "two",
                        },
                    )
                ],
            ),
            reply("ok"),
        ]
        self.reviews = [{"verdict": "approve"}]
        self.run_submit()
        self.resubmit("continue")
        self.assertEqual(
            len(self.review_calls),
            1,
            "at low intensity a small follow-up on a touched file is not a decision",
        )
        self.assertEqual(self.target("f.txt").read_text(), "two", "and it still ran")

    def test_a_user_approved_call_skips_the_reviewer_when_reproposed(self):
        real_interact = events.interact
        approvals = []

        def approve(kind, payload):
            if kind == events.APPROVAL:
                approvals.append(payload)
                return True
            return real_interact(kind, payload)

        self.enable()
        self.coding = [
            reply("", [("bash", {"command": "pip install requests"})]),
            reply("", [("bash", {"command": "pip install requests"})]),
            reply("ok"),
        ]
        self.reviews = [{"verdict": "approve"}]
        with mock.patch("harness.events.interact", side_effect=approve):
            self.run_submit()
        self.assertEqual(
            len(self.review_calls),
            1,
            "the identical re-proposal rides the human's approval",
        )
        self.assertEqual(
            len(approvals), 2, "the permission layer still asks every time"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
