"""Tuning constants for budgeting and compaction. No environment reads.

The context window is per model in providers.json (model row), falling back
to DEFAULT_CONTEXT_WINDOW when the row leaves it unset.
"""

DEFAULT_CONTEXT_WINDOW = 128_000

COMPACT_AT = 0.85
COMPACT_TO = 0.35
