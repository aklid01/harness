"""End-to-end: plan agent, build agent, and decision oversight on the wire.

This is the one test that does not patch call_llm. It runs the real Harness
backend, the real model call and the real provider routing against a scripted
in-process HTTP endpoint, so it proves what the unit tests cannot: that
providers.use actually sends the plan request to the plan model, the build
request to the build model, and the review to the reviewer model, over the wire,
with the reviewer holding no tools - and that the approved edit really lands.

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

from tests.mock_provider import _payload, _stream  # noqa: E402

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import agents, events, providers, server  # noqa: E402
from harness import paths as paths_mod  # noqa: E402
from harness import session as session_mod  # noqa: E402


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
            st.requests.append({
                "model": model,
                "stream": bool(body.get("stream")),
                "has_tools": bool(body.get("tools")),
                "messages": body.get("messages") or [],
            })
        if body.get("stream"):
            data = _stream(model, step, index + 1).encode()
        else:
            data = json.dumps(_payload(model, step, index + 1)).encode()
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/event-stream" if body.get("stream") else "application/json")
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
        self.addCleanup(agents.reset_cache)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)

        self.edited = self.root / "app.py"
        self.edited.write_text("# placeholder\n", encoding="utf-8")

        self.scripts = {
            "plan-model": [{"kind": "text", "content": "PLAN: open app.py, then set a marker."}],
            "coder-model": [
                {"kind": "tool", "name": "write_file",
                 "arguments": {"path": str(self.edited), "content": "# marker\n"}},
                {"kind": "text", "content": "Implemented the plan."},
            ],
            "reviewer-model": [{"kind": "text",
                                "content": '{"verdict":"approve","reasoning":"matches the plan"}'}],
        }
        self.server, base = start_mock(self.scripts)
        # shutdown BEFORE server_close: closing the socket while serve_forever
        # is still selecting on it crashed the loop with WinError 10038 and
        # littered the suite output with tracebacks that masked real failures.
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

        providers.save_group({"id": "grp-1", "name": "One", "base_url": base})
        providers.save_model({"id": "mdl-coder", "name": "Coder", "model": "coder-model"}, "grp-1")
        providers.save_model({"id": "mdl-plan", "name": "Planner", "model": "plan-model"}, "grp-1")
        providers.save_model({"id": "mdl-rev", "name": "Reviewer", "model": "reviewer-model"}, "grp-1")
        providers.set_active("mdl-coder")
        providers.set_key("grp-1", "sk-one-shared-secret")

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def done(self, session, timeout=30, kind="done"):
        limit = time.time() + timeout
        while time.time() < limit:
            if any(m.get("event") == kind and m.get("session") == session for m in self.sent):
                return True
            time.sleep(0.02)
        return False

    def sid(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                return m["payload"]["id"]
        return None

    def run_turn(self, text="set a marker in app.py", mode="build"):
        self.backend.dispatch({"op": "open", "session": "s", "root": str(self.root),
                               "req_id": time.time_ns()})
        self.assertTrue(self.done("s", kind="session"))
        self.sent.clear()
        self.backend.dispatch({"op": "submit", "session": "s", "text": text,
                               "mode": mode, "req_id": time.time_ns()})
        self.assertTrue(self.done("s"))
        time.sleep(0.2)
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def test_the_whole_workflow_over_the_wire(self):
        agents.save({"plan": {"enabled": True, "model_id": "mdl-plan"},
                     "build": {"enabled": False},  # build falls back to the session model
                     "oversight": {"enabled": True, "model_id": "mdl-rev",
                                   "intensity": "medium", "max_rounds": 2}})
        self.run_turn(mode="plan-build")

        # Only workflow requests, not the background title request (coder-model).
        flow = [r for r in mock_state(self.server).requests
                if not any("Title this coding task" in str(m.get("content") or "")
                           for m in r["messages"])]
        models = [r["model"] for r in flow]

        # Order: the plan runs first, then the coder proposes, the reviewer
        # checks that decision, and the coder continues after approval.
        self.assertEqual(models[:4], ["plan-model", "coder-model", "reviewer-model", "coder-model"],
                         f"routing order was {models}")

        reviewer_req = next(r for r in flow if r["model"] == "reviewer-model")
        self.assertFalse(reviewer_req["has_tools"], "the reviewer must hold no tools")

        # The plan reached the coder: the coder's post-review request carries the
        # <plan> handoff as structured context.
        coder_post = flow[3]
        self.assertTrue(any("<plan>" in str(m.get("content") or "") for m in coder_post["messages"]),
                        "the plan is handed to the build agent as context")

        # The approved edit really landed.
        self.assertEqual(self.edited.read_text(encoding="utf-8"), "# marker\n")

        # A reproducible, checkable artifact of the run.
        report = {
            "scenario": "plan -> build -> oversight-approve -> build",
            "model_request_order": models,
            "reviewer_is_tool_less": not reviewer_req["has_tools"],
            "plan_reached_build": any("<plan>" in str(m.get("content") or "")
                                      for m in coder_post["messages"]),
            "approved_edit_landed": self.edited.read_text(encoding="utf-8").strip() == "# marker",
            "transcript_has_plan": None,
        }
        sid = self.sid()
        transcript = (paths_mod.session_dir() / f"{sid}.jsonl").read_text(encoding="utf-8")
        report["transcript_has_plan"] = "<plan>" in transcript

        artifact = Path(self.tmp.name) / "e2e-workflow-report.json"
        artifact.write_text(json.dumps(report, indent=2), encoding="utf-8")
        reread = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(reread["model_request_order"][:4],
                         ["plan-model", "coder-model", "reviewer-model", "coder-model"])
        self.assertTrue(reread["reviewer_is_tool_less"])
        self.assertTrue(reread["plan_reached_build"])
        self.assertTrue(reread["approved_edit_landed"])
        self.assertTrue(reread["transcript_has_plan"])
        print("\n[e2e artifact]", json.dumps(report, indent=2))

    def test_oversight_revise_holds_the_edit_then_a_clarified_pass_writes(self):
        # A higher-complexity scenario: the reviewer first refuses the coder's
        # edit (revise), the coder adapts, and the reviewer approves. The flawed
        # write never lands; the adapted one does. All three models are real.
        self.edited.write_text("# placeholder\n", encoding="utf-8")
        mock_state(self.server).scripts["reviewer-model"] = [
            {"kind": "text", "content": '{"verdict":"revise","reasoning":"wipes the file",'
                                        '"correction":"append instead of overwrite"}'},
            {"kind": "text", "content": '{"verdict":"approve","reasoning":"append is safe"}'},
        ]
        # coder first proposes an overwrite, then adapts to a str_replace.
        mock_state(self.server).scripts["coder-model"] = [
            {"kind": "tool", "name": "write_file",
             "arguments": {"path": str(self.edited), "content": "OVERWRITTEN"}},
            {"kind": "tool", "name": "str_replace",
             "arguments": {"path": str(self.edited), "old_str": "# placeholder",
                           "new_str": "# placeholder\n# marker"}},
            {"kind": "text", "content": "Adapted and finished."},
        ]
        mock_state(self.server).cursors = {k: 0 for k in mock_state(self.server).scripts}
        agents.save({"oversight": {"enabled": True, "model_id": "mdl-rev",
                                   "intensity": "medium", "max_rounds": 2}})
        self.run_turn()

        flow = [r for r in mock_state(self.server).requests
                if not any("Title this coding task" in str(m.get("content") or "")
                           for m in r["messages"])]
        models = [r["model"] for r in flow]
        # coder(1) revise, reviewer(1), coder(2) approve, reviewer(2), coder(3)
        self.assertEqual(models, ["coder-model", "reviewer-model", "coder-model",
                                  "reviewer-model", "coder-model"],
                         f"got {models}")
        self.assertIn("placeholder", self.edited.read_text(encoding="utf-8"),
                      "the adapted edit preserved the placeholder")
        self.assertEqual(self.edited.read_text(encoding="utf-8"), "# placeholder\n# marker\n",
                         "the corrected edit is what landed")
        self.assertNotIn("OVERWRITTEN", self.edited.read_text(encoding="utf-8"),
                          "the action the reviewer refused never ran")


if __name__ == "__main__":
    unittest.main(verbosity=2)
