from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import Neo4jConfig
from xauditor.errors import GraphdbError, Neo4jError, RetryExhaustedError
from xauditor.integrations.neo4j import Neo4jAdapter
from xauditor.runtime_logging import RuntimeLogger


class Neo4jResilienceTests(unittest.TestCase):
    def test_ping_retries_transient_failures(self) -> None:
        class FailTwiceDriver:
            def __init__(self) -> None:
                self.calls = 0

            def verify_connectivity(self) -> None:
                self.calls += 1
                if self.calls < 3:
                    raise GraphdbError("temporary outage")

            def run_batch(self, queries, *, timeout=None) -> None:
                raise AssertionError("run_batch should not be called")

        driver = FailTwiceDriver()
        adapter = Neo4jAdapter(config=Neo4jConfig(), driver=driver)

        with patch("xauditor.resilience.time.sleep", return_value=None):
            adapter.ping()

        self.assertEqual(driver.calls, 3)

    def test_bootstrap_schema_raises_typed_error_after_retry_exhaustion(self) -> None:
        class AlwaysFailDriver:
            def __init__(self) -> None:
                self.calls = 0

            def verify_connectivity(self) -> None:
                return None

            def run_batch(self, _queries, *, timeout=None) -> None:
                self.calls += 1
                raise GraphdbError("neo4j secret outage")

        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret",))
        driver = AlwaysFailDriver()
        adapter = Neo4jAdapter(config=Neo4jConfig(), driver=driver, logger=logger)

        with patch("xauditor.resilience.time.sleep", return_value=None):
            with self.assertRaises(Neo4jError) as ctx:
                adapter.bootstrap_schema()

        self.assertIsInstance(ctx.exception.__cause__, RetryExhaustedError)
        self.assertEqual(driver.calls, 3)
        rendered = stream.getvalue()
        self.assertRegex(rendered, r"ERROR \[[^\]]+\]: Neo4j operation failed")
        self.assertIn("operation=neo4j.bootstrap_schema", rendered)
        self.assertIn("attempts=3", rendered)
        self.assertNotIn("secret", rendered)


if __name__ == "__main__":
    unittest.main()
