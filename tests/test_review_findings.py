"""What a reviewer returns, and what survives the check.

A reviewer is a model, so its answer is a rumour: parsed, checked, and
thrown away where it does not hold up. A finding that names a file nobody
reviewed, or a line the reviewer was never shown, is a hallucination - and a
hallucination that reaches a user costs more than a missed nit.
"""

import unittest

from harness.review import findings

A_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -10,3 +10,4 @@\n"
    " keep me\n"
    "-old line\n"
    "+new line\n"
    "+another new line\n"
    " keep me too\n"
)


def a_finding(**over):
    finding = {
        "category": "correctness",
        "severity": "high",
        "file": "app.py",
        "line": 11,
        "summary": "the branch is inverted",
        "why": "the wrong case runs",
        "fix": "flip it",
        "confidence": "high",
    }
    finding.update(over)
    return finding


class Parsing(unittest.TestCase):
    def test_a_valid_reply_yields_findings(self):
        reply = '{"findings": [' + str(a_finding()).replace("'", '"') + "]}"

        raw = findings.parse(reply)

        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["summary"], "the branch is inverted")

    def test_a_fenced_reply_is_still_parsed(self):
        raw = findings.parse('```json\n{"findings": [], "note": "fine"}\n```')

        self.assertEqual(raw, [])

    def test_a_reply_with_no_findings_is_an_empty_list(self):
        raw = findings.parse('{"findings": [], "note": "no issues found"}')

        self.assertEqual(raw, [], "a clean review is a result, not an error")

    def test_a_reply_that_only_says_it_found_nothing_is_an_empty_list(self):
        raw = findings.parse('{"note": "no issues found"}')

        self.assertEqual(raw, [])

    def test_a_list_reply_is_read_as_the_findings(self):
        raw = findings.parse('[{"category": "tests", "summary": "no test"}]')

        self.assertEqual(len(raw), 1)

    def test_entries_that_are_not_objects_are_dropped(self):
        raw = findings.parse('{"findings": [{"summary": "real"}, "junk"]}')

        self.assertEqual(len(raw), 1)

    def test_malformed_output_raises_a_format_error(self):
        for reply in ("", "   ", "I looked at the diff and it looks fine to me"):
            with self.assertRaises(findings.ReviewFormatError):
                findings.parse(reply)

    def test_a_findings_key_that_is_not_a_list_raises_a_format_error(self):
        with self.assertRaises(findings.ReviewFormatError):
            findings.parse('{"findings": "one big finding"}')

    def test_a_reply_without_a_findings_key_raises_a_format_error(self):
        with self.assertRaises(findings.ReviewFormatError):
            findings.parse('{"verdict": "approve"}')


class Validation(unittest.TestCase):
    def setUp(self):
        self.allowed = ["app.py"]
        self.known = {"app.py": {10, 11, 12, 13}}

    def test_a_valid_finding_survives(self):
        kept, rejected = findings.validate([a_finding()], self.allowed, self.known)

        self.assertEqual(len(kept), 1, "a real finding is kept")
        self.assertEqual(rejected, [])
        self.assertEqual(kept[0].file, "app.py")
        self.assertEqual(kept[0].line, 11)
        self.assertEqual(kept[0].severity, "high")

    def test_a_finding_citing_a_line_outside_the_diff_is_discarded(self):
        kept, rejected = findings.validate(
            [a_finding(line=99)], self.allowed, self.known
        )

        self.assertEqual(kept, [], "the reviewer was never shown line 99")
        self.assertEqual(len(rejected), 1)
        self.assertIn("line is not in the diff", rejected[0]["reason"])

    def test_a_finding_about_a_file_that_was_not_reviewed_is_discarded(self):
        kept, rejected = findings.validate(
            [a_finding(file="secret.py")], self.allowed, self.known
        )

        self.assertEqual(kept, [], "a reviewer cannot know about a file it never saw")
        self.assertEqual(len(rejected), 1)
        self.assertIn("file was not reviewed", rejected[0]["reason"])

    def test_a_valid_finding_survives_while_the_others_are_discarded(self):
        kept, rejected = findings.validate(
            [
                a_finding(line=11, summary="real one"),
                a_finding(line=99, summary="invented line"),
                a_finding(file="other.py", summary="invented file"),
            ],
            self.allowed,
            self.known,
        )

        self.assertEqual([f.summary for f in kept], ["real one"])
        self.assertEqual(len(rejected), 2)

    def test_a_path_quoted_the_way_the_diff_quotes_it_is_accepted(self):
        for quoted in ("b/app.py", "./app.py", "app.py"):
            kept, _ = findings.validate(
                [a_finding(file=quoted)], self.allowed, self.known
            )
            self.assertEqual(
                len(kept), 1, f"{quoted} names the reviewed file and must be kept"
            )

    def test_a_finding_without_a_summary_is_discarded(self):
        kept, rejected = findings.validate(
            [a_finding(summary="")], self.allowed, self.known
        )

        self.assertEqual(kept, [])
        self.assertIn("no summary", rejected[0]["reason"])

    def test_a_finding_without_a_line_is_discarded(self):
        kept, rejected = findings.validate(
            [a_finding(line=0)], self.allowed, self.known
        )

        self.assertEqual(kept, [])
        self.assertIn("no line number", rejected[0]["reason"])


class Defaults(unittest.TestCase):
    def setUp(self):
        self.known = {"app.py": {10, 11, 12, 13}}

    def test_an_unknown_category_becomes_other(self):
        kept, _ = findings.validate(
            [a_finding(category="wobble")], ["app.py"], self.known
        )

        self.assertEqual(kept[0].category, "other")
        self.assertEqual(
            kept[0].raw_category, "wobble", "what the model said is kept for the record"
        )

    def test_an_unknown_severity_becomes_medium(self):
        kept, _ = findings.validate(
            [a_finding(severity="urgent")], ["app.py"], self.known
        )

        self.assertEqual(kept[0].severity, "medium")

    def test_an_unknown_confidence_becomes_medium(self):
        kept, _ = findings.validate(
            [a_finding(confidence="maybe")], ["app.py"], self.known
        )

        self.assertEqual(kept[0].confidence, "medium")

    def test_a_known_category_is_kept_as_it_is(self):
        kept, _ = findings.validate(
            [a_finding(category="security")], ["app.py"], self.known
        )

        self.assertEqual(kept[0].category, "security")


class DiffLines(unittest.TestCase):
    def test_diff_lines_extracts_the_lines_a_diff_touches(self):
        lines = findings.diff_lines(A_DIFF)

        self.assertEqual(lines["app.py"], {10, 11, 12, 13})

    def test_diff_lines_covers_every_file_in_the_diff(self):
        other = A_DIFF.replace("app.py", "other.py")
        lines = findings.diff_lines(A_DIFF + other)

        self.assertEqual(sorted(lines), ["app.py", "other.py"])

    def test_a_deleted_only_hunk_keeps_its_old_line_numbers(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -5,2 +4,0 @@\n"
            "-gone\n"
            "-also gone\n"
        )

        lines = findings.diff_lines(diff)

        self.assertEqual(lines["app.py"], {5, 6})

    def test_an_empty_diff_touches_nothing(self):
        self.assertEqual(findings.diff_lines(""), {})
        self.assertEqual(findings.diff_lines(None), {})

    def test_a_new_file_diff_covers_every_line_it_adds(self):
        diff = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+one\n"
            "+two\n"
            "+three\n"
        )

        lines = findings.diff_lines(diff)

        self.assertEqual(lines["new.py"], {1, 2, 3})


if __name__ == "__main__":
    unittest.main(verbosity=2)
