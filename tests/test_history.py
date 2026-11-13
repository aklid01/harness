"""History: cap parks a spill, strip stubs once and idempotently,
sweep deletes the turn's spills, and prepare trims without touching the
stored transcript. The locked prefix survives all of it."""

import json
import os
import unittest
from unittest import mock

from harness import history



def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


def tool_result(content, call_id="call-1"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TurnSpills(unittest.TestCase):
    def setUp(self):
        history.begin_turn()
        self.addCleanup(history.sweep)

    def test_cap_under_the_limit_is_verbatim(self):
        self.assertEqual(history.cap("short"), "short")

    def test_cap_parks_the_full_text_and_leaves_a_pointer(self):
        text = "x" * (history.CAP + 500)
        result = history.cap(text)
        self.assertTrue(result.startswith(text[: history.CAP]))
        self.assertIn(history.CAPPED, result)
        self.assertNotIn(text, result)

        spills = history._turn_spills()
        self.assertEqual(len(spills), 1)
        self.assertEqual(spills[0].read_text(encoding="utf-8"), text)

    def test_cap_without_a_temp_file_still_trims(self):
        text = "y" * (history.CAP + 10)
        with mock.patch("harness.history.spill", side_effect=OSError("no space")):
            result = history.cap(text)
        self.assertIn(history.CAPPED, result)
        self.assertIn("could not be saved", result)

    def test_sweep_deletes_this_turns_spills(self):
        history.cap("z" * (history.CAP + 1))
        spills = list(history._turn_spills())
        self.assertEqual(len(spills), 1)
        self.assertTrue(spills[0].exists())
        history.sweep()
        self.assertFalse(spills[0].exists())
        self.assertEqual(history._turn_spills(), [])

    def test_begin_turn_starts_a_fresh_registry(self):
        history.cap("z" * (history.CAP + 1))
        history.begin_turn()
        self.assertEqual(history._turn_spills(), [])


class Strip(unittest.TestCase):
    def test_stubs_unlocked_tool_results_once(self):
        messages = [user("hi"), tool_result("a" * 1000)]
        shrunk = history.strip(messages)
        self.assertEqual(shrunk, 1)
        self.assertIn(history.TRIMMED, messages[1]["content"])
        self.assertLess(len(messages[1]["content"]), 1000)

        before = messages[1]["content"]
        self.assertEqual(history.strip(messages), 0)
        self.assertEqual(messages[1]["content"], before)

    def test_a_capped_result_still_strips(self):
        messages = [
            user("hi"),
            tool_result("c" * (history.STUB + 500) + f"\n\n{history.CAPPED} cut.]"),
        ]
        self.assertEqual(history.strip(messages), 1)
        self.assertIn(history.TRIMMED, messages[1]["content"])

    def test_result_ahead_of_the_summary_is_still_live(self):
        messages = [
            {"role": "system", "content": "s"},
            assistant("older question"),
            tool_result("a" * 2000),
            user(f"{history.SUMMARY} carried over"),
        ]

        self.assertEqual(history.strip(messages), 0)

    def test_result_after_the_summary_stubs(self):
        messages = [
            {"role": "system", "content": "s"},
            user(f"{history.SUMMARY} carried over"),
            assistant("newer question"),
            tool_result("a" * 2000),
        ]
        self.assertEqual(history.strip(messages), 1)
        self.assertIn(history.TRIMMED, messages[3]["content"])


class Prepare(unittest.TestCase):
    def transcript(self, exchanges=14):
        """A realistic transcript: each assistant turn carries the tool_call
        it made, and its tool result follows it."""
        messages = [
            {"role": "system", "content": "system prompt"},
            user(f"{history.SUMMARY} carried over"),
        ]
        for index in range(exchanges):
            call_id = f"call-{index}"
            messages.append(
                {
                    "role": "assistant",
                    "content": f"old work {index}",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": json.dumps({"command": "ls"}),
                            },
                        }
                    ],
                }
            )
            messages.append(tool_result("t" * 200, call_id))
        messages.append(user("current question"))
        return messages

    def test_small_transcripts_pass_through_untouched(self):
        messages = self.transcript(exchanges=2)
        request, note = history.prepare(messages, window=1_000_000)
        self.assertEqual(request, messages)
        self.assertEqual(note, "")

    def test_an_oversized_request_is_trimmed_without_touching_storage(self):
        messages = self.transcript(exchanges=14)
        request, note = history.prepare(messages, window=200)
        self.assertTrue(note, "the user is told the request was trimmed")
        self.assertIn("left out", note)

        frozen = history.frozen_prefix_len(messages)
        self.assertEqual(request[:frozen], messages[:frozen])

        self.assertEqual(request[-1], messages[-1])

        self.assertEqual(messages[3]["content"], "t" * 200)

        for index, message in enumerate(request):
            if message["role"] == "tool":
                self.assertTrue(request[index - 1].get("tool_calls"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
