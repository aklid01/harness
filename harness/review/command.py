"""The arguments of ``/multi-model-review``.

Parsing lives here, apart from the command that runs it, so the argument
rules can be tested without a model, a workspace or a session.

    /multi-model-review
    /multi-model-review uncommitted
    /multi-model-review base:main --models mdl-a,mdl-b --severity high
"""

from . import findings as findings_mod
from . import scope as scope_mod

HELP = """\
/multi-model-review - run independent reviewer models over the change

  /multi-model-review [scope] [options]

Scope (what gets reviewed):
  run              the files this agent run changed (the default)
  uncommitted      everything the working tree changed against HEAD
  staged           what is in the index, ready to commit
  base:<ref>       everything that differs from a branch or commit

Options:
  --models a,b     review with these model ids instead of the configured ones
  --focus <target> point one reviewer at one target, e.g. security
  --severity <s>   the lowest severity to report:
                   blocker, high, medium, low or nit
  --force          run again even when nothing changed since the last review
  --json           print the structured report as well as the card
  --help           this text

Reviewers never see each other's answers, and a review never edits anything:
it reads the change and reports what it found."""


def new_args(**overrides):
    """The one shape of a review request, with defaults.

    Every writer of review args - this parser, the review card's op, the
    automatic post-run review - starts here, so the option set exists once
    and a key cannot drift between writers. ``help``/``error`` are the
    parser's own report and belong to its return value only.
    """
    args = {
        "scope": "run",
        "base": "",
        "models": [],
        "focus": "",
        "severity": "",
        "force": False,
        "json": False,
        "paths": None,
        "auto": False,
        "offer": False,
    }
    unknown = set(overrides) - set(args)
    if unknown:
        raise TypeError(f"unknown review args: {', '.join(sorted(unknown))}")
    args.update(overrides)
    return args


def parse_args(text):
    """Parse the argument text of the command.

    Returns a ``new_args()`` request plus the parser's own ``help`` and
    ``error`` report. An unrecognised argument is an error rather than a
    silent fallback, because a typo that widened the scope would send more
    code to a provider than the user meant to send.
    """
    args = new_args()
    args["help"] = False
    args["error"] = ""
    tokens = (text or "").split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in ("--help", "-h"):
            args["help"] = True
            return args
        if token == "--json":
            args["json"] = True
            continue
        if token == "--force":
            args["force"] = True
            continue
        if token.startswith("--"):
            name, _, inline = token[2:].partition("=")
            value = inline
            if not value:
                if index < len(tokens) and not tokens[index].startswith("--"):
                    value = tokens[index]
                    index += 1
                else:
                    args["error"] = f"--{name} needs a value"
                    return args
            if name == "models":
                args["models"] = [m.strip() for m in value.split(",") if m.strip()]
            elif name == "focus":
                args["focus"] = value.strip()
            elif name == "severity":
                severity = value.strip().lower()
                if severity not in findings_mod.SEVERITIES:
                    args["error"] = (
                        f"unknown severity '{value}' - use one of "
                        + ", ".join(findings_mod.SEVERITIES)
                    )
                    return args
                args["severity"] = severity
            else:
                args["error"] = f"unknown option --{name}"
                return args
            continue
        if token.startswith("-"):
            args["error"] = f"unknown option {token}"
            return args
        if not scope_mod.valid_scope(token):
            args["error"] = (
                f"unknown scope '{token}' - use run, uncommitted, staged or base:<ref>"
            )
            return args
        mode, base = scope_mod.parse_scope(token)
        args["scope"] = mode
        args["base"] = base
    return args


def models_as_entries(models, focus=""):
    """The --models list as reviewer entries."""
    return [
        {"model_id": model_id, "label": "", "enabled": True, "focus": focus}
        for model_id in models
    ]
