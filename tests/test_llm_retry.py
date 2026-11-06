"""The model call: retries transient failures, never retries a broken stream,
and never sends a smaller model more than it can hold."""

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

from harness import errors, history, llm, paths, providers, retry


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def canned(content="ok", usage=None):
    message = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=usage
        or SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=None),
            prompt_tokens_details=SimpleNamespace(cached_tokens=None),
        ),
    )


class FakeCompletions:
    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            return canned()
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class LlmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "app",
        )
        self.addCleanup(paths.reset)
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()

        mock.patch.object(
            retry, "RETRY_SCHEDULE", (0.01,) * len(retry.RETRY_SCHEDULE)
        ).start()
        self.addCleanup(mock.patch.stopall)

    def use(self, **extra):
        providers.save_provider(
            {"id": "p", "name": "P", "base_url": "https://x/v1", "model": "m", **extra}
        )
        providers.set_active("p")
        return self

    def client(self, script=None):
        self.completions = FakeCompletions(script)
        client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        mock.patch.object(llm, "get_client", return_value=client).start()
        return client

    def test_transient_failure_is_retried(self):
        self.use()
        self.client([RuntimeError("connection reset by peer"), canned("recovered")])
        message, _ = llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual(message.content, "recovered")
        self.assertEqual(len(self.completions.calls), 2)

    def test_permanent_failure_is_not_retried(self):
        self.use()
        failure = errors.classify(_status(401, "Incorrect API key provided"))
        self.client([failure, canned()])
        with self.assertRaises(errors.ModelError) as raised:
            llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual(len(self.completions.calls), 1)
        self.assertEqual(raised.exception.kind, "auth")

    def test_tools_are_omitted_for_a_model_without_them(self):
        self.use(supports_tools=False)
        self.client()
        llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertNotIn("tools", self.completions.calls[0])

    def test_stream_break_after_text_is_not_retried(self):
        self.use()
        chunks = 0

        class Broken:
            def __iter__(self):
                return self

            def __next__(self):
                nonlocal chunks
                chunks += 1
                if chunks == 1:
                    return SimpleNamespace(
                        usage=None,
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content="partial", tool_calls=None
                                )
                            )
                        ],
                    )
                raise ConnectionResetError("connection reset by peer")

            def close(self):
                pass

        completions = FakeCompletions([Broken(), canned("should not happen")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        mock.patch.object(llm, "get_client", return_value=client).start()

        seen = []
        with self.assertRaises(errors.ModelError) as raised:
            llm.call_llm([{"role": "user", "content": "hi"}], on_delta=seen.append)
        self.assertEqual(seen, ["partial"])
        self.assertEqual(raised.exception.kind, "stream_interrupted")
        self.assertFalse(raised.exception.transient)
        self.assertEqual(len(completions.calls), 1, "no second request is sent")

    def test_stream_failure_before_text_is_retried(self):
        self.use()
        state = {"n": 0}

        class Flaky:
            def close(self):
                pass

            def __iter__(self):
                return self

            def __next__(self):
                state["n"] += 1
                if state["n"] == 1:
                    raise ConnectionResetError("connection reset by peer")
                raise StopIteration

        good = [
            SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="fine", tool_calls=None)
                    )
                ],
            ),
            SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=2,
                    completion_tokens=2,
                    completion_tokens_details=None,
                    prompt_tokens_details=None,
                ),
                choices=[],
            ),
        ]

        class Steady:
            def __init__(self):
                self.items = list(good)

            def close(self):
                pass

            def __iter__(self):
                return self

            def __next__(self):
                if not self.items:
                    raise StopIteration
                return self.items.pop(0)

        completions = FakeCompletions([Flaky(), Steady()])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        mock.patch.object(llm, "get_client", return_value=client).start()

        seen = []
        message, _ = llm.call_llm(
            [{"role": "user", "content": "hi"}], on_delta=seen.append
        )
        self.assertEqual(seen, ["fine"])
        self.assertEqual(message.content, "fine")
        self.assertEqual(len(completions.calls), 2)

    def test_cancel_stops_a_stream_mid_flight(self):
        self.use()
        cancel = threading.Event()
        release = threading.Event()
        served = {"chunks": 0}

        class Hanging:
            def close(self):
                release.set()

            def __iter__(self):
                return self

            def __next__(self):
                if not served["chunks"]:
                    served["chunks"] = 1
                    return SimpleNamespace(
                        usage=None,
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content="first", tool_calls=None)
                            )
                        ],
                    )
                release.wait(2)
                raise ConnectionResetError("should have been abandoned")

        completions = FakeCompletions([Hanging()])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        mock.patch.object(llm, "get_client", return_value=client).start()
        seen = []

        def worker():
            try:
                llm.call_llm(
                    [{"role": "user", "content": "hi"}],
                    on_delta=seen.append,
                    cancel=cancel,
                )
            except Exception as failure:  # noqa: BLE001
                self.outcome = failure

        thread = threading.Thread(target=worker)
        thread.start()

        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(seen, "the first chunk never arrived")
        cancel.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(seen, ["first"])
        self.assertEqual(getattr(self, "outcome", None).kind, "cancelled")
        self.assertTrue(release.is_set(), "the stream must be closed when abandoned")
        self.assertEqual(len(completions.calls), 1)

    def test_retry_events_reach_the_event_bus(self):
        from harness import events

        self.use()
        seen = []
        events.subscribe(lambda kind, payload: seen.append((kind, payload)))
        self.addCleanup(events.reset)
        self.client([RuntimeError("connection reset by peer"), canned("recovered")])
        llm.call_llm([{"role": "user", "content": "hi"}])
        retries = [payload for kind, payload in seen if kind == events.RETRY]
        self.assertEqual(len(retries), 1)
        self.assertTrue(retries[0]["kind"])
        self.assertEqual(retries[0]["model"], "P")
        self.assertIn("wait", retries[0])


def _status(status, message):
    return errors.classify(_StatusError(status, message))


class _StatusError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(
            status_code=status, headers={}, request=None, url="https://x/v1"
        )
        self.body = {"error": {"message": message}}


class ContextCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "app",
        )
        self.addCleanup(paths.reset)
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)

    def transcript(self, turns=6):
        messages = [{"role": "system", "content": "system prompt"}]
        for index in range(turns):
            messages.append(
                {"role": "user", "content": f"question {index} " + "x" * 400}
            )
            messages.append(
                {"role": "assistant", "content": f"answer {index} " + "y" * 400}
            )
        return messages

    def test_stored_history_is_untouched_when_it_does_not_fit(self):
        messages = self.transcript()
        before = json.dumps(messages)
        request, note = history.prepare(messages, window=500)
        self.assertTrue(note)
        self.assertLess(history.estimate(request), history.estimate(messages))
        self.assertEqual(
            json.dumps(messages), before, "the caller's copy must not change"
        )

    def test_system_and_live_tail_always_survive(self):
        messages = self.transcript()
        request, _ = history.prepare(messages, window=400)
        self.assertEqual(request[0], messages[0])
        self.assertEqual(request[-1], messages[-1])
        self.assertEqual(request[-2], messages[-2])

    def test_a_roomy_window_keeps_everything(self):
        messages = self.transcript()
        request, note = history.prepare(messages, window=1_000_000)
        self.assertEqual(request, messages)
        self.assertEqual(note, "")

    def test_a_smaller_window_trims_more(self):
        messages = self.transcript(12)
        big, _ = history.prepare(messages, window=4000)
        small, _ = history.prepare(messages, window=800)
        self.assertLess(history.estimate(small), history.estimate(big))

    def test_locked_prefix_survives(self):
        messages = [{"role": "system", "content": "system"}]
        messages.append({"role": "user", "content": "<summary>handoff</summary>"})
        for index in range(8):
            messages.append({"role": "user", "content": f"q{index} " + "z" * 300})
            messages.append(
                {"role": "tool", "tool_call_id": f"c{index}", "content": "r" * 300}
            )
        request, note = history.prepare(messages, window=600)
        self.assertEqual(request[1], messages[1])
        self.assertTrue(note)
        self.assertLess(len(request), len(messages))


class RunnerContextCase(unittest.TestCase):
    """Switching to a model with a smaller window trims the request only."""

    def setUp(self):
        from harness import paths

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "proj"
        root.mkdir(parents=True)
        paths.init(project_root=root, app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths.reset)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)
        providers.save_group({"id": "grp", "name": "N", "base_url": "https://x/v1"})
        providers.save_model(
            {"id": "mdl-big", "name": "big", "model": "big", "context_window": 200_000},
            "grp",
        )
        providers.save_model(
            {
                "id": "mdl-small",
                "name": "small",
                "model": "small",
                "context_window": 400,
            },
            "grp",
        )
        providers.set_active("mdl-big")

    def transcript(self):
        messages = [{"role": "system", "content": "system prompt"}]
        for index in range(8):
            messages.append({"role": "user", "content": f"q{index} " + "u" * 200})
            messages.append({"role": "assistant", "content": f"a{index} " + "v" * 200})
            messages.append(
                {"role": "tool", "tool_call_id": f"c{index}", "content": "r" * 200}
            )
        return messages

    def run_turn(self, model_id, text="keep going"):
        from harness import agent, events
        from harness import session as session_mod

        seen = []
        events.subscribe(lambda kind, payload: seen.append((kind, payload)))
        self.addCleanup(events.reset)
        session_mod.blank(session_mod.fresh_id())
        runner = agent.Runner(stream=True, cancel_event=None, mode="build")
        runner.messages = self.transcript()
        count = len(runner.messages)
        providers.set_active(model_id)
        with mock.patch(
            "harness.llm.call_llm",
            return_value=(
                SimpleNamespace(
                    content="ok",
                    tool_calls=[],
                    model_dump=lambda exclude_none=True: {
                        "role": "assistant",
                        "content": "ok",
                    },
                ),
                {},
            ),
        ):
            runner.handle_input(text)
        return runner, count, seen

    def test_big_model_sends_the_whole_history(self):
        runner, count, _ = self.run_turn("mdl-big")
        self.assertEqual(len(runner.messages), count + 1)
        self.assertNotIn("left out of this request", json.dumps(runner.messages))

    def test_small_model_gets_a_trimmed_request_and_history_survives(self):
        runner, count, seen = self.run_turn("mdl-small")
        notes = [payload["text"] for kind, payload in seen if kind == "note"]
        self.assertTrue(any("trimmed the request" in n for n in notes), notes)

        self.assertNotIn("left out of this request", json.dumps(runner.messages))
        self.assertEqual(len(runner.messages), count + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
