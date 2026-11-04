"""One-off: show the scope failure detail."""

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(r"D:\projects\harness")
sys.path.insert(0, str(ROOT))
site = ROOT / ".venv" / "Lib" / "site-packages"
if site.is_dir():
    sys.path.insert(1, str(site))

suite = unittest.defaultTestLoader.discover(ROOT / "tests", pattern="test_review_scope.py")
runner = unittest.TextTestRunner(verbosity=1)
out = io.StringIO()
import contextlib

with contextlib.redirect_stderr(io.StringIO()):
    result = runner.run(suite)
lines = []
for case, trace in result.failures + result.errors:
    lines.append(str(case))
    lines.append(trace[-400:])
Path(r"tests\scope-probe.txt").write_text("\n".join(lines), encoding="utf-8")
print("failures:", len(result.failures), "errors:", len(result.errors))
