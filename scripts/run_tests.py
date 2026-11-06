"""Run the whole engine suite: one entrypoint, one verdict.

    python scripts/run_tests.py            # or: uv run python scripts/run_tests.py

Prefers the project venv's site-packages so a synced checkout works with the
interpreter already on PATH. The exit code is the verdict - pipe nothing over
it, a masked exit code is how a red suite ships.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

site = ROOT / ".venv" / "Lib" / "site-packages"
if site.is_dir():
    sys.path.insert(1, str(site))


for key in ("NO_PROXY", "no_proxy"):
    hosts = {h.strip() for h in os.environ.get(key, "").split(",") if h.strip()}
    hosts.update(("127.0.0.1", "localhost", "::1"))
    os.environ[key] = ",".join(sorted(hosts))

suite = unittest.defaultTestLoader.discover(ROOT / "tests", pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
