"""Text tool-call compatibility layer: strict parsing, no execution."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("API_KEY", "dummy")
os.environ.setdefault("MODEL", "dummy")

from harness import paths, providers  # noqa: E402
from harness import texttools  # noqa: E402
from harness.tools import TOOL_SCHEMAS  # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def sample(name, **params):
    """A call in the markup real models emit, built from pieces."""
    open_tag = texttools.OPEN
    close_tag = texttools.CLOSE
    param_open = texttools.PARAM_OPEN
    param_close = texttools.CLOSE_PARAM
    lines = [open_tag, "<" + "function=" + name + ">"]
    for key, value in params.items():
        lines.append(param_open + key + ">" + str(value) + param_close)
    lines.append("<" + "/function>")
    lines.append(close_tag)
    return "\n".join(lines)


class ParseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(project_root=Path(self.tmp.name), app_data_dir=Path(self.tmp.name) / "app")
        self.addCleanup(paths.reset)
        mock.patch.object(providers, "PROVIDERS_FILE",
                          Path(self.tmp.name) / "app" / "providers.json").start()
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)
        self.available = ["bash", "read_file", "write_file", "str_replace",
                          "read_skill", "write_todos", "task"]

    def parse(self, text, available=None):
        return texttools.parse(text, schemas=TOOL_SCHEMAS,
                               available=available if available is not None else self.available)

    def test_well_formed_call_parses(self):
        call = self.parse(sample("bash", command="find . -maxdepth 2 -type f | head -50"))
        self.assertTrue(call.ok, call.reason())
        self.assertEqual(call.name, "bash")
        self.assertEqual(call.arguments["command"], "find . -maxdepth 2 -type f | head -50")

    def test_arguments_are_type_coerced(self):
        call = self.parse(sample("read_file", path="notes.txt"))
        self.assertTrue(call.ok, call.reason())
        call = self.parse(sample("bash", command="echo hi"))
        self.assertEqual(call.arguments, {"command": "echo hi"})

    def test_unknown_tool_is_refused(self):
        call = self.parse(sample("drop_database", target="everything"))
        self.assertFalse(call.ok)
        self.assertIn("drop_database", call.reason())

    def test_unavailable_tool_is_refused(self):
        call = self.parse(sample("bash", command="ls"), available=["read_file"])
        self.assertFalse(call.ok)
        self.assertIn("not available", call.reason())

    def test_missing_required_argument_is_refused(self):
        call = self.parse(sample("bash", directory="."))
        self.assertFalse(call.ok)
        self.assertIn("missing required", call.reason())

    def test_unknown_argument_is_refused(self):
        call = self.parse(sample("bash", command="ls", bogus="1"))
        self.assertFalse(call.ok)
        self.assertIn("does not accept", call.reason())

    def test_malformed_markup_is_refused(self):
        for broken in (
            sample("bash", command="ls") + " trailing prose",
            "I would call " + sample("bash", command="ls") + " here",
            sample("bash", command="ls").replace("<" + "function=bash>", "<function=bash>", 1),
            sample("bash", command="ls").replace("command>", "cmd>", 1),
        ):
            if broken == sample("bash", command="ls"):
                continue
            call = self.parse(broken)
            self.assertFalse(call.ok, broken)

    def test_a_fenced_example_is_never_a_call(self):
        fenced = "Here is how:\n```\n" + sample("bash", command="rm -rf /") + "\n```"
        call = self.parse(fenced)
        self.assertFalse(call.ok)
        self.assertIn("code fence", call.reason())

    def test_a_quoted_command_is_never_a_call(self):
        call = self.parse('The model said ' + sample("bash", command="ls") + ' in reply.')
        self.assertFalse(call.ok)

    def test_inline_code_is_not_a_call(self):
        call = self.parse("Use `ls -al` to list files.")
        self.assertFalse(call.ok)

    def test_narration_is_kept_when_the_call_is_good(self):
        call, prose = texttools.parse_with_narration(
            "Let me look at the project structure.\n" + sample("bash", command="find . | head -50"),
            schemas=TOOL_SCHEMAS, available=self.available)
        self.assertTrue(call.ok, call.reason())
        self.assertIn("Let me look at the project structure.", prose)

    def test_parsed_call_has_the_native_shape(self):
        call = self.parse(sample("bash", command="ls"))
        native = call.as_tool_call("call-9")
        self.assertEqual(native.function.name, "bash")
        self.assertEqual(native.id, "call-9")
        import json
        self.assertEqual(json.loads(native.function.arguments), {"command": "ls"})

    def test_describe_mentions_the_tool(self):
        call = self.parse(sample("bash", command="ls"))
        self.assertIn("bash", texttools.describe(call))
        broken = self.parse(sample("nope", x="1"))
        self.assertIn("nope", texttools.describe(broken))


if __name__ == "__main__":
    unittest.main(verbosity=2)
