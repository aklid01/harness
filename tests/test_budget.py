"""Phase budgets: the limits that end a phase, and the repeat detector."""

import os
import unittest


from harness.budget import Budget, Progress


class BudgetCase(unittest.TestCase):
    def test_turn_trips_after_the_last_allowed_request(self):
        budget = Budget(turns=2)
        self.assertFalse(budget.turn())
        self.assertFalse(budget.turn())
        self.assertTrue(budget.turn())

    def test_tool_trips_after_the_last_allowed_call(self):
        budget = Budget(tool_calls=1)
        self.assertFalse(budget.tool())
        self.assertTrue(budget.tool())

    def test_unset_limits_never_trip(self):
        budget = Budget()
        for _ in range(5):
            self.assertFalse(budget.turn())
            self.assertFalse(budget.tool())
        self.assertFalse(budget.timed_out())
        self.assertEqual(budget.reason(), "ran out of budget")

    def test_reason_names_the_budget_that_ran_out(self):
        budget = Budget(turns=1, tool_calls=1)
        budget.turn()
        budget.tool()
        budget.turn()
        budget.tool()
        self.assertEqual(budget.reason(), "hit its request budget")
        elapsed = Budget(seconds=10)
        elapsed._start -= 20
        self.assertEqual(elapsed.reason(), "hit its time budget")

    def test_snapshot_counts_requests_and_calls(self):
        budget = Budget(turns=10)
        budget.turn()
        budget.turn()
        budget.tool()
        snap = budget.snapshot()
        self.assertEqual(snap["turns"], 2)
        self.assertEqual(snap["tools"], 1)


class ProgressCase(unittest.TestCase):
    def test_repeats_trip_only_after_the_limit(self):
        progress = Progress(limit=3)
        for _ in range(3):
            self.assertFalse(progress.repeat(("bash", "npm test")))
        self.assertTrue(progress.repeat(("bash", "npm test")))

    def test_a_different_step_resets_the_streak(self):
        progress = Progress(limit=2)
        self.assertFalse(progress.repeat(("bash", "a")))
        self.assertFalse(progress.repeat(("bash", "b")))
        self.assertFalse(progress.repeat(("bash", "a")))
        self.assertFalse(progress.repeat(("bash", "b")))
        self.assertFalse(progress.repeat(("bash", "b")))
        self.assertTrue(progress.repeat(("bash", "b")))

    def test_limit_zero_trips_on_the_first_step(self):
        progress = Progress(limit=0)
        self.assertTrue(progress.repeat("anything"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
