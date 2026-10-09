"""Retries: bounded by one monotonic deadline, transient only, cancellable,
and never leaking the key."""

import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

import openai  # noqa: E402

from harness import errors, retry  # noqa: E402


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.request = mock.Mock(url="https://provider.example/v1")
        self.url = "https://provider.example/v1/chat/completions"


_STATUS_NAMES = {
    429: "RateLimitError", 500: "InternalServerError", 503: "InternalServerError",
    401: "AuthenticationError", 403: "PermissionDeniedError", 404: "NotFoundError",
    400: "BadRequestError", 422: "UnprocessableEntityError", 408: "APIStatusError",
    504: "APIStatusError", 502: "InternalServerError",
}


def http(status, message="", headers=None, body=None):
    """A real SDK status error, named the way the SDK would name it."""
    cls = type(_STATUS_NAMES.get(status, "APIStatusError"), (openai.APIStatusError,), {})
    return cls(message, response=FakeResponse(status, headers),
              body=body if body is not None else {"error": {"message": message or name_of(status)}})


def name_of(status):
    return _STATUS_NAMES.get(status, "APIStatusError")


class ClassifyCase(unittest.TestCase):
    def test_transient_failures(self):
        transient = [
            http(429, "slow down"),
            http(500, "boom"),
            http(502, "bad gateway"),
            http(503, "unavailable"),
            http(504, "gateway time"),
            http(408, "request timeout"),
            openai.APITimeoutError(request=mock.Mock(url="https://x/v1")),
            openai.APIConnectionError(request=mock.Mock(url="https://x/v1")),
            ConnectionResetError("connection reset by peer"),
            ValueError("Expecting value: line 1 column 1 (char 0)"),
        ]
        for failure in transient:
            error = errors.classify(failure, model="m")
            self.assertTrue(error.transient, f"{failure!r} should be transient")

    def test_permanent_failures(self):
        permanent = [
            http(401, "Incorrect API key provided"),
            http(403, "forbidden"),
            http(404, "model does not exist"),
            http(400, "Invalid model id"),
            http(422, "Unsupported parameter: 'reasoning_effort'"),
        ]
        for failure in permanent:
            error = errors.classify(failure, model="m")
            self.assertFalse(error.transient, f"{failure!r} should be permanent")

    def test_kinds(self):
        cases = {
            http(401, "Incorrect API key provided"): "auth",
            http(404, "no such model"): "not_found",
            http(429, "rate limited"): "rate_limited",
            http(503, "overloaded"): "server",
            openai.APITimeoutError(request=mock.Mock(url="https://x/v1")): "timeout",
            openai.APIConnectionError(request=mock.Mock(url="https://x/v1")): "network",
            ConnectionResetError("reset"): "network",
        }
        for failure, kind in cases.items():
            self.assertEqual(errors.classify(failure).kind, kind)

    def test_retry_after_is_read(self):
        error = errors.classify(http(429, "slow down", headers={"retry-after": "7"}))
        self.assertEqual(error.retry_after, 7.0)

    def test_messages_never_carry_the_key(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        error = errors.classify(http(401, f"Incorrect API key provided: {secret}", body=None),
                                api_key=secret)
        self.assertNotIn(secret, error.text())
        self.assertNotIn(secret, str(error))

    def test_model_is_named_in_the_message(self):
        error = errors.classify(http(404, "model not found"), model="DeepSeek V4.1 Flash")
        self.assertIn("DeepSeek V4.1 Flash", error.text())

    def test_provider_detail_survives(self):
        error = errors.classify(http(422, "Unsupported parameter: 'reasoning_effort'"), model="m")
        self.assertIn("reasoning_effort", error.text())


class RunCase(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def request(self, failures, result="ok"):
        state = {"n": 0}

        def call():
            state["n"] += 1
            self.calls.append(time.monotonic())
            if state["n"] <= failures:
                raise http(503, "overloaded")
            return result

        return call

    def test_transient_then_success(self):
        started = time.monotonic()
        value = retry.run(self.request(2), window=30, host="h")
        self.assertEqual(value, "ok")
        self.assertEqual(len(self.calls), 3)
        self.assertLess(time.monotonic() - started, 10)

    def test_permanent_fails_immediately(self):
        def call():
            self.calls.append(time.monotonic())
            raise http(401, "Incorrect API key provided")

        started = time.monotonic()
        with self.assertRaises(errors.ModelError) as raised:
            retry.run(call, window=30, host="h")
        self.assertEqual(len(self.calls), 1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(raised.exception.kind, "auth")

    def test_repeated_failures_stop_at_the_deadline(self):
        def call():
            self.calls.append(time.monotonic())
            raise openai.APITimeoutError(request=mock.Mock(url="https://x/v1"))

        started = time.monotonic()
        with self.assertRaises(errors.ModelError) as raised:
            retry.run(call, window=2.0, host="h")
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 3.0)
        self.assertTrue(raised.exception.exhausted)
        self.assertTrue(raised.exception.kind == "timeout")
        self.assertGreater(len(self.calls), 2, "the window should allow several attempts")

    def test_retry_after_is_respected(self):
        def call():
            self.calls.append(time.monotonic())
            if len(self.calls) == 1:
                raise http(429, "slow down", headers={"retry-after": "0.5"})
            return "ok"

        started = time.monotonic()
        self.assertEqual(retry.run(call, window=10, host="h"), "ok")
        gap = self.calls[1] - self.calls[0]
        self.assertGreaterEqual(gap, 0.45)
        self.assertLess(gap, 1.5)

    def test_retry_after_that_does_not_fit_fails_fast(self):
        def call():
            self.calls.append(time.monotonic())
            raise http(429, "slow down", headers={"retry-after": "120"})

        started = time.monotonic()
        with self.assertRaises(errors.ModelError) as raised:
            retry.run(call, window=1.0, host="h")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(raised.exception.exhausted)
        self.assertEqual(raised.exception.kind, "rate_limited")

    def test_cancel_stops_retrying(self):
        cancel = threading.Event()

        def call():
            self.calls.append(time.monotonic())
            raise http(503, "overloaded")

        def stop_soon():
            time.sleep(0.3)
            cancel.set()

        threading.Thread(target=stop_soon, daemon=True).start()
        with self.assertRaises(errors.ModelError) as raised:
            retry.run(call, window=30, cancel=cancel, host="h")
        self.assertEqual(raised.exception.kind, "cancelled")
        time.sleep(0.4)
        attempts = len(self.calls)
        time.sleep(0.6)
        self.assertEqual(len(self.calls), attempts, "no attempt runs after a cancel")

    def test_progress_events_report_wait_and_model(self):
        seen = []
        retry.run(self.request(1), window=10, host="h", model="DeepSeek V4.1 Flash",
                  on_attempt=lambda state: seen.append(state))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["model"], "DeepSeek V4.1 Flash")
        self.assertGreater(seen[0]["attempt"], 1)
        self.assertGreaterEqual(seen[0]["wait"], 0.0)
        self.assertEqual(seen[0]["window"], 10)

    def test_concurrent_retries_share_one_slot_per_host(self):
        gate = retry._gate("shared")
        self.assertIs(gate, retry._gate("shared"))
        self.assertIsNot(gate, retry._gate("other"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
