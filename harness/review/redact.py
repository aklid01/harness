"""Secret redaction for anything a reviewer is about to see.

A diff is about to leave the machine for a model the user picked for *reviewing*,
on a provider the user may never have audited. A credential that leaked into a
changed file must not ride along with it.

Two rules shape this module:

* Line counts never change. A reviewer that cites "line 42" must still be
  pointing at line 42 after redaction, so a redaction replaces text in place
  and never adds or removes a newline.
* Nothing about the secret is reported. The user is told that a redaction
  happened and roughly where; the value itself is never echoed back into a
  report, a log or an event.
"""

import re

REDACTED = "[REDACTED]"

# (name, pattern). Order matters: the first match wins, so the specific
# key shapes come before the generic assignment rule.
_PATTERNS = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
    ),
    (
        "url-credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)"),
    ),
    (
        "assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token"
            r"|client[_-]?secret|private[_-]?token|password|passwd|pwd|secret"
            r"|token)\b(\s*[:=]\s*)[\"']?([^\s\"'#,;]{10,})"
        ),
    ),
)

# A value that looks like plain code - a bare identifier or a dotted path -
# is a variable, not a leaked credential. Redacting `response.token` would
# cost the reviewer the very line it was asked to judge.
_CODE_VALUE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")

def _redact_value(name, match):
    """The replacement text for one match, keeping the surrounding text."""
    if name == "bearer":
        return f"{match.group(1)}{REDACTED}"
    if name == "url-credentials":
        return f"{match.group(1)}{REDACTED}{match.group(3)}"
    if name == "private-key":
        # Keep the line count: one [REDACTED] per line of the block.
        lines = match.group(0).split("\n")
        return "\n".join(REDACTED for _ in lines)
    return REDACTED


def redact(text):
    """Return (redacted_text, hits). ``hits`` is a list of pattern names.

    A secret that spans lines keeps its line count, so line numbers in the
    redacted diff still match the real file.
    """
    if not text:
        return text or "", []
    hits = []
    out = text
    for name, pattern in _PATTERNS:
        if name == "assignment":
            out = _redact_assignments(out, name, pattern, hits)
            continue

        def _swap(match, _name=name):
            replacement = _redact_value(_name, match)
            if replacement is None:
                return match.group(0)
            hits.append(_name)
            return replacement

        out = pattern.sub(_swap, out)
    return out, hits


def _redact_assignments(text, name, pattern, hits):
    def _swap(match):
        key, sep, value = match.group(1), match.group(2), match.group(3)
        if _CODE_VALUE.match(value):
            return match.group(0)
        hits.append(name)
        return f"{key}{sep}{REDACTED}"

    return pattern.sub(_swap, text)


def redact_lines(lines):
    """Redact a list of lines, returning (lines, hits).

    Line-by-line redaction keeps the line count exact, which is what the
    finding validator depends on when it checks a cited line against the
    diff.
    """
    out, hits = [], []
    for line in lines:
        clean, found = redact(line)
        out.append(clean)
        hits.extend(found)
    return out, hits


def redact_diff(diff):
    """Redact a unified diff, returning (diff, hits).

    Only the added/context lines carry payload, but the whole diff is
    redacted so a secret that leaked into a removed line is caught too.
    """
    out, hits = [], []
    for line in diff.split("\n"):
        clean, found = redact(line)
        out.append(clean)
        hits.extend(found)
    return "\n".join(out), hits
