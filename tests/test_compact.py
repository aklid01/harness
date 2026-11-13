"""Compaction: the safe cut point, the handoff note, and the /compact flow.

The compactor is the one place that throws transcript away for good, so the
tests pin the invariants rather than the arithmetic: the system message and a
suffix of the original survive, the handoff note replaces everything before
it, and a tool result is never separated from the assistant message that
asked for it.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import commands, events, history, paths
from harness.compact import compact, needed, safe_boundary, tail_start

SUMMARY_TEXT = "## Goal\nmake the compact tests pass"


def tool_call_pair(index):
    """An assistant message asking for one tool call, plus its result."""
    call_id = f"call-{index}"
    return [
        {
            "role": "assistant",
            "content": f"working {index}",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": f"echo {index}"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": f"out {index}"},
    ]


def transcript(pairs=8):
    """system + `pairs` tool exchanges + one final answer."""
    messages = [{"role": "system", "content": "system prompt"}]
    for index in range(pairs):
        messages.extend(tool_call_pair(index))
    messages.append({"role": "assistant", "content": "final answer"})
    return messages


def fake_summariser():
    """A fake chat client whose completions return the canned handoff note."""
    response = mock.Mock()
    response.choices = [mock.Mock()]
    response.choices[0].message.content = SUMMARY_TEXT
    client = mock.Mock()
    client.chat.completions.create.return_value = response
    return client


class SafeBoundary(unittest.TestCase):
    def setUp(self):
        self.messages = [{"role": "system", "content": "s"}]
        self.messages.extend(tool_call_pair(0))
        self.messages.extend(tool_call_pair(1))
        self.messages.append({"role": "assistant", "content": "done"})

    def test_a_tool_result_and_its_asker_move_together(self):

        for start in range(1, len(self.messages) + 1):
            cut = safe_boundary(self.messages, start)
            self.assertGreaterEqual(cut, max(start, 1))
            if cut == len(self.messages):
                continue
            self.assertNotEqual(self.messages[cut]["role"], "tool")
            self.assertFalse(self.messages[cut - 1].get("tool_calls"))

    def test_a_fresh_exchange_is_a_safe_cut(self):
        self.assertEqual(safe_boundary(self.messages, 1), 1)

    def test_past_the_end_is_the_end(self):
        self.assertEqual(
            safe_boundary(self.messages, len(self.messages)), len(self.messages)
        )

    def test_tail_start_buys_a_suffix_that_fits(self):
        messages = transcript(pairs=8)
        cut = tail_start(messages, 300)
        self.assertGreater(cut, 1)
        self.assertLess(cut, len(messages))


class Needed(unittest.TestCase):
    def test_over_the_compact_line(self):
        self.assertTrue(needed({"prompt_tokens": 100}, window=100))

    def test_under_it(self):
        self.assertFalse(needed({"prompt_tokens": 10}, window=100))


class CompactRebuilds(unittest.TestCase):
    def test_keeps_system_and_suffix_and_summarises_the_prefix(self):
        messages = transcript(pairs=8)
        with (
            mock.patch("harness.compact.get_client", return_value=fake_summariser()),
            mock.patch("harness.compact.active_model", return_value="test-model"),
        ):
            result = compact(messages, window=1000)

        self.assertIsNot(result, messages)
        self.assertEqual(result[0], messages[0])
        self.assertTrue(
            result[1]["content"].startswith(history.SUMMARY),
            "the handoff note is marked as compacted memory",
        )
        self.assertIn(SUMMARY_TEXT, result[1]["content"])

        tail_len = len(result) - 2
        self.assertEqual(result[2:], messages[len(messages) - tail_len :])

        self.assertEqual(result[-1], messages[-1])

    def test_no_tool_result_is_orphaned_in_the_result(self):
        messages = transcript(pairs=8)
        with (
            mock.patch("harness.compact.get_client", return_value=fake_summariser()),
            mock.patch("harness.compact.active_model", return_value="test-model"),
        ):
            result = compact(messages, window=1000)
        for index, message in enumerate(result):
            if message["role"] == "tool":
                self.assertGreaterEqual(index, 3)
                self.assertTrue(result[index - 1].get("tool_calls"))

    def test_nothing_old_enough_leaves_the_transcript_alone(self):
        messages = transcript(pairs=8)
        with (
            mock.patch("harness.compact.get_client", return_value=fake_summariser()),
            mock.patch("harness.compact.active_model", return_value="test-model"),
        ):
            result = compact(messages, window=1_000_000)
        self.assertEqual(result, messages)


class CompactCommand(unittest.TestCase):
    """The /compact flow: summary in, transcript replaced, session saved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "app",
        )
        self.addCleanup(paths.reset)
        events.reset()
        self.seen = []
        events.subscribe(lambda kind, payload: self.seen.append((kind, payload)))
        self.addCleanup(events.reset)

    def test_a_successful_compact_replaces_and_persists(self):
        messages = transcript(pairs=8)
        with (
            mock.patch("harness.compact.get_client", return_value=fake_summariser()),
            mock.patch("harness.compact.active_model", return_value="test-model"),
            mock.patch("harness.session.compacted") as saved,
        ):
            result = commands.compact(messages, window=1000)
        self.assertLess(len(result), len(messages))
        saved.assert_called_once_with(result)
        kinds = [kind for kind, _ in self.seen]
        self.assertIn(events.COMPACTED, kinds)

    def test_a_summariser_failure_keeps_the_transcript(self):
        messages = transcript(pairs=8)
        broken = mock.Mock()
        broken.chat.completions.create.side_effect = ConnectionError("provider down")
        with (
            mock.patch("harness.compact.get_client", return_value=broken),
            mock.patch("harness.compact.active_model", return_value="test-model"),
            mock.patch("harness.session.compacted") as saved,
        ):
            result = commands.compact(messages, window=1000)
        self.assertEqual(result, messages)
        saved.assert_not_called()
        notes = [
            str(payload.get("text") or "")
            for kind, payload in self.seen
            if kind == events.NOTE
        ]
        self.assertTrue(any("compaction failed" in text for text in notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
