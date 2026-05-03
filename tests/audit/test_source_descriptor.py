"""``AuditGraphSourceDescriptor`` ADT + ``rebuild_source`` factory tests.

Covers:
- ``Neo4jSourceDescriptor`` round-trips through ``pickle.dumps`` /
  ``pickle.loads`` (subprocess pool ships them across the
  ``multiprocessing.spawn`` boundary).
- ``rebuild_source(Neo4jSourceDescriptor)`` is exercised at the
  factory dispatch level — instantiating a ``Neo4jDriver`` requires a
  live Bolt endpoint, which the unit-test environment lacks; we patch
  the driver/repository constructors with no-op stubs and verify the
  factory wires arguments correctly.
- ``rebuild_source`` rejects unknown descriptor types.

0.9.0 ``consolidate-on-neo4j-source``: the in-memory descriptor /
in-memory source path is gone; only Neo4j is supported.
"""

from __future__ import annotations

import pickle
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit.source import (
    Neo4jSourceDescriptor,
    rebuild_source,
)
from xauditor.config import Neo4jConfig


class Neo4jSourceDescriptorTests(unittest.TestCase):
    def _make_descriptor(self) -> Neo4jSourceDescriptor:
        # Neo4jConfig is a frozen dataclass; instantiation does not
        # connect to any Neo4j instance.
        neo4j_config = Neo4jConfig(
            image="neo4j:5",
            container_name="xauditor-test-neo4j",
            password="test-password",
            bolt_port=7687,
            http_port=7474,
            username="neo4j",
            database="neo4j",
            network_name="xauditor-test-net",
        )
        return Neo4jSourceDescriptor(
            neo4j_config=neo4j_config,
            host="localhost",
            build_fingerprint="fp-neo4j",
            repo_root=Path("/tmp/repo"),
            page_size=200,
        )

    def test_pickle_round_trip(self) -> None:
        desc = self._make_descriptor()
        revived = pickle.loads(pickle.dumps(desc))
        self.assertEqual(revived, desc)
        self.assertEqual(revived.host, "localhost")
        self.assertEqual(revived.build_fingerprint, "fp-neo4j")
        self.assertEqual(revived.page_size, 200)
        self.assertEqual(revived.neo4j_config.bolt_port, 7687)


class RebuildSourceFactoryTests(unittest.TestCase):
    def test_neo4j_descriptor_dispatches_via_rebuild(self) -> None:
        # Patch the Neo4jDriver / Neo4jGraphRepository constructors
        # so the factory call doesn't try to open a real Bolt
        # connection. We're testing dispatch correctness, not the
        # neo4j client itself.
        neo4j_config = Neo4jConfig(
            image="neo4j:5",
            container_name="x",
            password="p",
            bolt_port=7687,
            http_port=7474,
            username="u",
            database="d",
            network_name="n",
        )
        desc = Neo4jSourceDescriptor(
            neo4j_config=neo4j_config,
            host="bolt-host",
            build_fingerprint="fp-neo4j-rebuild",
            repo_root=Path("/tmp/repo"),
            page_size=50,
        )

        with patch(
            "xauditor.integrations.neo4j_driver.Neo4jDriver"
        ) as driver_cls, patch(
            "xauditor.integrations.neo4j_repository.Neo4jGraphRepository"
        ) as repo_cls:
            driver_cls.return_value = object()  # stub instance
            repo_cls.return_value = object()
            source = rebuild_source(desc)

        # Driver constructor receives the descriptor's config + host.
        driver_cls.assert_called_once_with(neo4j_config, host="bolt-host")
        # Repository receives the same config and the stub driver.
        ((), call_kwargs) = (repo_cls.call_args.args, repo_cls.call_args.kwargs)
        self.assertIs(call_kwargs["config"], neo4j_config)
        self.assertIs(call_kwargs["driver"], driver_cls.return_value)
        # Source carries the descriptor's identity fields.
        self.assertEqual(source.build_fingerprint, "fp-neo4j-rebuild")
        self.assertEqual(source.repo_root, Path("/tmp/repo"))

    def test_unknown_descriptor_type_raises(self) -> None:
        class _BogusDescriptor:
            pass

        with self.assertRaises(NotImplementedError) as ctx:
            rebuild_source(_BogusDescriptor())  # type: ignore[arg-type]
        self.assertIn("rebuild_source", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
