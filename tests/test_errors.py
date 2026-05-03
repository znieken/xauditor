from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.errors import (
    CircuitOpenError,
    LLMError,
    Neo4jError,
    RetryExhaustedError,
    ValidationError,
    XAuditorError,
)


class ErrorHierarchyTests(unittest.TestCase):
    def test_resilience_errors_inherit_from_xauditor_error(self) -> None:
        for error_type in (
            LLMError,
            Neo4jError,
            ValidationError,
            RetryExhaustedError,
            CircuitOpenError,
        ):
            self.assertTrue(issubclass(error_type, XAuditorError))


if __name__ == "__main__":
    unittest.main()
