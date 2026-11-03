"""The non-interactive review, for scripts and CI.

    python -m harness.review --scope uncommitted --json

Exit codes are the verdict, so a script can branch on them:

    0   clean, or suggestions only
    1   changes requested
    2   no reviewer is configured, or none of them can be reached
    3   there was nothing to review
    4   every reviewer failed
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

EXIT_CLEAN = 0
EXIT_CHANGES = 1
EXIT_NO_REVIEWER = 2
EXIT_NOTHING = 3
EXIT_FAILED = 4


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m harness.review",
        description=(
            "Run one or more reviewer models over the change in this "
            "workspace and print a merged report."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="the workspace to review (defaults to the current directory)",
    )
    parser.add_argument(
        "--scope",
        default="uncommitted",
        help=(
            "what to review: run (the files the last run changed), "
            "uncommitted, staged, or base:<ref> to compare against a branch"
        ),
    )
    parser.add_argument(
        "--models",
        default="",
        help="comma separated model ids to review with (defaults to the configured reviewers)",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="restrict one reviewer's attention to one target, e.g. security",
    )
    parser.add_argument(
        "--severity",
        default="",
        help="the lowest severity to report: blocker, high, medium, low or nit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the structured report instead of the readable summary",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run again even when nothing changed since the last review",
    )
    parser.add_argument(
        "--output",
        default="",
        help="write the report to this file as well as to stdout",
    )
    return parser


def _reviewers_from(args, settings):
    chosen = [m.strip() for m in (args.models or "").split(",") if m.strip()]
    if not chosen:
        from . import config as config_mod

        return config_mod.enabled_reviewers(settings)
    return [
        {"model_id": model_id, "label": "", "enabled": True, "focus": args.focus}
        for model_id in chosen
    ]


def _render(report):
    findings = report.get("findings") or []
    lines = [
        f"review: {report.get('verdict_label', 'unknown')} "
        f"({len(findings)} finding(s))",
    ]
    scope = report.get("scope") or {}
    if scope.get("files"):
        lines.append(f"files: {', '.join(f['path'] for f in scope['files'])}")
    if scope.get("skipped"):
        names = ", ".join(item["path"] for item in scope["skipped"][:10])
        lines.append(f"skipped: {names}")
    if report.get("redacted"):
        lines.append(f"redacted: {report['redacted']} secret-looking value(s)")
    for model in report.get("models") or []:
        lines.append(
            f"model {model.get('label') or model.get('model_id')}: "
            f"{model.get('status')} ({model.get('tokens')} tokens, "
            f"{model.get('seconds')}s)"
            + (f" - {model.get('error')}" if model.get("error") else "")
        )
    for finding in findings:
        location = finding.get("file", "")
        if finding.get("line"):
            location = f"{location}:{finding['line']}"
        lines.append("")
        lines.append(
            f"[{finding.get('id')}] {finding.get('severity')} "
            f"({finding.get('category')}) {location}"
        )
        lines.append(f"  {finding.get('summary')}")
        if finding.get("why"):
            lines.append(f"  why: {finding['why']}")
        if finding.get("fix"):
            lines.append(f"  fix: {finding['fix']}")
        if finding.get("models"):
            lines.append(f"  raised by: {', '.join(finding['models'])}")
    return "\n".join(lines)


def main(argv=None):
    args = build_parser().parse_args(argv)

    from .. import config as config_mod
    from .. import paths
    from . import config as review_config
    from . import engine, scope as scope_mod

    root = Path(args.root).resolve() if args.root else paths.project_root()
    # Only initialise when nothing is set up yet: re-initialising would
    # point the app data dir back at the default and lose the saved role
    # configuration - including the reviewers this run is about to use.
    if paths.is_initialized():
        scope_context = paths.rooted(root)
    else:
        paths.init(project_root=root)
        scope_context = contextlib.nullcontext()

    with scope_context:
        settings = dict(review_config.load())
        if args.severity:
            settings["severity_threshold"] = args.severity

        mode, base = scope_mod.parse_scope(args.scope)
        reviewers = _reviewers_from(args, settings)
        if not reviewers:
            message = (
                "no reviewer model is configured; choose one in Settings, Review, "
                "or pass --models <id>"
            )
            print(message, file=sys.stderr)
            return EXIT_NO_REVIEWER

        document = engine.run(
            root,
            "",
            mode=mode,
            base=base,
            reviewers=reviewers,
            settings=settings,
            force=args.force,
            triggered_by="cli",
        )

        verdict = document.get("verdict")
        if verdict == "nothing":
            print("nothing to review", file=sys.stderr)
            code = EXIT_NOTHING
        elif verdict == "failed":
            print(document.get("error") or "the review failed", file=sys.stderr)
            code = EXIT_FAILED
        elif verdict == "changes_requested":
            code = EXIT_CHANGES
        else:
            code = EXIT_CLEAN

        body = (
            json.dumps(document, ensure_ascii=False, indent=2)
            if args.json
            else _render(document)
        )
        print(body)
        if args.output:
            try:
                Path(args.output).write_text(
                    json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as failure:
                print(f"could not write {args.output}: {failure}", file=sys.stderr)
                code = code or EXIT_FAILED
        return code


if __name__ == "__main__":
    sys.exit(main())
