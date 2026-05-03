"""Pytest gate for ``tools/check_no_test_helpers_imported.py``.

Runs the lint and asserts zero violations. Acts as the CI gate
via the test suite — if anyone adds ``from tests._helpers`` to
production code, this test fails.

Spec change: ``consolidate-on-neo4j-source``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Add project root to import path so we can import the lint helper
# directly without polluting sys.path further.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "tools"))

from check_no_test_helpers_imported import find_violations  # noqa: E402


class NoTestHelpersInProductionTests(unittest.TestCase):
    def test_no_violations_under_src(self) -> None:
        src_dir = _PROJECT_ROOT / "src"
        violations = find_violations(src_dir)
        if violations:
            lines = [
                f"  {path}:{line_no}: {text}"
                for path, line_no, text in violations
            ]
            self.fail(
                "Production code must not import from tests._helpers.\n"
                "Found:\n" + "\n".join(lines)
            )


if __name__ == "__main__":
    unittest.main()
