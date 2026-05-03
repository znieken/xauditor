"""Lint: production code (under ``src/``) MUST NOT import from
``tests._helpers``.

The ``tests/_helpers/`` package contains test-only doubles like
``_TestOnlyGraphSource``. Production code must not depend on
them — that would defeat the purpose (and violate the project's
test/source separation contract documented in
``consolidate-on-neo4j-source``).

Usage:
    python tools/check_no_test_helpers_imported.py [src_dir]

Exits 1 with a descriptive error if any forbidden import is
found; exits 0 otherwise. Designed to be wired into pre-commit /
CI pipelines AND to be runnable as a unit test (see
``tests/test_no_test_helpers_in_src.py``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Forbidden patterns. Cover both `from tests._helpers ...` and
# `import tests._helpers ...` and the package-relative variants.
FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*from\s+tests\._helpers\b", re.MULTILINE),
    re.compile(r"^\s*import\s+tests\._helpers\b", re.MULTILINE),
]


def find_violations(src_dir: Path) -> list[tuple[Path, int, str]]:
    """Return list of (file_path, line_number, line_text) for every
    forbidden import found under ``src_dir``."""
    violations: list[tuple[Path, int, str]] = []
    if not src_dir.exists():
        return violations
    for py_file in src_dir.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in FORBIDDEN_PATTERNS:
            for match in pattern.finditer(text):
                line_number = text.count("\n", 0, match.start()) + 1
                line_text = text.splitlines()[line_number - 1].strip()
                violations.append((py_file, line_number, line_text))
    return violations


def main(argv: list[str]) -> int:
    src_dir = Path(argv[1]) if len(argv) > 1 else Path("src")
    violations = find_violations(src_dir)
    if not violations:
        return 0
    sys.stderr.write(
        f"Forbidden imports of `tests._helpers` found in {src_dir}/:\n"
    )
    for path, line, text in violations:
        sys.stderr.write(f"  {path}:{line}: {text}\n")
    sys.stderr.write(
        "\nProduction code must not depend on test-only helpers.\n"
        "See consolidate-on-neo4j-source spec change for context.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
