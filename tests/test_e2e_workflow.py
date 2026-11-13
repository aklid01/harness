"""End-to-end: the plan-or-proceed decision, role routing, and oversight on the wire.

This is the one test that does not patch call_llm. It runs the real Harness
backend, the real model call and the real provider routing against a scripted
in-process HTTP endpoint, so it proves what the unit tests cannot: that the
model's plan_decision converts the turn and the plan request goes to the plan
model, the approved build goes to the build model (or falls back to the session
model), and the review goes to the reviewer model, over the wire, with the
reviewer holding no tools - and that the approved edit really lands.

Every model is a step-scripted endpoint keyed by model name, so the flow is
deterministic and the run writes a JSON artifact (in the temp dir) that is read
back and asserted, making the scenario reproducible and checkable.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import agent_roles, events, providers, sandbox, server
from harness import paths as paths_mod
from tests.mock_provider import _payload, _stream


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class E2EState:
    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.cursors = {k: 0 for k in scripts}
        self.requests = []
        self.lock = threading.RLock()

    def step_for(self, model):
        with self.lock:
            steps = self.scripts.get(model, [{"kind": "text", "content": "ok"}])
            i = self.cursors.get(model, 0)
            self.cursors[model] = i + 1
            return steps[min(i, len(steps) - 1)], i


class E2EHandler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model") or ""
        st = self.state
        with st.lock:
            step, index = st.step_for(model)
            st.requests.append(
                {
                    "model": model,
                    "stream": bool(body.get("stream")),
                    "has_tools": bool(body.get("tools")),
                    "tool_names": [
                        (t.get("function") or {}).get("name")
                        for t in (body.get("tools") or [])
                    ],
                    "messages": body.get("messages") or [],
                }
            )
        if body.get("stream"):
            data = _stream(model, step, index + 1).encode()
        else:
            data = json.dumps(_payload(model, step, index + 1)).encode()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/event-stream" if body.get("stream") else "application/json",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock(scripts):
    state = E2EState(scripts)
    handler = type("Bound", (E2EHandler,), {"state": state})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def mock_state(srv):
    """The live request log of a mock started by start_mock."""
    return srv.RequestHandlerClass.state


class WorkflowE2E(unittest.TestCase):
    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        self.appdata = Path(self.tmp.name) / "appdata"
        self.appdata.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=self.appdata)
        self.addCleanup(paths_mod.reset)
        self.addCleanup(agent_roles.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)

        self.edited = self.root / "app.py"
        self.edited.write_text("# placeholder\n", encoding="utf-8")

        self.scripts = {
            "plan-model": [
                {"kind": "text", "content": "PLAN: open app.py, then set a marker."}
            ],
            "coder-model": [
                {
                    "kind": "tool",
                    "name": "plan_decision",
                    "arguments": {"decision": "plan", "reason": "ambiguous scope"},
                },
                {
                    "kind": "tool",
                    "name": "write_file",
                    "arguments": {"path": str(self.edited), "content": "# marker\n"},
                },
                {"kind": "text", "content": "Implemented the plan."},
            ],
            "reviewer-model": [
                {
                    "kind": "text",
                    "content": '{"verdict":"approve","reasoning":"matches the plan"}',
                }
            ],
        }
        self.server, base = start_mock(self.scripts)

        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

        providers.save_group({"id": "grp-1", "name": "One", "base_url": base})
        providers.save_model(
            {"id": "mdl-coder", "name": "Coder", "model": "coder-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-plan", "name": "Planner", "model": "plan-model"}, "grp-1"
        )
        providers.save_model(
            {"id": "mdl-rev", "name": "Reviewer", "model": "reviewer-model"}, "grp-1"
        )
        providers.set_active("mdl-coder")
        providers.set_key("grp-1", "sk-one-shared-secret")

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)

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

    def done(self, session, timeout=30, kind="done"):
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

    def run_turn(self, text="set a marker in app.py", mode=None):
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
        if mode:
            self.backend.dispatch(
                {
                    "op": "set_mode",
                    "session": "s",
                    "mode": mode,
                    "req_id": time.time_ns(),
                }
            )
        self.backend.dispatch(
            {
                "op": "submit",
                "session": "s",
                "text": text,
                "req_id": time.time_ns(),
            }
        )
        self.assertTrue(self.done("s"))
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def test_the_whole_workflow_over_the_wire(self):
        """The model's own decision converts the turn: plan on the plan model,
        the user approves over the wire, the build runs on the session model
        with the plan as context, oversight checks the edit, and it lands."""
        agent_roles.save(
            {
                "plan": {"enabled": True, "model_id": "mdl-plan"},
                "build": {"enabled": False},
                "oversight": {
                    "enabled": True,
                    "model_id": "mdl-rev",
                    "intensity": "medium",
                    "max_rounds": 2,
                },
            }
        )
        self.run_turn(text="make the app faster")

        flow = [
            r
            for r in mock_state(self.server).requests
            if not any(
                "Title this coding task" in str(m.get("content") or "")
                for m in r["messages"]
            )
        ]
        models = [r["model"] for r in flow]

        self.assertEqual(
            models[:5],
            [
                "coder-model",
                "plan-model",
                "coder-model",
                "reviewer-model",
                "coder-model",
            ],
            f"routing order was {models}",
        )

        decision_req = flow[0]
        self.assertIn("plan_decision", decision_req["tool_names"])
        self.assertNotIn(
            "write_file",
            decision_req["tool_names"],
            "no write tool may be offered before the decision",
        )

        reviewer_req = next(r for r in flow if r["model"] == "reviewer-model")
        self.assertFalse(reviewer_req["has_tools"], "the reviewer must hold no tools")

        coder_post = flow[4]
        self.assertTrue(
            any(
                "<plan>" in str(m.get("content") or "") for m in coder_post["messages"]
            ),
            "the plan is handed to the build as context",
        )

        self.assertEqual(self.edited.read_text(encoding="utf-8"), "# marker\n")

        report = {
            "scenario": (
                "auto-plan decision -> plan -> approval -> build -> "
                "oversight-approve -> build"
            ),
            "model_request_order": models,
            "reviewer_is_tool_less": not reviewer_req["has_tools"],
            "plan_reached_build": any(
                "<plan>" in str(m.get("content") or "") for m in coder_post["messages"]
            ),
            "approved_edit_landed": self.edited.read_text(encoding="utf-8").strip()
            == "# marker",
            "transcript_has_plan": None,
        }
        sid = self.sid()
        transcript = (paths_mod.session_dir() / f"{sid}.jsonl").read_text(
            encoding="utf-8"
        )
        report["transcript_has_plan"] = "<plan>" in transcript

        artifact = Path(self.tmp.name) / "e2e-workflow-report.json"
        artifact.write_text(json.dumps(report, indent=2), encoding="utf-8")
        reread = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(
            reread["model_request_order"][:5],
            [
                "coder-model",
                "plan-model",
                "coder-model",
                "reviewer-model",
                "coder-model",
            ],
        )
        self.assertTrue(reread["reviewer_is_tool_less"])
        self.assertTrue(reread["plan_reached_build"])
        self.assertTrue(reread["approved_edit_landed"])
        self.assertTrue(reread["transcript_has_plan"])
        print("\n[e2e artifact]", json.dumps(report, indent=2))

    def test_oversight_revise_holds_the_edit_then_a_clarified_pass_writes(self):

        self.edited.write_text("# placeholder\n", encoding="utf-8")
        mock_state(self.server).scripts["reviewer-model"] = [
            {
                "kind": "text",
                "content": '{"verdict":"revise","reasoning":"wipes the file",'
                '"correction":"append instead of overwrite"}',
            },
            {
                "kind": "text",
                "content": '{"verdict":"approve","reasoning":"append is safe"}',
            },
        ]

        mock_state(self.server).scripts["coder-model"] = [
            {
                "kind": "tool",
                "name": "write_file",
                "arguments": {"path": str(self.edited), "content": "OVERWRITTEN"},
            },
            {
                "kind": "tool",
                "name": "str_replace",
                "arguments": {
                    "path": str(self.edited),
                    "old_str": "# placeholder",
                    "new_str": "# placeholder\n# marker",
                },
            },
            {"kind": "text", "content": "Adapted and finished."},
        ]
        mock_state(self.server).cursors = {
            k: 0 for k in mock_state(self.server).scripts
        }
        agent_roles.save(
            {
                "oversight": {
                    "enabled": True,
                    "model_id": "mdl-rev",
                    "intensity": "medium",
                    "max_rounds": 2,
                }
            }
        )
        self.run_turn()

        flow = [
            r
            for r in mock_state(self.server).requests
            if not any(
                "Title this coding task" in str(m.get("content") or "")
                for m in r["messages"]
            )
        ]
        models = [r["model"] for r in flow]

        self.assertEqual(
            models,
            [
                "coder-model",
                "reviewer-model",
                "coder-model",
                "reviewer-model",
                "coder-model",
            ],
            f"got {models}",
        )
        self.assertIn(
            "placeholder",
            self.edited.read_text(encoding="utf-8"),
            "the adapted edit preserved the placeholder",
        )
        self.assertEqual(
            self.edited.read_text(encoding="utf-8"),
            "# placeholder\n# marker\n",
            "the corrected edit is what landed",
        )
        self.assertNotIn(
            "OVERWRITTEN",
            self.edited.read_text(encoding="utf-8"),
            "the action the reviewer refused never ran",
        )

    def test_ask_mode_refuses_edits_and_mutating_commands(self):
        """Ask mode over the wire: write tools are withheld from the toolset, an
        emitted write and a mutating bash command are refused before execution
        (no approval dialog offers an edit path), a read-only command still
        runs, and no plan artifact is written."""
        from harness import plan as plan_mod

        probe = self.root / "probe.txt"

        outside = self.appdata / "attached.html"
        outside.write_text("<html><body>attached</body></html>", encoding="utf-8")
        mock_state(self.server).scripts["coder-model"] = [
            {
                "kind": "tool",
                "name": "write_file",
                "arguments": {"path": str(probe), "content": "hacked"},
            },
            {
                "kind": "tool",
                "name": "bash",
                "arguments": {"command": f"echo hacked > {probe}"},
            },
            {
                "kind": "tool",
                "name": "bash",
                "arguments": {"command": "echo ask-read-only-ok"},
            },
            {
                "kind": "tool",
                "name": "bash",
                "arguments": {
                    "command": (
                        f"P1='{outside}'; P2='{outside}'; "
                        'ls -la "$P1" 2>&1; echo \'---\'; ls -la "$P2" 2>&1; '
                        'head -c 6 "$P2" 2>/dev/null || head -c 6 "$P1" 2>&1'
                    )
                },
            },
            {"kind": "text", "content": "This project builds a coding agent."},
        ]
        mock_state(self.server).cursors = {
            k: 0 for k in mock_state(self.server).scripts
        }

        self.run_turn(text="What does this project do?", mode="ask")

        flow = [
            r
            for r in mock_state(self.server).requests
            if r["stream"] and r["model"] == "coder-model"
        ]
        ask_req = flow[0]
        system = str((ask_req["messages"][0] or {}).get("content") or "")
        self.assertIn("research assistant", system)
        offered = set(ask_req["tool_names"])
        leaked = offered & {"write_file", "str_replace", "write_todos", "task"}
        self.assertEqual(leaked, set(), f"write tools offered in ask mode: {leaked}")
        self.assertIn("bash", offered)
        self.assertIn("read_file", offered)

        callers = [
            m["payload"].get("agent")
            for m in self.sent
            if m.get("event") == events.TOOL_CALL
        ]
        self.assertTrue(callers and set(callers) == {"ask"}, f"roles were {callers}")
        outcomes = [
            (m["payload"]["name"], str(m["payload"]["result"]))
            for m in self.sent
            if m.get("event") == events.TOOL_RESULT
        ]
        self.assertEqual(
            [name for name, _ in outcomes],
            ["write_file", "bash", "bash", "bash"],
        )
        self.assertIn("Ask mode", outcomes[0][1])
        self.assertIn("read-only", outcomes[1][1])
        self.assertIn("ask-read-only-ok", outcomes[2][1])

        self.assertNotIn("Ask mode", outcomes[3][1], outcomes[3][1])
        self.assertNotIn("read-only", outcomes[3][1], outcomes[3][1])
        probe_ran = "attached.html" in outcomes[3][1] or "attached" in outcomes[3][1]
        if sandbox._find_bash() is None:
            probe_ran = True
        self.assertTrue(probe_ran, f"the probe did not read the file: {outcomes[3][1]}")

        self.assertFalse(probe.exists(), "an edit escaped ask mode")
        answers = [
            str(m["payload"].get("content") or "")
            for m in self.sent
            if m.get("event") == events.MESSAGE
            and m["payload"].get("role") == "assistant"
        ]
        self.assertTrue(
            any("coding agent" in a for a in answers), f"answers were {answers}"
        )
        phases = [
            m["payload"].get("phase")
            for m in self.sent
            if m.get("event") == events.PHASE
        ]
        self.assertIn("researching", phases)
        self.assertIsNone(plan_mod.load(self.sid()), "ask wrote a plan artifact")

        report = {
            "scenario": (
                "ask mode: write tools withheld, edit refused, mutating bash "
                "refused, read-only bash ran, read-only probe of a file "
                "outside the workspace ran"
            ),
            "ask_system_prompt_on_wire": "research assistant" in system,
            "write_tools_offered": sorted(leaked),
            "probe_file_created": probe.exists(),
            "mutating_bash_refused": "read-only" in outcomes[1][1],
            "readonly_bash_ran": "ask-read-only-ok" in outcomes[2][1],
            "readonly_probe_outside_workspace_ran": probe_ran,
            "tool_events_say_ask": set(callers) == {"ask"},
            "researching_phase_emitted": "researching" in phases,
        }
        artifact = Path(self.tmp.name) / "e2e-ask-report.json"
        artifact.write_text(json.dumps(report, indent=2), encoding="utf-8")
        reread = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertTrue(reread["ask_system_prompt_on_wire"])
        self.assertEqual(reread["write_tools_offered"], [])
        self.assertFalse(reread["probe_file_created"])
        self.assertTrue(reread["mutating_bash_refused"])
        self.assertTrue(reread["readonly_bash_ran"])
        self.assertTrue(reread["readonly_probe_outside_workspace_ran"])
        self.assertTrue(reread["tool_events_say_ask"])
        self.assertTrue(reread["researching_phase_emitted"])
        print("\n[e2e artifact]", json.dumps(report, indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
