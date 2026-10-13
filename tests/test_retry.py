"""Retries: a fixed 3/5/15/20/30s schedule, bounded by an attempt count and
one shared deadline, transient only, cancellable, and never leaking the key."""

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

    def test_each_status_suggests_its_own_next_step(self):
        cases = {
            http(401, "Incorrect API key provided"): "key",
            http(403, "forbidden"): "permissions",
            http(404, "model does not exist"): "model",
            http(429, "slow down"): "Retry",
            http(422, "bad parameter"): "parameter",
            http(503, "overloaded"): "Retry",
        }
        for failure, needle in cases.items():
            error = errors.classify(failure, model="m")
            self.assertIn(needle, error.hint, f"{failure!r}: {error.hint!r}")
            self.assertTrue(error.hint.endswith((".", "?")))

    def test_hint_reaches_the_payload(self):
        payload = errors.classify(http(429, "slow down"), model="m").payload()
        self.assertIn("hint", payload)
        self.assertIn("Retry", payload["hint"])

    def test_provider_error_body_is_parsed_cleanly(self):
        body = {"error": {"message": "Unsupported parameter", "code": "unsupported_parameter",
                          "type": "invalid_request_error", "param": "reasoning_effort"}}
        error = errors.classify(http(422, "", body=body), model="m")
        self.assertIn("Unsupported parameter", error.detail)
        self.assertIn("reasoning_effort", error.detail)
        self.assertIn("unsupported_parameter", error.detail)
        self.assertIn("parameter reasoning_effort", error.detail)


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
        value = retry.run(self.request(2), window=30, host="h",
                          schedule=(0.05, 0.05))
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
            retry.run(call, window=2.0, attempts=10, host="h")
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 3.0)
        self.assertTrue(raised.exception.exhausted)
        self.assertTrue(raised.exception.kind == "timeout")
        # The scheduled 3s wait is squeezed into what is left of the window,
        # so the deadline still allows a retry before it gives up.
        self.assertGreaterEqual(len(self.calls), 2,
                                "the window still allows a retry")

    def test_attempt_count_caps_the_retries(self):
        def call():
            self.calls.append(time.monotonic())
            raise http(503, "overloaded")

        with self.assertRaises(errors.ModelError) as raised:
            retry.run(call, window=60, host="h", schedule=(0.05,) * 5)
        self.assertEqual(len(self.calls), retry.MAX_ATTEMPTS)
        self.assertTrue(raised.exception.exhausted)
        self.assertEqual(raised.exception.retries, retry.MAX_ATTEMPTS - 1)
        self.assertIn(f"still failing after {retry.MAX_ATTEMPTS - 1} retries",
                      raised.exception.text())

    def test_the_fixed_schedule_is_honored(self):
        """The user-visible promise: retry 1 comes 3s later, retry 2 after
        5s, and the card counts them off in that order."""
        def call():
            self.calls.append(time.monotonic())
            if len(self.calls) <= 2:
                raise http(503, "overloaded")
            return "ok"

        self.assertEqual(retry.run(call, window=120, host="h"), "ok")
        first = self.calls[1] - self.calls[0]
        second = self.calls[2] - self.calls[1]
        self.assertGreaterEqual(first, 2.5)
        self.assertLess(first, 4.5)
        self.assertGreaterEqual(second, 4.5)
        self.assertLess(second, 7.5)

    def test_the_budget_outlasts_a_single_attempt_timeout(self):
        """The regression pin: one attempt burning its whole read timeout must
        leave a whole retry behind it, not eat the budget. With the fixed
        schedule that means the window spans every attempt plus every wait,
        and the schedule is the 3/5/15/20/30 the card promises."""
        self.assertEqual(retry.RETRY_SCHEDULE, (3.0, 5.0, 15.0, 20.0, 30.0))
        self.assertEqual(retry.MAX_ATTEMPTS, len(retry.RETRY_SCHEDULE) + 1)
        self.assertGreaterEqual(
            retry.RETRY_WINDOW,
            retry.MAX_ATTEMPTS * retry.READ_TIMEOUT + sum(retry.RETRY_SCHEDULE))

    def test_a_slow_first_attempt_still_gets_its_retry(self):
        """A provider that stalls for longer than the old read timeout, then
        answers, must survive as one retry rather than kill the turn."""
        stalls = {"left": 2}

        def call():
            self.calls.append(time.monotonic())
            if stalls["left"] > 0:
                stalls["left"] -= 1
                raise openai.APITimeoutError(request=mock.Mock(url="https://x/v1"))
            return "answer"

        self.assertEqual(retry.run(call, attempts=3, host="h",
                                   schedule=(0.05, 0.05)), "answer")
        self.assertEqual(len(self.calls), 3)

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
            if len(self.calls) >= 2:
                cancel.set()
            raise http(503, "overloaded")

        with self.assertRaises(errors.ModelError) as raised:
            retry.run(call, window=30, cancel=cancel, host="h",
                      schedule=(0.2, 0.2, 0.2, 0.2, 0.2))
        self.assertEqual(raised.exception.kind, "cancelled")
        attempts = len(self.calls)
        time.sleep(0.8)
        self.assertEqual(len(self.calls), attempts, "no attempt runs after a cancel")

    def test_progress_events_report_wait_and_model(self):
        seen = []
        retry.run(self.request(1), window=10, host="h", model="DeepSeek V4.1 Flash",
                  schedule=(0.1, 0.1),
                  on_attempt=lambda state: seen.append(state))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["model"], "DeepSeek V4.1 Flash")
        self.assertGreater(seen[0]["attempt"], 1)
        self.assertGreaterEqual(seen[0]["wait"], 0.0)
        self.assertEqual(seen[0]["retries_total"], 2)
        self.assertEqual(seen[0]["window"], 10)

    def test_concurrent_retries_share_one_slot_per_host(self):
        gate = retry._gate("shared")
        self.assertIs(gate, retry._gate("shared"))
        self.assertIsNot(gate, retry._gate("other"))

    def test_second_retry_waits_for_the_host_gate(self):
        timings = {}

        def first():
            started = time.monotonic()
            retry._pause("gate-host", 0.4, None, limit=5)
            timings["first"] = time.monotonic() - started

        def second():
            time.sleep(0.05)
            started = time.monotonic()
            retry._pause("gate-host", 0.05, None, limit=5)
            timings["second"] = time.monotonic() - started

        threads = [threading.Thread(target=fn) for fn in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertGreaterEqual(timings["first"], 0.4)
        # it queued behind the first retry's wait instead of firing alongside it
        self.assertGreaterEqual(timings["second"], 0.35)

    def test_pause_is_cancellable(self):
        cancel = threading.Event()

        def stopper():
            time.sleep(0.05)
            cancel.set()

        threading.Thread(target=stopper, daemon=True).start()
        started = time.monotonic()
        self.assertTrue(retry._pause("cancel-host", 2.0, cancel, limit=5))
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
