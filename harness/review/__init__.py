"""Multi-model review: independent reviewer models critique a change.

The entry point is :func:`engine.run`, which takes a workspace and returns a
report. Everything else in this package is a piece of that:

``scope``      what to review (a git diff, or the checkpoint journal)
``redact``     what must not be sent to a provider
``prompt``     the reviewer's instructions - the one place they live
``findings``   what a reviewer returns, and what survives validation
``aggregate``  merging several reviewers into one report
``engine``     running the reviewers
``report``     the saved report, and the short version the agent reads
"""

_SUBMODULES = (
    "aggregate",
    "config",
    "engine",
    "findings",
    "prompt",
    "redact",
    "report",
    "scope",
)


def __getattr__(name):
    if name in _SUBMODULES:
        import importlib

        module = importlib.import_module("." + name, __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run(*args, **kwargs):
    from . import engine

    return engine.run(*args, **kwargs)


__all__ = [*_SUBMODULES, "run"]
