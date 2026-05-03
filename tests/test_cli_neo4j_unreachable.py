"""Tests for the startup Neo4j connectivity check.

Covers ``consolidate-on-neo4j-source`` task 6.x: the
``_check_neo4j_reachable_or_die`` helper at the start of
``xauditor graph build`` and ``xauditor audit run`` emits a numbered
three-option error block when Neo4j is unreachable.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.cli import _check_neo4j_reachable_or_die, main
from xauditor.errors import GraphdbError, XAuditorError
from xauditor.services import ApplicationServices


class _FakeAdapter:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.set_logger_calls: list = []

    def ping(self) -> None:
        raise self._exc

    def set_logger(self, logger) -> None:
        self.set_logger_calls.append(logger)


class _FakeServices:
    def __init__(self, exc: Exception) -> None:
        from xauditor.config import Neo4jConfig

        self.neo4j = _FakeAdapter(exc)

        class _Config:
            graphdb = Neo4jConfig(
                image="neo4j:5",
                container_name="x",
                password="p",
                bolt_port=7687,
                http_port=7474,
                username="neo4j",
                database="neo4j",
                network_name="n",
            )

        self.config = _Config()


class StartupConnectivityCheckTests(unittest.TestCase):
    def test_connection_refused_raises_with_remediation_block(self) -> None:
        services = _FakeServices(GraphdbError("Connection refused"))
        with self.assertRaises(XAuditorError) as ctx:
            _check_neo4j_reachable_or_die(services)
        message = str(ctx.exception)
        self.assertIn("Cannot connect to Neo4j", message)
        self.assertIn("xauditor graphdb init", message)
        self.assertIn("xauditor graphdb start", message)
        self.assertIn("graph:", message)
        self.assertIn("remote:", message)
        self.assertIn("url: bolt://your-host:7687", message)

    def test_auth_failure_message_includes_cause(self) -> None:
        services = _FakeServices(GraphdbError("authentication failure: bad password"))
        with self.assertRaises(XAuditorError) as ctx:
            _check_neo4j_reachable_or_die(services)
        message = str(ctx.exception)
        self.assertIn("authentication failure", message)
        self.assertIn("xauditor graphdb init", message)


if __name__ == "__main__":
    unittest.main()
