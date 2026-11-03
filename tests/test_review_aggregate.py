"""Merging several reviewers into one report.

The reviewers ran independently, so the same problem arrives several times.
Merging has to do two things at once: collapse the duplicates, because a
finding two models agree on is worth more than one model alone, and keep the
disagreements visible, because a finding two models read differently is
worth reading twice rather than throwing away.
"""

import unittest

from harness.review import aggregate, findings


def a_finding(
    category="correctness",
    severity="high",
    file="app.py",
    line=10,
    summary="the branch is inverted",
    why="the wrong case runs",
    fix="flip it",
    confidence="high",
):
    return findings.Finding(
        category=category,
        severity=severity,
        file=file,
        line=line,
        summary=summary,
        why=why,
        fix=fix,
        confidence=confidence,
    )


def an_outcome(model_id, *found, status="ran"):
    return aggregate.ModelOutcome(
        model_id=model_id, label=model_id.upper(), status=status, findings=list(found)
    )


class Merging(unittest.TestCase):
    def test_two_reviewers_finding_the_same_issue_merge_into_one(self):
        one = an_outcome(
            "mdl-a",
            a_finding(summary="the branch is inverted", why="the wrong case runs"),
        )
        two = an_outcome(
            "mdl-b",
            a_finding(summary="branch inverted", why="the wrong case runs"),
        )

        merged = aggregate.merge([one, two])

        self.assertEqual(len(merged["findings"]), 1, "one issue, one finding")
        self.assertEqual(
            merged["findings"][0].models,
            ("mdl-a", "mdl-b"),
            "the merged finding names both models that raised it",
        )
        self.assertEqual(
            aggregate.agreement_label(merged["findings"][0].models),
            "two reviewers agree",
        )

    def test_a_merged_finding_keeps_the_clearer_wording(self):
        one = an_outcome(
            "mdl-a",
            a_finding(summary="bug", why="it breaks"),
        )
        two = an_outcome(
            "mdl-b",
            a_finding(
                summary="bug",
                why="the condition is inverted, so the wrong branch runs on every write",
            ),
        )

        merged = aggregate.merge([one, two])

        self.assertEqual(
            merged["findings"][0].why,
            "the condition is inverted, so the wrong branch runs on every write",
            "the clearer explanation of the group is kept",
        )

    def test_findings_a_few_lines_apart_are_the_same_issue(self):
        one = an_outcome("mdl-a", a_finding(line=10))
        two = an_outcome("mdl-b", a_finding(line=13))

        merged = aggregate.merge([one, two])

        self.assertEqual(len(merged["findings"]), 1)

    def test_findings_at_different_places_do_not_merge(self):
        one = an_outcome("mdl-a", a_finding(line=10))
        two = an_outcome("mdl-b", a_finding(line=40))

        merged = aggregate.merge([one, two])

        self.assertEqual(
            len(merged["findings"]),
            2,
            "two problems in two places are two findings",
        )
        self.assertEqual(
            sorted(len(f.models) for f in merged["findings"]),
            [1, 1],
            "neither finding claims agreement",
        )

    def test_findings_in_different_files_do_not_merge(self):
        one = an_outcome("mdl-a", a_finding(file="app.py"))
        two = an_outcome("mdl-b", a_finding(file="other.py"))

        merged = aggregate.merge([one, two])

        self.assertEqual(len(merged["findings"]), 2)

    def test_a_disagreement_keeps_both_findings_and_labels_them(self):
        one = an_outcome("mdl-a", a_finding(category="correctness", line=10))
        two = an_outcome("mdl-b", a_finding(category="security", line=10))

        merged = aggregate.merge([one, two])

        self.assertEqual(
            len(merged["findings"]),
            2,
            "a disagreement is kept, not averaged into one finding",
        )
        first, second = merged["findings"]
        self.assertNotEqual(first.category, second.category)
        self.assertIn(second.id, first.conflict_with)
        self.assertIn(first.id, second.conflict_with)

    def test_a_failed_reviewer_contributes_nothing(self):
        broken = an_outcome("mdl-b", a_finding(summary="never said"), status="failed")
        good = an_outcome("mdl-a", a_finding(summary="real"))

        merged = aggregate.merge([broken, good])

        self.assertEqual(len(merged["findings"]), 1)
        self.assertEqual(merged["findings"][0].summary, "real")

    def test_every_finding_gets_an_id(self):
        merged = aggregate.merge(
            [an_outcome("mdl-a", a_finding(line=10), a_finding(line=20))]
        )

        ids = [f.id for f in merged["findings"]]
        self.assertEqual(len(set(ids)), len(ids), "the ids are unique")
        self.assertTrue(all(ids), "every finding is addressable")


class Verdict(unittest.TestCase):
    def test_no_findings_is_a_clean_review(self):
        merged = aggregate.merge([an_outcome("mdl-a")])

        self.assertEqual(merged["verdict"], "clean")
        self.assertEqual(merged["verdict_label"], "clean")

    def test_only_medium_and_low_findings_are_suggestions(self):
        merged = aggregate.merge(
            [
                an_outcome(
                    "mdl-a",
                    a_finding(severity="medium"),
                    a_finding(severity="low", line=20),
                )
            ]
        )

        self.assertEqual(merged["verdict"], "suggestions")

    def test_a_blocker_asks_for_changes(self):
        merged = aggregate.merge([an_outcome("mdl-a", a_finding(severity="blocker"))])

        self.assertEqual(merged["verdict"], "changes_requested")
        self.assertEqual(merged["verdict_label"], "changes requested")

    def test_a_high_severity_asks_for_changes(self):
        merged = aggregate.merge([an_outcome("mdl-a", a_finding(severity="high"))])

        self.assertEqual(merged["verdict"], "changes_requested")


class Counts(unittest.TestCase):
    def test_the_counts_by_severity_are_right(self):
        merged = aggregate.merge(
            [
                an_outcome(
                    "mdl-a",
                    a_finding(severity="high"),
                    a_finding(severity="low", line=20),
                    a_finding(severity="low", line=30),
                )
            ]
        )

        self.assertEqual(merged["by_severity"]["high"], 1)
        self.assertEqual(merged["by_severity"]["low"], 2)
        self.assertEqual(merged["by_severity"]["medium"], 0)
        self.assertEqual(merged["by_severity"]["blocker"], 0)

    def test_the_counts_by_category_are_right(self):
        merged = aggregate.merge(
            [
                an_outcome(
                    "mdl-a",
                    a_finding(category="security"),
                    a_finding(category="security", line=20),
                    a_finding(category="tests", line=30),
                )
            ]
        )

        self.assertEqual(merged["by_category"]["security"], 2)
        self.assertEqual(merged["by_category"]["tests"], 1)
        self.assertEqual(merged["by_category"]["correctness"], 0)

    def test_a_merged_finding_is_counted_once(self):
        one = an_outcome("mdl-a", a_finding(category="security"))
        two = an_outcome("mdl-b", a_finding(category="security"))

        merged = aggregate.merge([one, two])

        self.assertEqual(
            merged["by_category"]["security"],
            1,
            "one merged finding, one count",
        )


class Threshold(unittest.TestCase):
    def test_findings_below_the_threshold_are_suppressed_not_shown(self):
        merged = aggregate.merge(
            [
                an_outcome(
                    "mdl-a",
                    a_finding(severity="high"),
                    a_finding(severity="medium", line=20),
                    a_finding(severity="nit", line=30),
                )
            ],
            threshold="high",
        )

        shown = [f.severity for f in merged["findings"]]
        self.assertEqual(shown, ["high"], "only what is at or above the bar is shown")
        self.assertEqual(len(merged["suppressed"]), 2, "the rest is counted")
        self.assertEqual(merged["threshold"], "high")

    def test_the_low_threshold_shows_everything_but_the_nits(self):
        merged = aggregate.merge(
            [an_outcome("mdl-a", a_finding(severity="nit"))], threshold="low"
        )

        self.assertEqual(len(merged["findings"]), 0)
        self.assertEqual(len(merged["suppressed"]), 1)

    def test_the_nit_threshold_shows_the_nits(self):
        merged = aggregate.merge(
            [an_outcome("mdl-a", a_finding(severity="nit"))], threshold="nit"
        )

        self.assertEqual(len(merged["findings"]), 1)
        self.assertEqual(len(merged["suppressed"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
