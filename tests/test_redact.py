"""Redaction: every pattern catches its own shape, line counts never move,
and a code-like value is never redacted as if it were a secret.

The fake credentials here are assembled from pieces at runtime: a literal
secret-shaped string in this file would be scrubbed by the same tooling that
scrubs real leaks.
"""

import unittest

from harness.review import redact

REDACTED = "[REDACTED]"
ANTHROPIC = "sk-ant-" + "a" * 24
OPENAI = "sk-" + "o" * 24
GITHUB_PAT = "github_pat_" + "g" * 24
GITHUB_TOKEN = "ghp_" + "h" * 30
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GOOGLE = "AIza" + "A1" + "b" * 33
SLACK = "xoxb-" + "s" * 12
JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
BEARER_TOKEN = "b" * 24
URL_SECRET = "u" * 20
ASSIGN_SECRET = "K9" + "zX" * 9  # mixed case+digits: clearly not an identifier


class TokenShapes(unittest.TestCase):
    def assert_hit(self, text, name):
        out, hits = redact.redact(text)
        self.assertIn(name, hits, text)
        self.assertNotEqual(out, text)
        self.assertEqual(out.count("\n"), text.count("\n"), "line counts never move")
        return out

    def test_private_key_block(self):
        block = "\n".join(
            [
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                "b3BlbnNzaC1rZXk=",
                "-----END OPENSSH PRIVATE KEY-----",
            ]
        )
        out = self.assert_hit(block, "private-key")
        self.assertEqual(out.count(REDACTED), 3, "one per line of the block")

    def test_anthropic_key(self):
        self.assert_hit(f"key: {ANTHROPIC}", "anthropic-key")

    def test_openai_key(self):
        self.assert_hit(f"key: {OPENAI}", "openai-key")

    def test_github_pat(self):
        self.assert_hit(f"token: {GITHUB_PAT}", "github-pat")

    def test_github_token(self):
        self.assert_hit(f"token: {GITHUB_TOKEN}", "github-token")

    def test_aws_key(self):
        self.assert_hit(f"aws_access_key_id = {AWS}", "aws-key")

    def test_google_key(self):
        self.assert_hit(f"key: {GOOGLE}", "google-key")

    def test_slack_token(self):
        self.assert_hit(f"token: {SLACK}", "slack-token")

    def test_jwt(self):
        self.assert_hit(f"Authorization: Bearer {JWT}", "jwt")

    def test_bearer(self):
        out = self.assert_hit(f"Authorization: Bearer {BEARER_TOKEN}", "bearer")
        self.assertIn(REDACTED, out)
        self.assertNotIn(BEARER_TOKEN, out)

    def test_url_credentials(self):
        out = self.assert_hit(
            f"postgres://admin:{URL_SECRET}@db.example.com/db", "url-credentials"
        )
        self.assertIn("admin:", out)
        self.assertIn("@db.example.com", out)
        self.assertNotIn(URL_SECRET, out)

    def test_assignment_shapes(self):
        for key in ("API_KEY", "password", "auth-token", "client_secret", "secret"):
            value = ASSIGN_SECRET
            text = f"{key} = {value}"
            out, hits = redact.redact(text)
            self.assertEqual(hits, ["assignment"], text)
            self.assertIn(REDACTED, out, text)
            self.assertNotIn(value, out, text)

    def test_the_secret_value_never_survives(self):
        text = f"password = {ASSIGN_SECRET}"
        out, _ = redact.redact(text)
        self.assertNotIn(ASSIGN_SECRET, out)

    def test_a_code_like_value_is_left_alone(self):
        text = "token = response.auth_token"
        out, hits = redact.redact(text)
        self.assertEqual(hits, [])
        self.assertEqual(out, text)

    def test_no_redaction_no_hits(self):
        out, hits = redact.redact("print('nothing secret here')\nplain = value")
        self.assertEqual(hits, [])
        self.assertNotIn(REDACTED, out)

    def test_redact_lines_and_diff_keep_their_shape(self):
        lines = [f"+API_KEY = {ASSIGN_SECRET}", "+print('ok')"]
        out, hits = redact.redact_lines(lines)
        self.assertEqual(len(out), len(lines))
        self.assertIn("+", out[0])
        self.assertIn(REDACTED, out[0])
        self.assertEqual(hits, ["assignment"])

        diff = "\n".join(lines)
        text, hits = redact.redact_diff(diff)
        self.assertEqual(text.count("\n"), diff.count("\n"))
        self.assertEqual(hits, ["assignment"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
