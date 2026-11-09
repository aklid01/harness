"""Decision oversight: a bounded reviewer for the coding agent's decisions.

A second, usually stronger model that judges a small number of significant
decisions the coding agent is about to make - not every token, and never in
parallel with it. It is a reviewer with authority to challenge and correct,
not a copy that repeats the coding agent's work, and it holds no tools: it
cannot edit, run, or bypass anything.

The whole thing is one bounded function, ``review``. It assembles only the
evidence a judgment needs (the task, the plan, the project rules, the proposed
action and the coding agent's reason, plus the prior rounds when this decision
is being challenged), asks the reviewer once, and returns a structured verdict.
Retries, timeouts and rate limits are already bounded inside call_llm; a
failure here comes back as an error verdict so the caller applies its risk
policy instead of deadlocking. The coding agent gets to push back: when the
same decision is proposed again, the prior verdicts travel with it, so the
reviewer must answer the counterargument with evidence rather than repeat
itself. The number of rounds is capped by the caller, and an unresolved
disagreement escalates to the user rather than looping.
"""

import dataclasses
import json

from . import instructions, providers
from . import shellrisk as shellrisk_mod
from .llm import call_llm
from .toolsets import REVIEWABLE

MODEL_VERDICTS = ("approve", "revise", "clarify", "escalate")

VERDICTS = MODEL_VERDICTS + ("error",)

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


SMALL_EDIT_CHARS = 2000


_MIN_RISK_BY_INTENSITY = {"low": "high", "medium": "medium", "high": "low"}


def risk_ge(level, floor):
    return RISK_ORDER.get(level, 0) >= RISK_ORDER.get(floor, 0)


def command_risk(command):
    """Risk of a shell command, from the shared quote-aware classifier.

    Kept as a thin wrapper so permission risk and review eligibility answer the
    same question the same way: a read-only `cat`, a stderr `2>/dev/null`, and a
    plain `ls` of several directories are low and are not reviewed; editing,
    deleting, installing or piping into a shell rises with real impact.
    """
    return shellrisk_mod.classify_bash(command)[0]


def decision_risk(name, args, touched=()):
    """How significant this *specific* call is as a decision, or None to skip.

    ``touched`` holds the paths this session has already edited. Editing
    source is what this feature exists to check, so a first touch, a new file
    or a large rewrite is always high; a small change to a file the session
    is already working in is follow-up work and grades medium.
    """
    if name not in REVIEWABLE:
        return None
    if name in ("write_file", "str_replace"):
        if str(args.get("path") or "") in touched and _within_small_edit_budget(
            name, args
        ):
            return "medium"
        return "high"
    if name == "bash":
        return command_risk(args.get("command", ""))
    return None


def _within_small_edit_budget(name, args):
    if name == "str_replace":
        size = len(str(args.get("old_str") or "")) + len(str(args.get("new_str") or ""))
    else:
        size = len(str(args.get("content") or ""))
    return size <= SMALL_EDIT_CHARS


def is_small_edit(name, args):
    """Would this call count as a small follow-up? The per-file review-once
    rule lets these ride on an earlier verdict; anything bigger re-reviews."""
    return name in ("write_file", "str_replace") and _within_small_edit_budget(
        name, args
    )


def min_review_risk(intensity):
    return _MIN_RISK_BY_INTENSITY.get(intensity, "medium")


def wants_review(name, args, intensity, touched=()):
    """Should this call be sent to the reviewer at this intensity?"""
    risk = decision_risk(name, args, touched)
    return risk is not None and risk_ge(risk, min_review_risk(intensity))


def block_on_error(name, args):
    """When the reviewer itself fails, is this action too hot to proceed sight
    unseen? Only destructive/config shell is; edits and installs continue under
    the normal permission and approval layer, which is still in force."""
    if name == "bash":
        return risk_ge(command_risk(args.get("command", "")), "high")
    return False


CHECKER_SYSTEM = """You are a critical technical reviewer inside a coding agent. A
coding agent is about to take one significant action, and you judge it BEFORE it
happens. You do not write code, edit files, or run anything. You only review and
answer with a verdict.

Judge the proposed action on:
- correctness: will it actually do what is intended, given the evidence?
- necessity: is this approach sound and the simplest reasonable one?
- safety: data loss, security, permission or destructive side effects.
- compatibility: does it fit the existing code and the project's rules?
- regressions: what else could it break?

How to reason:
- Use only the evidence you are given. Do not invent files, behaviour, or
  repository details you were not shown. If the evidence is not enough, say so
  instead of guessing.
- Separate what is verified from what you are assuming, and name assumptions.
- Be concrete. A correction must be something actionable.
- Be concise. Acknowledge uncertainty where it exists.

Answer with a single JSON object and nothing else:
{"verdict":"approve|revise|clarify|escalate",
 "reasoning":"one or two sentences",
 "risks":["short risk"],
 "correction":"the concrete change; only when verdict is revise",
 "question":"the one thing you need; only when verdict is clarify"}

verdict meanings:
- approve: the action is reasonable; let it proceed.
- revise: the approach is flawed or unsafe; give the specific correction.
- clarify: the evidence is insufficient; ask one precise question.
- escalate: there is a material disagreement, unresolved risk, or a choice the
  user must make."""


@dataclasses.dataclass
class Verdict:
    verdict: str
    reasoning: str = ""
    risks: list[str] = dataclasses.field(default_factory=list)
    correction: str = ""
    question: str = ""
    error: str = ""


def _text(value, limit):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def _render_action(name, args):
    if name == "write_file":
        return (
            f"write_file(path={args.get('path')})\n"
            f"--- new content ---\n{_text(args.get('content'), 1600)}"
        )
    if name == "str_replace":
        return (
            f"str_replace(path={args.get('path')})\n"
            f"--- replace this ---\n{_text(args.get('old_str'), 800)}\n"
            f"--- with this ---\n{_text(args.get('new_str'), 800)}"
        )
    if name == "bash":
        return f"bash(command)\n{_text(args.get('command'), 1200)}"
    return f"{name}({_text(json.dumps(args, ensure_ascii=False), 800)})"


def _fits(budget_chars, *blocks):
    """Assemble the prompt, dropping the optional context blocks first if it
    would exceed the reviewer's context window. Task is always kept."""
    header = [
        ("## Task the user gave\n", blocks[0]),
        ("## Plan the planner produced\n", blocks[1]),
        ("## Project rules (AGENTS.md)\n", blocks[2]),
        ("## Instructions given for this reviewer\n", blocks[3]),
    ]
    kept = header
    while sum(len(h) + len(b) for h, b in kept) > budget_chars and len(kept) > 1:
        kept = kept[:-1]
    return kept


def _build_prompt(task, plan, action_text, note, reviewer_instructions, history):
    chars_budget = 4000
    rules = _text(instructions.load(), 3000)
    context_blocks = _fits(
        chars_budget,
        _text(task, 2200),
        _text(plan, 2200),
        rules,
        _text(reviewer_instructions, 2000),
    )

    parts = []
    for heading, body in context_blocks:
        if body:
            parts.append(heading + body)
    parts.append("## Proposed action (not yet executed)\n" + action_text)
    if note:
        parts.append("## The coding agent's stated reason\n" + _text(note, 1600))
    if history:
        rows = []
        for i, h in enumerate(history):
            line = f"- Round {i + 1}: {h.get('verdict', '?')}"
            if h.get("correction"):
                line += f" - correction: {h['correction']}"
            rows.append(line)
        parts.append(
            "## Prior review rounds on this same decision\n"
            + "\n".join(rows)
            + "\n\nThe coding agent has answered those rounds with the action and "
            "reason above. It may be applying a correction or challenging one "
            "with its own reasoning. Re-judge on the merits: address its "
            "counterargument with evidence, and do not repeat an earlier verdict "
            "if the new reasoning answers it. If it has genuinely resolved the "
            "concern, you may now approve."
        )
    parts.append("Return your JSON verdict now.")
    return "\n\n".join(parts)


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text[:4].lower() == "json":
            text = text[4:]
    return text.strip()


def parse_verdict(text):
    """Pull a Verdict out of the reviewer's answer. Never raises."""
    raw = _strip_fences(str(text or ""))
    data = None
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None
    if data is None:
        return Verdict(
            "clarify",
            reasoning="The reviewer's answer could not be read as a "
            "verdict, so the action needs a human look.",
            error="unparseable",
        )
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in MODEL_VERDICTS:
        return Verdict(
            "clarify",
            reasoning=str(
                data.get("reasoning") or "The verdict was not one we recognise."
            ),
            error="unknown-verdict",
        )
    risks = data.get("risks")
    if not isinstance(risks, list):
        risks = [str(risks)] if risks else []
    return Verdict(
        verdict=verdict,
        reasoning=str(data.get("reasoning") or ""),
        risks=[str(r) for r in risks],
        correction=str(data.get("correction") or ""),
        question=str(data.get("question") or ""),
    )


def review(
    checker_model_id,
    reviewer_instructions,
    *,
    task,
    plan,
    name,
    args,
    note="",
    history=None,
    cancel=None,
):
    """Ask the reviewer for one verdict. Returns (Verdict, usage_dict).

    The reviewer runs on its own model, with no tools, bounded by call_llm's
    retry window. Any failure - timeout, rate limit, provider error - comes
    back as an error verdict rather than an exception, so the caller can apply
    a risk-based policy and never deadlock.
    """
    zero = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": None,
        "cached_tokens": None,
    }
    if not checker_model_id:
        return Verdict("error", error="no reviewer model configured"), zero
    prompt = _build_prompt(
        task,
        plan,
        _render_action(name, args),
        note,
        reviewer_instructions,
        history or [],
    )
    messages = [
        {"role": "system", "content": CHECKER_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    try:
        with providers.use(checker_model_id):
            message, usage = call_llm(messages, tools=[], cancel=cancel)
    except Exception as failure:  # noqa: BLE001
        return Verdict("error", error=f"{type(failure).__name__}: {failure}"), zero
    return parse_verdict(message.content), dict(usage)


def fingerprint(name, args):
    """A stable id for one decision, so re-proposing the same call counts as a
    challenge round while a genuinely different approach starts fresh."""
    import hashlib

    try:
        blob = json.dumps(
            {"name": name, "args": args},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        blob = f"{name}:{args}"
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
