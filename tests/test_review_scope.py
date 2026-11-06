"""What gets reviewed: the four scopes, and the files they leave out.

A scope is the answer to "which files is this review about", and it is the
part of the review a user cannot check for the engine: everything else is
either reported back to them or thrown away silently. So these tests are
about what the scope names, what it skips, and what it says about the skips.
"""

import unittest

from harness.review import scope
from tests.review_support import ReviewCase, commit_all, git, init_repo


class ScopeCase(ReviewCase):
    def skipped_names(self, detected):
        return [item["path"] for item in detected.skipped]

    def skip_reason(self, detected, path):
        for item in detected.skipped:
            if item["path"] == path:
                return item["reason"]
        return None


class GitScopes(ScopeCase):
    def setUp(self):
        super().setUp()
        init_repo(self.root)
        self.write("a.py", "one\n")
        self.write("b.py", "one\n")
        commit_all(self.root, "first")

    def test_run_scope_covers_exactly_the_files_the_run_touched(self):
        self.write("a.py", "two\n")
        self.write("b.py", "two\n")

        detected = scope.detect(self.root, mode="run", only_paths=[self.root / "a.py"])

        self.assertEqual(
            detected.paths(),
            ["a.py"],
            "the run scope must name the run's files and nothing else",
        )
        self.assertIn("two", detected.files[0].diff, "the diff is the change itself")

    def test_uncommitted_scope_covers_the_working_tree(self):
        self.write("a.py", "two\n")
        self.write("new.py", "fresh\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertEqual(
            sorted(detected.paths()),
            ["a.py", "new.py"],
            "every working-tree change is in scope, tracked or not",
        )
        self.assertNotIn("b.py", detected.paths(), "an untouched file is not reviewed")

    def test_staged_scope_covers_the_index(self):
        self.write("a.py", "two\n")
        git(self.root, "add", "a.py")
        self.write("b.py", "two\n")

        detected = scope.detect(self.root, mode="staged")

        self.assertEqual(
            detected.paths(),
            ["a.py"],
            "only what is staged for the next commit is in scope",
        )
        self.assertIn("two", detected.files[0].diff)

    def test_base_scope_covers_the_difference_from_a_branch(self):
        git(self.root, "checkout", "-q", "-b", "feature")
        self.write("feature.py", "only on the branch\n")
        commit_all(self.root, "feature work")

        detected = scope.detect(self.root, mode="base", base="main")

        self.assertEqual(
            detected.paths(),
            ["feature.py"],
            "the base scope is the difference against the named ref",
        )
        self.assertEqual(detected.base, "main")

    def test_untracked_files_come_in_as_new_file_diffs(self):
        self.write("added.py", "brand new\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertIn("added.py", detected.paths())
        change = next(c for c in detected.files if c.path == "added.py")
        self.assertEqual(change.status, "A", "an untracked file is an addition")
        self.assertIn("new file mode", change.diff)
        self.assertIn("+brand new", change.diff)

    def test_a_repository_with_no_commits_still_has_a_scope(self):
        unborn = self.root / "unborn"
        unborn.mkdir()
        init_repo(unborn)
        (unborn / "first.txt").write_text("hello\n", encoding="utf-8")
        git(unborn, "add", "first.txt")

        staged = scope.detect(unborn, mode="staged")
        self.assertEqual(
            staged.paths(),
            ["first.txt"],
            "an unborn HEAD has no HEAD to diff, but the index still holds a change",
        )

        everything = scope.detect(unborn, mode="uncommitted")
        self.assertEqual(everything.paths(), ["first.txt"])

        run = scope.detect(unborn, mode="run", only_paths=[unborn / "first.txt"])
        self.assertEqual(run.paths(), ["first.txt"])

    def test_a_detached_head_is_reviewed_like_any_other(self):
        git(self.root, "checkout", "-q", "--detach", "HEAD")
        self.write("a.py", "two\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertEqual(
            detected.paths(),
            ["a.py"],
            "a detached HEAD is still a commit to diff against",
        )
        self.assertTrue(detected.head, "the detached commit is recorded")

    def test_a_path_with_a_space_is_reviewed_under_its_real_name(self):
        self.write("my file.py", "one\n")
        commit_all(self.root, "spaced name")
        self.write("my file.py", "two\n")

        detected = scope.detect(self.root, mode="uncommitted")
        self.assertEqual(detected.paths(), ["my file.py"])

        run = scope.detect(self.root, mode="run", only_paths=[self.root / "my file.py"])
        self.assertEqual(run.paths(), ["my file.py"])
        self.assertIn("two", run.files[0].diff)

    def test_a_unicode_filename_is_reviewed_under_its_real_name(self):
        self.write("café notes.txt", "one\n")
        commit_all(self.root, "unicode name")
        self.write("café notes.txt", "two\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertEqual(
            detected.paths(),
            ["café notes.txt"],
            "a file with a non-ascii name must still reach the reviewer",
        )


class NonRepoScopes(ScopeCase):
    def test_the_checkpoint_journal_is_the_scope_outside_a_repository(self):
        self.assertFalse(
            scope.is_repo(self.root), "this workspace must not be a repository"
        )
        self.record_change("app.py", "before\n", "after\n")

        detected = scope.detect(self.root, mode="run", session_id=self.session_id)

        self.assertFalse(detected.is_repo, "a workspace without git says so")
        self.assertEqual(detected.paths(), ["app.py"])
        self.assertIn("+after", detected.files[0].diff)
        self.assertIn("-before", detected.files[0].diff)

    def test_the_journal_of_another_session_is_not_reviewed(self):
        self.record_change("app.py", "before\n", "after\n", session_id="sess-other")

        detected = scope.detect(self.root, mode="run", session_id=self.session_id)

        self.assertEqual(
            detected.paths(),
            [],
            "a review only sees the changes of its own session",
        )

    def test_only_the_files_the_run_touched_are_reviewed(self):
        self.record_change("app.py", "before\n", "after\n")
        self.record_change("other.py", "before\n", "after\n")

        detected = scope.detect(
            self.root,
            mode="run",
            only_paths=[self.root / "app.py"],
            session_id=self.session_id,
        )

        self.assertEqual(detected.paths(), ["app.py"])


class Exclusions(ScopeCase):
    def setUp(self):
        super().setUp()
        init_repo(self.root)
        self.write("app.py", "one\n")
        commit_all(self.root, "first")

    def test_lockfiles_and_minified_files_are_skipped_by_name(self):
        self.write("poetry.lock", "locked\n")
        self.write("package-lock.json", "{}\n")
        self.write("bundle.min.js", "minified\n")
        self.write("app.py", "two\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertEqual(detected.paths(), ["app.py"], "only real source is reviewed")
        skipped = self.skipped_names(detected)
        for name in ("poetry.lock", "package-lock.json", "bundle.min.js"):
            self.assertIn(name, skipped, f"{name} must be reported as skipped, by name")
        self.assertIn("excluded", self.skip_reason(detected, "poetry.lock"))

    def test_generated_directories_are_skipped_by_name(self):
        self.write("node_modules/pkg/index.js", "vendored\n")
        self.write("dist/bundle.js", "built\n")
        self.write("app.py", "two\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertEqual(
            detected.paths(),
            ["app.py"],
            "vendored and built files are not worth a reviewer's time",
        )
        skipped = self.skipped_names(detected)
        self.assertIn("node_modules/pkg/index.js", skipped)
        self.assertIn("dist/bundle.js", skipped)

    def test_is_excluded_matches_the_pattern_list(self):
        self.assertTrue(scope.is_excluded("bundle.min.js", scope.DEFAULT_EXCLUSIONS))
        self.assertTrue(scope.is_excluded("poetry.lock", scope.DEFAULT_EXCLUSIONS))
        self.assertTrue(
            scope.is_excluded("package-lock.json", scope.DEFAULT_EXCLUSIONS)
        )
        self.assertFalse(scope.is_excluded("src/app.py", scope.DEFAULT_EXCLUSIONS))

        self.assertTrue(scope.is_excluded("notes.txt", ("*.txt",)))
        self.assertFalse(scope.is_excluded("app.py", ("*.txt",)))
        self.assertTrue(scope.is_excluded("config/settings.yaml", ("config/*.yaml",)))
        self.assertFalse(scope.is_excluded("src/settings.yaml", ("config/*.yaml",)))

    def test_a_custom_exclusion_list_is_honoured(self):
        self.write("notes.txt", "scratch\n")
        self.write("app.py", "two\n")

        detected = scope.detect(self.root, mode="uncommitted", exclusions=("*.txt",))

        self.assertEqual(detected.paths(), ["app.py"])
        self.assertIn("notes.txt", self.skipped_names(detected))

    def test_binary_files_are_skipped_and_reported(self):
        (self.root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        (self.root / "blob.dat").write_bytes(b"\x00\x01\x02binary")
        self.write("app.py", "two\n")

        detected = scope.detect(self.root, mode="uncommitted")

        self.assertEqual(detected.paths(), ["app.py"], "a binary file is not reviewed")
        skipped = self.skipped_names(detected)
        self.assertIn("logo.png", skipped)
        self.assertIn("blob.dat", skipped)
        self.assertEqual(self.skip_reason(detected, "blob.dat"), "binary file")

    def test_the_file_limit_is_honoured_and_reported(self):
        self.write("one.py", "1\n")
        self.write("two.py", "2\n")
        self.write("three.py", "3\n")

        detected = scope.detect(self.root, mode="uncommitted", max_files=2)

        self.assertEqual(len(detected.files), 2, "no more files than the limit")
        self.assertTrue(detected.truncated, "the scope says it was cut short")
        self.assertEqual(len(detected.skipped), 1)
        self.assertIn(
            "over the file limit",
            self.skip_reason(detected, detected.skipped[0]["path"]),
        )


class ScopeArguments(unittest.TestCase):
    def test_valid_scope_accepts_the_four_scopes(self):
        for text in ("run", "uncommitted", "staged", "base:main", "base:HEAD~1", ""):
            self.assertTrue(
                scope.valid_scope(text), f"{text!r} names a scope the engine knows"
            )

    def test_valid_scope_rejects_junk(self):
        for text in ("junk", "base:", "against:", "everything", "runs"):
            self.assertFalse(
                scope.valid_scope(text), f"{text!r} must not pass as a scope"
            )

    def test_parse_scope_reads_the_scope_and_its_base(self):
        self.assertEqual(scope.parse_scope("run"), ("run", ""))
        self.assertEqual(scope.parse_scope("uncommitted"), ("uncommitted", ""))
        self.assertEqual(scope.parse_scope("staged"), ("staged", ""))
        self.assertEqual(scope.parse_scope("base:main"), ("base", "main"))
        self.assertEqual(scope.parse_scope("BASE:feature/x"), ("base", "feature/x"))
        self.assertEqual(scope.parse_scope(""), ("run", ""))

    def test_parse_scope_falls_back_to_run_for_junk(self):

        self.assertEqual(scope.parse_scope("junk"), ("run", ""))
        self.assertEqual(scope.parse_scope("base:"), ("run", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
