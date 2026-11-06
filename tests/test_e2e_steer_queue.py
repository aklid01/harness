"""End-to-end: steer/queue mid-turn messaging and thinking capture.

This test does not patch call_llm. It runs the real Harness backend, the real
streaming model call, and real session persistence against a scripted
in-process HTTP endpoint, so it proves on the wire what unit tests cannot:

* a steer filed while a turn runs joins the conversation before the next
  model request, and never starts a second turn;
* a queue item waits for the turn to end well, then auto-runs as the next
  turn, with one busy spell and a single done event;
* a provider error discards the queue items still pending (named in the
  error payload) while steers survive;
* pending messages can be edited and removed before they are sent;
* a manual stop keeps everything pending and auto-runs nothing;
* reasoning deltas stream out as thinking events, land on the transcript's
  assistant messages, and never go back to the provider.

Writes tests/e2e-steer-queue-report.json so the run is checkable after the
fact, the same way the renderer check writes its own artifact.
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

from tests.mock_provider import _payload, _stream

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import agent_roles, events, providers, server
from harness import paths as paths_mod

TITLE_MARKER = "Title this coding task"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class ScriptState:
    """Per-model scripted steps, consumed in order, then clamped to the last."""

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


class SteerHandler(BaseHTTPRequestHandler):
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
                    "messages": body.get("messages") or [],
                }
            )

        delay = float(step.get("delay") or 0)
        if delay:
            time.sleep(delay)
        if step.get("kind") == "error":
            payload = {
                "error": {"message": "mock upstream failure", "type": "server_error"}
            }
            data = json.dumps(payload).encode()
            self.send_response(int(step.get("status") or 500))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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


class SteerQueueE2E(unittest.TestCase):
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

        self.target = self.root / "notes.md"
        self.target.write_text("hello\n", encoding="utf-8")

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.state = None
        self.report = {}

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def boot(self, scripts):
        if isinstance(scripts, list):
            scripts = {"sq-model": scripts}
        state = ScriptState(scripts)
        self.state = state
        handler = type("Bound", (SteerHandler,), {"state": state})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        providers.save_group({"id": "grp", "name": "One", "base_url": base})
        providers.save_model({"id": "mdl-sq", "name": "SQ", "model": "sq-model"}, "grp")
        providers.set_active("mdl-sq")
        providers.set_key("grp", "sk-steer-queue-test")

    def wait_until(self, cond, timeout=15):
        limit = time.time() + timeout
        while time.time() < limit:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def count(self, kind, session="s"):
        return sum(
            1
            for m in self.sent
            if m.get("event") == kind and m.get("session") == session
        )

    def done(self, session="s", timeout=30):
        return self.wait_until(lambda: self.count("done", session) >= 1, timeout)

    def replies(self, op):
        return [m for m in self.sent if m.get("reply") == op]

    def pending_events(self):
        return [m for m in self.sent if m.get("event") == events.PENDING]

    def last_pending(self):
        evs = self.pending_events()
        return evs[-1]["payload"]["items"] if evs else None

    def flow_requests(self):
        """Model requests of the scenario, without the background title call."""
        return [
            r
            for r in self.state.requests
            if not any(
                TITLE_MARKER in str(m.get("content") or "") for m in r["messages"]
            )
        ]

    def transcript_messages(self):
        sid = None
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                sid = m["payload"]["id"]

        messages = []
        path = paths_mod.session_dir() / f"{sid}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "rewind_to" in entry:
                del messages[entry["rewind_to"] :]
            elif "compacted" in entry:
                messages = list(entry["compacted"])
            else:
                messages.append(entry)
        return messages

    def dispatch(self, obj):
        obj.setdefault("session", "s")
        obj.setdefault("root", str(self.root))
        obj["req_id"] = time.time_ns()
        self.backend.dispatch(obj)

    def open_and_submit(self, text, mode="build"):
        self.dispatch({"op": "open", "mode": mode})
        self.assertTrue(
            self.wait_until(
                lambda: any(
                    m.get("event") == events.SESSION and m.get("session") == "s"
                    for m in self.sent
                )
            )
        )
        self.sent.clear()
        self.dispatch({"op": "submit", "text": text, "mode": mode})

    def test_steer_joins_the_running_turn(self):
        self.boot(
            [
                {
                    "kind": "tool",
                    "name": "read_file",
                    "arguments": {"path": str(self.target)},
                    "delay": 1.5,
                },
                {"kind": "text", "content": "steered work complete"},
            ]
        )
        self.open_and_submit("read the notes")
        self.assertTrue(self.wait_until(lambda: self.count(events.STATUS) >= 1))
        self.dispatch({"op": "enqueue", "text": "use library Y", "kind": "steer"})
        self.assertTrue(self.done())

        ok = [m for m in self.replies("enqueue")]
        self.assertTrue(
            ok and ok[-1].get("ok"),
            f"enqueue accepted while running; sent={json.dumps(self.sent)[:2000]}",
        )

        flow = self.flow_requests()
        self.assertEqual(len(flow), 2, "one turn, two requests")
        self.assertFalse(
            any(m.get("content") == "use library Y" for m in flow[0]["messages"]),
            "the first request went out before the steer existed",
        )

        self.assertEqual(flow[1]["messages"][-2]["content"], "use library Y")

        via = [
            m
            for m in self.sent
            if m.get("event") == events.MESSAGE and m["payload"].get("via") == "steer"
        ]
        self.assertTrue(via and via[-1]["payload"]["content"] == "use library Y")
        self.assertEqual(self.count("done"), 1, "a steer never starts a second turn")
        self.assertEqual(self.last_pending(), [], "the steer drained")

        transcript = self.transcript_messages()
        self.assertTrue(
            any(
                m.get("role") == "user" and m.get("content") == "use library Y"
                for m in transcript
            ),
            "the steer is part of the saved conversation",
        )
        self.report["steer_joins_running_turn"] = True

    def test_queue_auto_runs_after_the_turn(self):
        self.boot(
            [
                {"kind": "text", "content": "first answer", "delay": 1.2},
                {"kind": "text", "content": "second answer"},
            ]
        )
        self.open_and_submit("start the work")
        self.assertTrue(self.wait_until(lambda: self.count(events.STATUS) >= 1))
        self.dispatch({"op": "enqueue", "text": "now the follow-up", "kind": "queue"})
        self.assertTrue(self.done())

        flow = self.flow_requests()
        self.assertEqual(len(flow), 2, "the queue item ran as its own turn")
        self.assertFalse(
            any(m.get("content") == "now the follow-up" for m in flow[0]["messages"]),
            "the queue item waited for the turn to end",
        )
        self.assertEqual(flow[1]["messages"][-2]["content"], "now the follow-up")
        via = [
            m
            for m in self.sent
            if m.get("event") == events.MESSAGE and m["payload"].get("via") == "queue"
        ]
        self.assertTrue(via and via[-1]["payload"]["content"] == "now the follow-up")
        self.assertEqual(
            self.count("done"), 1, "the chain is one busy spell with one done"
        )
        self.assertEqual(self.last_pending(), [])
        self.report["queue_auto_runs_after_success"] = True

    def test_provider_error_discards_queues_and_steers_reach_the_next_turn(self):
        self.boot(
            [
                {"kind": "text", "content": "part one", "delay": 1.2},
                {"kind": "error", "status": 401},
            ]
        )
        self.open_and_submit("begin")
        self.assertTrue(self.wait_until(lambda: self.count(events.STATUS) >= 1))
        self.dispatch({"op": "enqueue", "text": "queued one", "kind": "queue"})
        self.dispatch({"op": "enqueue", "text": "steer note", "kind": "steer"})
        self.dispatch({"op": "enqueue", "text": "queued two", "kind": "queue"})
        self.assertTrue(self.done())

        errors = [m for m in self.sent if m.get("event") == events.ERROR]
        self.assertTrue(errors, "the provider failure surfaced")
        discarded = errors[-1]["payload"].get("discarded_queue") or []
        self.assertEqual(
            discarded,
            ["queued two"],
            "queues behind the failed turn are discarded and named",
        )
        self.assertFalse(
            any(
                m.get("content") == "queued two"
                for r in self.flow_requests()
                for m in r["messages"]
            ),
            "the discarded queue text is never sent into a request",
        )

        transcript = self.transcript_messages()
        self.assertTrue(
            any(
                m.get("role") == "user" and m.get("content") == "queued one"
                for m in transcript
            )
        )

        flow = self.flow_requests()
        self.assertTrue(
            any(
                m.get("role") == "user" and m.get("content") == "steer note"
                for m in flow[1]["messages"]
            ),
            "the pending steer reached the model on the chained turn",
        )
        self.assertEqual(self.last_pending(), [])
        self.report["error_discards_queues_steers_reach_next_turn"] = True

    def test_steer_survives_an_error_on_the_first_request(self):
        self.boot(
            [
                {"kind": "error", "status": 401, "delay": 1.0},
            ]
        )
        self.open_and_submit("start")
        self.assertTrue(
            self.wait_until(lambda: len(self.state.requests) >= 1),
            "the first model request reached the endpoint",
        )
        self.dispatch({"op": "enqueue", "text": "steer survives", "kind": "steer"})
        self.assertTrue(self.done())

        items = self.last_pending()
        self.assertEqual(
            [i["text"] for i in items],
            ["steer survives"],
            "steers survive a provider error",
        )
        self.assertFalse(
            any(
                m.get("content") == "steer survives"
                for r in self.flow_requests()
                for m in r["messages"]
            ),
            "the surviving steer was not sent",
        )
        self.report["steer_survives_provider_error"] = True

    def test_pending_messages_can_be_edited_and_removed(self):
        self.boot(
            [
                {"kind": "text", "content": "working on it", "delay": 1.5},
                {"kind": "text", "content": "done"},
            ]
        )
        self.open_and_submit("do the thing")
        self.assertTrue(self.wait_until(lambda: self.count(events.STATUS) >= 1))
        self.dispatch({"op": "enqueue", "text": "original steer text", "kind": "steer"})
        self.dispatch({"op": "enqueue", "text": "queued to remove", "kind": "queue"})
        self.assertTrue(
            self.wait_until(lambda: len(self.last_pending() or []) == 2),
            "both pending items reported",
        )
        items = {i["text"]: i["id"] for i in self.last_pending()}
        self.dispatch(
            {
                "op": "pending_edit",
                "id": items["original steer text"],
                "text": "edited steer text",
            }
        )
        self.dispatch({"op": "pending_remove", "id": items["queued to remove"]})
        self.assertTrue(
            self.wait_until(
                lambda: (
                    [i["text"] for i in (self.last_pending() or [])]
                    == ["edited steer text"]
                )
            )
        )
        self.assertTrue(self.done())

        flow = self.flow_requests()
        self.assertEqual(len(flow), 2)
        self.assertEqual(flow[1]["messages"][-2]["content"], "edited steer text")
        for sent_text in ("original steer text", "queued to remove"):
            self.assertFalse(
                any(m.get("content") == sent_text for r in flow for m in r["messages"]),
                f'"{sent_text}" was replaced or removed before sending',
            )
        self.report["pending_edit_and_remove"] = True

    def test_stop_keeps_everything_pending(self):
        self.boot(
            [
                {"kind": "text", "content": "slow first answer", "delay": 1.2},
                {"kind": "text", "content": "never sent"},
            ]
        )
        self.open_and_submit("long task")

        self.assertTrue(
            self.wait_until(lambda: len(self.state.requests) >= 1),
            "the first model request reached the endpoint",
        )
        self.dispatch({"op": "enqueue", "text": "survives stop", "kind": "queue"})
        self.dispatch({"op": "stop"})
        self.assertTrue(self.done())

        self.assertEqual(
            len(self.flow_requests()),
            1,
            "nothing auto-ran after a stop",
        )
        items = self.last_pending()
        self.assertEqual(
            [i["text"] for i in items],
            ["survives stop"],
            "a manual stop keeps pending messages",
        )
        notes = [
            m
            for m in self.sent
            if m.get("event") == events.NOTE and m["payload"].get("text") == "stopped"
        ]
        self.assertTrue(notes, "the stop was reported as a stop, not an error")
        self.assertFalse(
            any(m.get("event") == events.ERROR for m in self.sent),
            "a manual stop is not a provider error",
        )
        self.report["stop_keeps_pending"] = True

    def test_thinking_streams_persists_and_never_goes_back(self):
        self.boot(
            [
                {
                    "kind": "tool",
                    "name": "read_file",
                    "arguments": {"path": str(self.target)},
                    "reasoning": "I should look at the file first",
                    "delay": 0.3,
                },
                {
                    "kind": "text",
                    "content": "here is what it says",
                    "reasoning": "now I can answer",
                },
            ]
        )
        self.open_and_submit("what do the notes say")
        self.assertTrue(self.done())

        thoughts = "".join(
            m["payload"].get("text", "")
            for m in self.sent
            if m.get("event") == events.THINKING
        )
        self.assertIn("I should look at the file first", thoughts)
        self.assertIn("now I can answer", thoughts)

        transcript = self.transcript_messages()
        reasoning_blocks = {
            m.get("content"): m.get("reasoning")
            for m in transcript
            if m.get("role") == "assistant" and m.get("reasoning")
        }
        self.assertEqual(
            reasoning_blocks.get("here is what it says"),
            "now I can answer",
            "reasoning persists on the assistant message",
        )

        flow = self.flow_requests()
        self.assertEqual(len(flow), 2)
        for r in flow:
            for m in r["messages"]:
                self.assertNotIn(
                    "reasoning",
                    m,
                    "reasoning is display-only and never sent to the provider",
                )
        self.report["thinking_captured_and_stripped"] = True

    def tearDown(self):
        artifact = Path(__file__).resolve().parent / "e2e-steer-queue-report.json"
        existing = {}
        if artifact.exists():
            try:
                existing = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        existing.update(self.report)
        artifact.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print("\n[e2e artifact]", artifact)


if __name__ == "__main__":
    unittest.main()
