"""The command: the arguments of /multi-model-review, and the CLI.

Two front doors lead into the same review: the slash command inside a
session, and `python -m harness.review` for scripts and CI. Both are tested
here, because both make promises a user relies on - that a typo is an error
rather than a wider review, and that the exit code is the verdict.
"""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from harness import commands, events
from harness import paths as paths_mod
from harness.review import command
from harness.review.__main__ import main

from tests.review_support import FakeLLM, ReviewCase, commit_all, init_repo

A_FINDING = (
    '{"findings": [{"category": "correctness", "severity": "high", '
    '"file": "app.py", "line": 1, "summary": "the value is wrong", '
    '"why": "it writes the old value", "fix": "use the new one", '
    '"confidence": "high"}]}'
)

A_NIT = (
    '{"findings": [{"category": "quality", "severity": "medium", '
    '"file": "app.py", "line": 1, "summary": "the name is unclear"}]}'
)


class Arguments(unittest.TestCase):
    def test_no_arguments_means_the_run_scope(self):
        args = command.parse_args("")

        self.assertEqual(args["scope"], "run")
        self.assertEqual(args["base"], "")
        self.assertEqual(args["models"], [])
        self.assertEqual(args["focus"], "")
        self.assertEqual(args["severity"], "")
        self.assertFalse(args["force"])
        self.assertFalse(args["json"])
        self.assertFalse(args["help"])
        self.assertEqual(args["error"], "")

    def test_the_scope_is_read(self):
        self.assertEqual(command.parse_args("uncommitted")["scope"], "uncommitted")
        self.assertEqual(command.parse_args("staged")["scope"], "staged")

    def test_a_base_scope_carries_its_ref(self):
        args = command.parse_args("base:main")

        self.assertEqual(args["scope"], "base")
        self.assertEqual(args["base"], "main")

    def test_the_models_are_read_as_a_list(self):
        args = command.parse_args("--models a,b")

        self.assertEqual(args["models"], ["a", "b"])

    def test_the_focus_is_read(self):
        self.assertEqual(command.parse_args("--focus security")["focus"], "security")

    def test_the_severity_is_read(self):
        self.assertEqual(command.parse_args("--severity high")["severity"], "high")

    def test_json_and_force_are_flags(self):
        args = command.parse_args("--json --force")

        self.assertTrue(args["json"])
        self.assertTrue(args["force"])

    def test_help_is_asked_for_and_nothing_else_is_parsed(self):
        args = command.parse_args("--help")

        self.assertTrue(args["help"])
        self.assertEqual(args["error"], "")

    def test_an_unknown_option_is_an_error(self):
        args = command.parse_args("--wat")

        self.assertTrue(args["error"], "a typo must not be ignored")
        self.assertIn("--wat", args["error"])

    def test_an_unknown_scope_is_an_error(self):
        args = command.parse_args("everything")

        self.assertTrue(args["error"], "a typo must not widen the review")
        self.assertIn("everything", args["error"])

    def test_an_unknown_severity_is_an_error(self):
        args = command.parse_args("--severity urgent")

        self.assertTrue(args["error"])
        self.assertIn("urgent", args["error"])

    def test_an_option_without_a_value_is_an_error(self):
        args = command.parse_args("--focus")

        self.assertTrue(args["error"])
        self.assertIn("needs a value", args["error"])


class FakeContext:
    """The desktop side of the command: it asks for a review, it does not run it."""

    def __init__(self):
        self.calls = []

    def request_review(self, args):
        self.calls.append(args)


class Wired(unittest.TestCase):
    def setUp(self):
        events.reset()
        self.addCleanup(events.reset)
        self.notes = []
        events.subscribe(lambda kind, payload: self.notes.append((kind, payload)))
        self.ctx = FakeContext()
        self.messages = [{"role": "user", "content": "review this"}]

    def test_the_command_asks_the_session_for_a_review(self):
        commands.handle("/multi-model-review", self.messages, self.ctx)

        self.assertEqual(len(self.ctx.calls), 1, "the session was asked to review")
        self.assertEqual(self.ctx.calls[0]["scope"], "run")
        self.assertEqual(self.ctx.calls[0]["error"], "")

    def test_the_command_passes_the_arguments_on(self):
        commands.handle(
            "/multi-model-review uncommitted --models a,b --severity high",
            self.messages,
            self.ctx,
        )

        args = self.ctx.calls[0]
        self.assertEqual(args["scope"], "uncommitted")
        self.assertEqual(args["models"], ["a", "b"])
        self.assertEqual(args["severity"], "high")

    def test_the_command_without_a_session_says_so(self):
        commands.handle("/multi-model-review", self.messages, None)

        kinds = [kind for kind, _ in self.notes]
        self.assertIn(events.NOTE, kinds, "the user is told why nothing happened")

    def test_help_emits_a_note_with_the_help_text(self):
        commands.handle("/multi-model-review --help", self.messages, self.ctx)

        notes = [payload["text"] for kind, payload in self.notes if kind == events.NOTE]
        self.assertTrue(notes, "the help is shown")
        self.assertIn("/multi-model-review", notes[0])
        self.assertIn("--severity", notes[0])
        self.assertEqual(self.ctx.calls, [], "help does not start a review")

    def test_a_bad_argument_emits_a_note_and_starts_nothing(self):
        commands.handle("/multi-model-review --wat", self.messages, self.ctx)

        notes = [payload["text"] for kind, payload in self.notes if kind == events.NOTE]
        self.assertTrue(notes, "the error is shown")
        self.assertIn("--wat", notes[0])
        self.assertEqual(self.ctx.calls, [], "a bad argument starts no review")


class Cli(ReviewCase):
    def setUp(self):
        super().setUp()
        init_repo(self.root)
        self.write("app.py", "value = 1\n")
        commit_all(self.root, "first")
        self.write("app.py", "value = 2\n")
        # The CLI resolves its own paths, and the home directory is where it
        # looks for the provider registry. Point it at the temp dir so the
        # machine's own settings are never read or written.
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def run_cli(self, reply=A_FINDING, extra=()):
        fake = FakeLLM(replies={"mdl-a": reply})
        output = io.StringIO()
        with mock.patch.object(Path, "home", classmethod(lambda cls: self.home)):
            paths_mod.init(project_root=self.root, app_data_dir=self.home / ".agents")
            self.register_models("mdl-a")
            with mock.patch("harness.review.engine.call_llm", fake):
                with contextlib.redirect_stdout(output):
                    with contextlib.redirect_stderr(io.StringIO()):
                        code = main(
                            [
                                "--root",
                                str(self.root),
                                "--scope",
                                "uncommitted",
                                "--models",
                                "mdl-a",
                                *extra,
                            ]
                        )
        return code, output.getvalue(), fake

    def test_changes_requested_exits_with_one(self):
        code, output, fake = self.run_cli(A_FINDING)

        self.assertEqual(code, 1, "a review that asks for changes fails the script")
        self.assertIn("the value is wrong", output, "the finding is printed")
        self.assertEqual(len(fake.calls), 1, "the model was asked exactly once")

    def test_a_clean_review_exits_with_zero(self):
        code, output, _ = self.run_cli('{"findings": [], "note": "no issues found"}')

        self.assertEqual(code, 0, "a clean review passes the script")
        self.assertIn("review: clean", output)

    def test_suggestions_exit_with_zero(self):
        code, output, _ = self.run_cli(A_NIT)

        self.assertEqual(code, 0, "suggestions do not fail the script")
        self.assertIn("review: suggestions", output)

    def test_the_json_output_is_the_report(self):
        import json

        code, output, _ = self.run_cli(A_FINDING, extra=["--json"])

        document = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(document["verdict"], "changes_requested")
        self.assertEqual(len(document["findings"]), 1)

    def test_nothing_to_review_has_its_own_exit_code(self):
        self.write("app.py", "value = 1\n")
        code, _, fake = self.run_cli(A_FINDING)

        self.assertEqual(code, 3, "nothing to review is told apart from a failure")
        self.assertEqual(len(fake.calls), 0, "and it costs nothing")

    def test_a_review_without_a_reviewer_is_told_so(self):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(Path, "home", classmethod(lambda cls: self.home)):
            paths_mod.init(project_root=self.root, app_data_dir=self.home / ".agents")
            with contextlib.redirect_stdout(output):
                with contextlib.redirect_stderr(errors):
                    code = main(["--root", str(self.root), "--scope", "uncommitted"])

        self.assertEqual(code, 2, "no reviewer configured is its own exit code")
        self.assertIn("no reviewer model is configured", errors.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
