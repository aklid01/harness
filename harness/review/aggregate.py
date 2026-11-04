"""Merging several reviewers into one report.

The reviewers ran independently, so the same problem arrives described
several times. Merging is therefore two jobs at once: collapse the
duplicates, and keep the disagreements visible. A finding two models agree on
is worth more than one model alone; a finding two models disagree about is
worth reading twice, not throwing away.

The rules, in one place:

* Two findings are the same when they name the same file, the same category
  and a line within five of each other.
* Two findings that name the same file and nearly the same line but a
  *different* category are a disagreement. Both are kept, and each is
  labelled with the other.
* The merged finding keeps the clearest wording of the group and the list of
  models that raised it.
"""

import dataclasses
import re

from . import findings as findings_mod

VERDICTS = ("clean", "suggestions", "changes_requested")

VERDICT_LABELS = {
    "clean": "clean",
    "suggestions": "suggestions",
    "changes_requested": "changes requested",
}

SAME_LINE = 5
CONFLICT_LINE = 3


@dataclasses.dataclass
class ModelOutcome:
    """What one reviewer did, and what it found."""

    model_id: str
    label: str = ""
    status: str = "ran"  # ran / skipped / failed / cancelled
    error: str = ""
    note: str = ""
    findings: list = dataclasses.field(default_factory=list)
    rejected: list = dataclasses.field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    @property
    def tokens(self):
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def to_dict(self):
        return {
            "model_id": self.model_id,
            "label": self.label or self.model_id,
            "status": self.status,
            "error": self.error,
            "note": self.note,
            "finding_count": len(self.findings),
            "rejected_count": len(self.rejected),
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "tokens": self.tokens,
            "seconds": round(float(self.seconds), 2),
        }


def _key(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _clearer(first, second):
    """Keep the better-written of two findings describing the same thing."""
    if first.confidence == "low" and second.confidence != "low":
        return second
    if len(second.why) > len(first.why) and len(first.why) < 40:
        return second
    if len(second.fix) > len(first.fix) and len(first.fix) < 40:
        return second
    return first


def merge(outcomes, threshold="low"):
    """Merge the outcomes into one report.

    Returns a dict with the verdict, the merged findings, the counts and the
    per-model status. Never raises: a broken outcome is reported as failed.
    """
    groups = []
    for outcome in outcomes:
        if outcome.status != "ran":
            continue
        for finding in outcome.findings:
            placed = False
            for group in groups:
                existing = group["finding"]
                if existing.file != finding.file:
                    continue
                if existing.category != finding.category:
                    continue
                if abs(existing.line - finding.line) > SAME_LINE:
                    continue
                group["finding"] = _clearer(existing, finding)
                group["models"].add(outcome.model_id)
                placed = True
                break
            if not placed:
                groups.append(
                    {
                        "finding": finding,
                        "models": {outcome.model_id},
                        "conflict_with": set(),
                    }
                )

    merged = []
    for group in groups:
        finding = group["finding"]
        finding.models = tuple(sorted(group["models"]))
        merged.append(finding)

    limit = findings_mod.SEVERITY_RANK.get(threshold, findings_mod.SEVERITY_RANK["low"])
    shown = [f for f in merged if f.rank <= limit]
    suppressed = [f for f in merged if f.rank > limit]

    shown.sort(key=findings_mod.sort_key)
    for index, finding in enumerate(shown, start=1):
        finding.id = f"R{index}"
    for index, finding in enumerate(suppressed, start=len(shown) + 1):
        finding.id = f"R{index}"

    # A disagreement is two findings that point at the same place but read it
    # differently. Both stay; each is labelled with the other.
    for index, finding in enumerate(shown):
        for other in shown[index + 1 :]:
            if finding.file != other.file or finding.category == other.category:
                continue
            if abs(finding.line - other.line) > CONFLICT_LINE:
                continue
            finding.conflict_with = tuple(sorted(set(finding.conflict_with) | {other.id}))
            other.conflict_with = tuple(sorted(set(other.conflict_with) | {finding.id}))

    by_severity = {name: 0 for name in findings_mod.SEVERITIES}
    by_category = {name: 0 for name in findings_mod.CATEGORIES}
    for finding in shown:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        by_category[finding.category] = by_category.get(finding.category, 0) + 1

    verdict = "clean"
    for finding in shown:
        if finding.severity in ("blocker", "high"):
            verdict = "changes_requested"
            break
    if verdict == "clean" and shown:
        verdict = "suggestions"

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        "findings": shown,
        "suppressed": suppressed,
        "by_severity": by_severity,
        "by_category": by_category,
        "models": [outcome.to_dict() for outcome in outcomes],
        "threshold": threshold,
    }


def agreement_label(models):
    """How many reviewers raised this, in words a user can read."""
    count = len(models)
    if count <= 1:
        return "one reviewer"
    if count == 2:
        return "two reviewers agree"
    return f"{count} reviewers agree"
