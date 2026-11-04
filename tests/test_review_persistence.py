"""The saved report: what survives the turn, and what stays out of sight.

A report has to outlive the turn that made it, because the card, the agent's
context and the next session all read the same saved document. It also has
to stay where it belongs: a review of one session's work is not another
session's business, and a review of work that a rewind threw away must stop
being the current answer.
"""

import unittest
from unittest import mock

from harness.review import engine, findings, report, scope
from tests.review_support import FakeLLM, ReviewCase

A_FINDING = (
    '{"findings": [{"category": "correctness", "severity": "high", '
    '"file": "app.py", "line": 1, "summary": "the value is wrong", '
    '"why": "it writes the old value", "fix": "use the new one", '
    '"confidence": "high"}]}'
)

A_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-value = 1\n"
    "+value = 2\n"
)


def a_scope(path="app.py"):
    return scope.Scope(
        mode="run",
        files=[scope.FileChange(path=path, diff=A_DIFF, added=1, removed=1)],
    )


def a_finding(file="app.py", line=1, severity="high"):
    return findings.Finding(
        category="correctness",
        severity=severity,
        file=file,
        line=line,
        summary="the value is wrong",
        why="it writes the old value",
        fix="use the new one",
        confidence="high",
    )


def a_merged(*found):
    return {
        "verdict": "changes_requested",
        "verdict_label": "changes requested",
        "findings": list(found),
        "suppressed": [],
        "by_severity": {"high": len(found)},
        "by_category": {"correctness": len(found)},
        "models": [],
        "threshold": "low",
    }


class SavedReports(ReviewCase):
    def test_a_report_is_saved_and_reloaded(self):
        document = report.build("rv-test", a_scope(), a_merged(a_finding()))

        report.save(self.session_id, document)
        loaded = report.load(self.session_id, "rv-test")

        self.assertIsNotNone(loaded, "the report is on disk")
        self.assertEqual(loaded["review_id"], "rv-test")
        self.assertEqual(loaded["verdict"], "changes_requested")
        self.assertEqual(len(loaded["findings"]), 1)
        self.assertEqual(loaded["findings"][0]["summary"], "the value is wrong")

    def test_the_latest_report_is_the_newest_one(self):
        first = report.build("rv-one", a_scope(), a_merged(a_finding()))
        second = report.build("rv-two", a_scope(), a_merged(a_finding(line=1)))
        report.save(self.session_id, first)
        report.save(self.session_id, second)

        latest = report.latest(self.session_id)

        self.assertIsNotNone(latest)
        self.assertEqual(latest["review_id"], "rv-two")

    def test_loading_a_report_that_was_never_saved_returns_nothing(self):
        self.assertIsNone(report.load(self.session_id, "rv-missing"))
        self.assertIsNone(report.latest(self.session_id))

    def test_a_dismissed_finding_is_remembered(self):
        document = report.build("rv-test", a_scope(), a_merged(a_finding()))
        report.save(self.session_id, document)

        report.dismiss(self.session_id, "rv-test", "R1")

        loaded = report.load(self.session_id, "rv-test")
        self.assertEqual(loaded["dismissed"], ["R1"], "the dismissal is remembered")
        self.assertEqual(
            len(loaded["findings"]), 1, "the finding itself is still there to read"
        )

    def test_dismissing_twice_keeps_one_entry(self):
        document = report.build("rv-test", a_scope(), a_merged(a_finding()))
        report.save(self.session_id, document)

        report.dismiss(self.session_id, "rv-test", "R1")
        report.dismiss(self.session_id, "rv-test", "R1")

        self.assertEqual(report.load(self.session_id, "rv-test")["dismissed"], ["R1"])


class StaleReports(ReviewCase):
    def test_a_review_after_a_rewind_is_marked_stale(self):
        early = report.build(
            "rv-early", a_scope(), a_merged(a_finding()), message_index=3
        )
        late = report.build(
            "rv-late", a_scope(), a_merged(a_finding()), message_index=9
        )
        report.save(self.session_id, early)
        report.save(self.session_id, late)

        changed = report.mark_stale(self.session_id, 5)

        self.assertEqual(
            [r["review_id"] for r in changed],
            ["rv-late"],
            "only the work after the rewind point is stale",
        )
        self.assertTrue(report.load(self.session_id, "rv-late")["stale"])
        self.assertFalse(
            report.load(self.session_id, "rv-early")["stale"],
            "the review of the work that was kept is still current",
        )

    def test_a_stale_report_is_not_served_from_the_cache(self):
        document = report.build(
            "rv-test", a_scope(), a_merged(a_finding()), cache_key="key-1"
        )
        report.save(self.session_id, document)

        self.assertIsNotNone(report.cached(self.session_id, "key-1"))

        report.mark_stale(self.session_id, 0)

        self.assertIsNone(
            report.cached(self.session_id, "key-1"),
            "a stale answer is not the current answer",
        )


class SessionScope(ReviewCase):
    def test_a_report_belongs_to_the_session_that_made_it(self):
        document = report.build("rv-test", a_scope(), a_merged(a_finding()))
        report.save("sess-one", document)

        self.assertIsNotNone(report.load("sess-one", "rv-test"))
        self.assertIsNone(
            report.load("sess-two", "rv-test"),
            "another session cannot reach this report",
        )
        self.assertIsNone(report.latest("sess-two"))
        self.assertEqual(report.all_reports("sess-two"), [])

    def test_two_sessions_keep_their_own_reports(self):
        report.save(
            "sess-one", report.build("rv-one", a_scope(), a_merged(a_finding()))
        )
        report.save(
            "sess-two",
            report.build("rv-two", a_scope(), a_merged(a_finding(severity="low"))),
        )

        self.assertEqual(
            report.latest("sess-one")["review_id"], "rv-one", "each session its own"
        )
        self.assertEqual(report.latest("sess-two")["review_id"], "rv-two")


class CachedReviews(ReviewCase):
    def setUp(self):
        super().setUp()
        self.register_models("mdl-a")
        self.record_change("app.py", "value = 1\n", "value = 2\n")

    def review(self, fake, **kwargs):
        settings = self.settings()
        with mock.patch("harness.review.engine.call_llm", fake):
            return engine.run(
                self.root,
                self.session_id,
                scope=scope.detect_for_settings(
                    self.root,
                    settings,
                    mode="run",
                    session_id=self.session_id,
                ),
                reviewers=[
                    {"model_id": "mdl-a", "label": "", "enabled": True, "focus": ""}
                ],
                settings=settings,
                **kwargs,
            )

    def test_reviewing_the_same_change_twice_answers_from_the_cache(self):
        fake = FakeLLM(replies={"mdl-a": A_FINDING})

        first = self.review(fake)
        second = self.review(fake)

        self.assertEqual(
            len(fake.calls), 1, "the second review did not spend money again"
        )
        self.assertEqual(
            second["review_id"], first["review_id"], "it is the same report"
        )
        self.assertTrue(second.get("cached"), "the report says it came from the cache")

    def test_force_bypasses_the_cache(self):
        fake = FakeLLM(replies={"mdl-a": A_FINDING})

        self.review(fake)
        forced = self.review(fake, force=True)

        self.assertEqual(len(fake.calls), 2, "a forced review asks the model again")
        self.assertNotEqual(forced["review_id"], "", "it produced a fresh report")

    def test_a_changed_file_is_reviewed_again(self):
        fake = FakeLLM(replies={"mdl-a": A_FINDING})

        self.review(fake)
        self.record_change("app.py", "value = 2\n", "value = 3\n")
        again = self.review(fake)

        self.assertEqual(
            len(fake.calls), 2, "the change is different, so it is reviewed"
        )
        self.assertFalse(again.get("cached", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
