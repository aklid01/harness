"""The finding: what a reviewer returns, and what survives validation.

A reviewer is a model, so it is trusted the way a rumour is trusted: it is
parsed, checked and - where it does not hold up - thrown away. A finding has
to name a file that was actually reviewed and a line that actually appears in
that file's diff. Anything else is discarded before it can reach a user, an
agent or a report.

Nothing here raises on bad input except one deliberate case: parse() raises
ReviewFormatError when the reply carries no usable structure at all, so the
caller can retry once and then report the model as failed. A reviewer that
returns prose instead of JSON is a failed review, not a crashed engine.
"""

import dataclasses
import json
import re

CATEGORIES = (
    "correctness",
    "error-handling",
    "security",
    "performance",
    "concurrency",
    "quality",
    "maintainability",
    "tests",
    "dependencies",
    "documentation",
    "observability",
    "agent-experience",
    "other",
)

CATEGORY_LABELS = {
    "correctness": "Correctness",
    "error-handling": "Error handling",
    "security": "Security",
    "performance": "Performance",
    "concurrency": "Concurrency and resource safety",
    "quality": "Code quality and bad practices",
    "maintainability": "Maintainability and consistency",
    "tests": "Tests",
    "dependencies": "Dependencies and compatibility",
    "documentation": "Documentation and DX",
    "observability": "Observability",
    "agent-experience": "Agent-experience (AX) risks",
    "other": "Other",
}

SEVERITIES = ("blocker", "high", "medium", "low", "nit")

SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

CONFIDENCES = ("high", "medium", "low")

# A severity at or above this rank is worth showing; the settings slider picks
# one and everything below it is filtered out of the card.
DEFAULT_THRESHOLD = "low"


@dataclasses.dataclass
class Finding:
    category: str
    severity: str
    file: str
    line: int
    summary: str
    why: str = ""
    fix: str = ""
    confidence: str = "medium"
    end_line: int = 0
    id: str = ""
    models: tuple[str, ...] = ()
    conflict_with: tuple = ()
    raw_category: str = ""

    @property
    def rank(self):
        return SEVERITY_RANK.get(self.severity, SEVERITY_RANK["medium"])

    @property
    def location(self):
        """A stable key for "the same place in the same file"."""
        return (self.file, int(self.line))

    def to_dict(self):
        return {
            "id": self.id,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "severity": self.severity,
            "file": self.file,
            "line": int(self.line),
            "end_line": int(self.end_line or 0),
            "summary": self.summary,
            "why": self.why,
            "fix": self.fix,
            "confidence": self.confidence,
            "models": list(self.models),
            "conflict_with": list(self.conflict_with),
        }


class ReviewFormatError(Exception):
    """The reviewer returned something that is not a usable report."""


# --------------------------------------------------------------------------
# diff awareness: which lines a finding may legitimately cite
# --------------------------------------------------------------------------


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_lines(diff_text):
    """Every line number the diff touches, per file.

    A finding may cite a line the diff shows as context or as added - both are
    lines the reviewer actually saw - but not a line three hundred lines
    below the change. Deleted-only hunks keep their old-side numbers, since
    the new side has none.
    """
    lines = {}
    path = None
    for raw in (diff_text or "").split("\n"):
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None
            if target and target != "/dev/null":
                path = target[2:] if target.startswith("b/") else target
                lines.setdefault(path, set())
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("diff --git "):
            path = None
            continue
        match = _HUNK.match(raw)
        if not match:
            continue
        if path is None:
            continue
        old_start, old_count, new_start, new_count = match.groups()
        old_count = int(old_count or 1)
        new_count = int(new_count or 1)
        bucket = lines.setdefault(path, set())
        if new_count:
            start = int(new_start)
            bucket.update(range(start, start + new_count))
        if old_count:
            start = int(old_start)
            bucket.update(range(start, start + old_count))
    return lines


def _normalise_path(path):
    """A path a reviewer might quote, reduced to the workspace form."""
    if not path:
        return ""
    text = str(path).strip().strip("`\"'")
    text = text.replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    for prefix in ("a/", "b/"):
        if text.startswith(prefix):
            text = text[2:]
    return text


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value, limit=4000):
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def _balanced(body, start):
    """The JSON fragment that begins at ``start``, or None."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(body)):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return body[start : index + 1]
    return None


def _candidates(body):
    """The JSON fragments in a reply, most likely reading first.

    A reviewer may answer with the JSON on its own, in a fenced block, or
    buried in a sentence - and it may answer with an object or with a bare
    array of findings. Each shape gets a turn.
    """
    out = [body]
    stripped = body.lstrip()
    offset = len(body) - len(stripped)
    starts = []
    if stripped.startswith("["):
        starts.append(offset)
    brace = body.find("{")
    bracket = body.find("[")
    if brace != -1:
        starts.append(brace)
    if bracket != -1 and bracket not in starts:
        starts.append(bracket)
    for start in starts:
        fragment = _balanced(body, start)
        if fragment and fragment not in out:
            out.append(fragment)
    return out


def parse(text):
    """Parse a reviewer reply into raw finding dicts.

    Raises ReviewFormatError when the reply carries no usable structure, so
    the caller can retry once and then report the model as failed.
    """
    body = (text or "").strip()
    if not body:
        raise ReviewFormatError("the reviewer returned nothing")
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
        body = body.strip()

    problem = "the reply was not a usable review"
    for fragment in _candidates(body):
        try:
            data = json.loads(fragment)
        except ValueError:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            continue
        raw = data.get("findings")
        if raw is None:
            raw = data.get("issues")
        if raw is None:
            # A reply that only says "no issues found" is a valid clean
            # review.
            if _says_clean(body):
                return []
            problem = "the reply had no findings list"
            continue
        if not isinstance(raw, list):
            raise ReviewFormatError("the findings list was not a list")
        return [item for item in raw if isinstance(item, dict)]
    raise ReviewFormatError(problem)


_CLEAN_WORDS = ("no issues found", "no issues", "nothing to report", "no findings")


def _says_clean(body):
    lowered = body.lower()
    return any(word in lowered for word in _CLEAN_WORDS)


def validate(raw, allowed_files, known_lines):
    """Keep the findings that hold up, and say why the others did not.

    ``allowed_files`` is the set of files that were actually reviewed and
    ``known_lines`` maps each of them to the line numbers its diff shows.
    A finding that names another file, or a line that is not in the diff, is
    discarded: a reviewer cannot know about code it was never shown.
    """
    kept, rejected = [], []
    files = {_normalise_path(path) for path in allowed_files}
    for item in raw:
        category = _as_text(item.get("category"), 60).lower().replace("_", "-")
        raw_category = category
        if category not in CATEGORIES:
            category = "other"
        severity = _as_text(item.get("severity"), 20).lower()
        if severity not in SEVERITIES:
            severity = "medium"
        confidence = _as_text(item.get("confidence"), 20).lower()
        if confidence not in CONFIDENCES:
            confidence = "medium"
        path = _normalise_path(item.get("file") or item.get("path"))
        if not path or path not in files:
            rejected.append({"file": path, "reason": "file was not reviewed"})
            continue
        line = _as_int(item.get("line") or item.get("line_number"), 0)
        if line <= 0:
            rejected.append({"file": path, "reason": "no line number"})
            continue
        allowed = known_lines.get(path, set())
        if allowed and line not in allowed:
            rejected.append({"file": path, "line": line, "reason": "line is not in the diff"})
            continue
        end_line = _as_int(item.get("end_line") or item.get("end"), 0)
        if end_line and end_line < line:
            end_line = 0
        summary = _as_text(item.get("summary") or item.get("title"), 400)
        if not summary:
            rejected.append({"file": path, "line": line, "reason": "no summary"})
            continue
        kept.append(
            Finding(
                category=category,
                raw_category=raw_category,
                severity=severity,
                file=path,
                line=line,
                end_line=end_line,
                summary=summary,
                why=_as_text(item.get("why") or item.get("explanation"), 2000),
                fix=_as_text(item.get("fix") or item.get("suggestion"), 4000),
                confidence=confidence,
            )
        )
    return kept, rejected


def sort_key(finding):
    """Severity first, then agreement, then position, then title."""
    return (
        finding.rank,
        -len(finding.models),
        finding.file,
        finding.line,
        finding.summary.lower(),
    )
