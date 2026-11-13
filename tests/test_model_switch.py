"""Switching models and retrying a failed request, end to end.

A failed request keeps its user message in one piece, a model switch does not
disturb the session, and nothing writes to a model the user has left.
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


from harness import events, providers, server
from harness.errors import ModelError


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def message(content):
    return SimpleNamespace(
        content=content,
        tool_calls=[],
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": content},
    )


USAGE = {
    "prompt_tokens": 1,
    "completion_tokens": 1,
    "reasoning_tokens": None,
    "cached_tokens": None,
}


class SwitchCase(unittest.TestCase):
    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "proj"
        appdata = Path(self.tmp.name) / "appdata"
        root.mkdir(parents=True, exist_ok=True)
        server.paths_mod.init(project_root=root, app_data_dir=appdata)
        self.addCleanup(server.paths_mod.reset)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)
        providers.save_group(
            {"id": "grp-1", "name": "NVIDIA", "base_url": "https://nvidia.example/v1"}
        )
        self.a = providers.save_model(
            {"id": "mdl-a", "name": "GLM 5.3 Flash", "model": "glm-5.3-flash"}, "grp-1"
        )
        self.b = providers.save_model(
            {
                "id": "mdl-b",
                "name": "DeepSeek V4.1 Flash",
                "model": "deepseek-v4.1-flash",
            },
            "grp-1",
        )
        providers.set_active("mdl-a")
        providers.set_key("grp-1", "sk-shared")
        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)

        self.sid = None

    def dispatch(self, **obj):
        return self.backend.dispatch(obj)

    def done(self, timeout=20, kind="done"):
        limit = time.time() + timeout
        while time.time() < limit:
            if any(m.get("event") == kind for m in self.sent):
                return True
            time.sleep(0.02)
        return False

    def clear_sent(self):
        self.sent.clear()

    def events_of(self, kind):
        return [m["payload"] for m in self.sent if m.get("event") == kind]

    def submit(self, text):
        self.clear_sent()
        self.dispatch(op="submit", session="s", text=text, mode="build")
        self.assertTrue(self.done())

    def note_session(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                self.sid = m["payload"]["id"]

    def user_messages(self):
        return [m for m in self.session_messages() if m.get("role") == "user"]

    def session_messages(self):
        self.assertTrue(self.sid, "no session was opened yet")
        path = server.paths_mod.session_dir() / f"{self.sid}.jsonl"
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if "rewind_to" in entry:
                del messages[entry["rewind_to"] :]
            elif "compacted" not in entry:
                messages.append(entry)
        return messages

    def test_failed_request_then_retry_on_another_model(self):
        seen = []

        def fake_call(
            messages, tools=None, on_delta=None, on_reasoning=None, cancel=None
        ):
            model = providers.active_config()[2]
            seen.append(model)
            if model == "glm-5.3-flash":
                raise ModelError(
                    "network", "could not reach the provider", model="GLM 5.3 Flash"
                )
            return message("done by deepseek"), USAGE

        with mock.patch("harness.agent.call_llm", side_effect=fake_call):
            self.dispatch(
                op="open", session="s", root=str(server.paths_mod.project_root())
            )
            self.assertTrue(self.done(kind="session"))
            self.note_session()
            self.submit("fix the bug")

            self.note_session()

        failures = self.events_of(events.ERROR)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "network")
        self.assertIn("GLM 5.3 Flash", failures[0]["text"])
        self.assertEqual(failures[0]["retry"]["text"], "fix the bug")

        self.dispatch(op="set_active", session="s", id="mdl-b")
        self.clear_sent()
        with mock.patch("harness.agent.call_llm", side_effect=fake_call):
            self.dispatch(
                op="retry",
                session="s",
                count=failures[0]["retry"]["count"],
                text="fix the bug",
                mode="build",
            )
            self.assertTrue(self.done())

        bodies = self.user_messages()
        self.assertEqual(len(bodies), 1, "the failed message must not be duplicated")
        self.assertEqual(bodies[0]["content"], "fix the bug")
        answers = [m for m in self.session_messages() if m.get("role") == "assistant"]
        self.assertEqual([a["content"] for a in answers], ["done by deepseek"])
        self.assertEqual(seen, ["glm-5.3-flash", "deepseek-v4.1-flash"])
        self.assertEqual(providers.get_active_model_id(), "mdl-b")

    def test_switching_model_mid_generation_stops_it(self):
        release = threading.Event()
        started = threading.Event()

        def slow(messages, tools=None, on_delta=None, on_reasoning=None, cancel=None):
            started.set()
            release.wait(5)
            return message("late answer"), USAGE

        with mock.patch("harness.agent.call_llm", side_effect=slow):
            self.dispatch(
                op="open", session="s", root=str(server.paths_mod.project_root())
            )
            self.assertTrue(self.done(kind="session"))
            self.note_session()
            self.clear_sent()
            self.dispatch(op="submit", session="s", text="long task", mode="build")
            self.assertTrue(started.wait(5))

            self.dispatch(op="set_active", session="s", id="mdl-b")
            release.set()
            self.assertTrue(self.done())
            self.note_session()

        assistant = [m for m in self.session_messages() if m.get("role") == "assistant"]
        self.assertEqual(assistant, [], "an abandoned generation is not appended")
        self.assertFalse(
            self.backend.sessions["s"].running, "the session must not stay stuck busy"
        )

        self.clear_sent()
        with mock.patch(
            "harness.agent.call_llm", return_value=(message("fresh"), USAGE)
        ) as call:
            self.dispatch(op="submit", session="s", text="again", mode="build")
            self.assertTrue(self.done())
            self.assertEqual(call.call_count, 1)
        self.assertEqual(
            [m for m in self.session_messages() if m.get("role") == "assistant"][0][
                "content"
            ],
            "fresh",
        )

    def test_two_models_of_one_provider_stay_selectable(self):
        models = providers.list_models("grp-1")
        self.assertEqual([m["id"] for m in models], ["mdl-a", "mdl-b"])
        self.assertTrue(providers.set_active("mdl-a"))
        self.assertEqual(providers.active_config()[2], "glm-5.3-flash")
        self.assertTrue(providers.set_active("mdl-b"))
        self.assertEqual(providers.active_config()[2], "deepseek-v4.1-flash")

    def test_retry_is_refused_while_busy(self):
        self.dispatch(op="open", session="s", root=str(server.paths_mod.project_root()))
        self.assertTrue(self.done(kind="session"))
        self.note_session()
        self.backend.sessions["s"].running = True
        self.clear_sent()
        self.dispatch(op="retry", session="s", count=0, text="hi", mode="build")
        replies = [m for m in self.sent if m.get("reply") == "retry"]
        self.assertEqual(len(replies), 1)
        self.assertFalse(replies[0]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
