"""End-to-end smoke against a real Neo4j 5.x via testcontainers.

Closes the §2.3 deferred task in the ``consolidate-on-neo4j-source``
change. Exercises the streaming canonical_finalize → Neo4jAuditGraphSource
read path on a live Bolt connection — what shipped in 0.9.0 but had been
unverified by automated tests.

This test is **not part of the default unit-test sweep**:

  - Skipped unless ``XAUDITOR_TEST_NEO4J_TESTCONTAINERS=1`` is set.
  - Skipped if ``testcontainers`` is not importable.
  - Skipped if Docker is unreachable.

Run it locally with:

    XAUDITOR_TEST_NEO4J_TESTCONTAINERS=1 \\
        uv run --python 3.12 --with pytest --with neo4j \\
            --with 'testcontainers[neo4j]' --with httpx --with pyyaml \\
            pytest tests/integration/test_neo4j_e2e.py

Container startup is ~10–20 s on a warm host; the smoke methods run in
single-digit seconds against the live container. Keep this file lean —
the unit-level coverage of chunk writers and the Protocol seams lives in
the regular ``tests/`` tree.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.config import Neo4jConfig
from xauditor.integrations.neo4j_driver import Neo4jDriver
from xauditor.integrations.neo4j_repository import Neo4jGraphRepository
from xauditor.integrations.audit_source_neo4j import Neo4jAuditGraphSource
from xauditor.models import (
    ConfidenceLevel,
    EdgeRecord,
    FunctionRecord,
    GraphProvenance,
    PathRecord,
)


_RUN_GATE = os.environ.get("XAUDITOR_TEST_NEO4J_TESTCONTAINERS", "").strip() == "1"


@unittest.skipUnless(
    _RUN_GATE,
    "Set XAUDITOR_TEST_NEO4J_TESTCONTAINERS=1 to run this test against a "
    "Neo4j container managed by testcontainers.",
)
class Neo4jStreamingFinalizeSmokeTests(unittest.TestCase):
    """Real-Bolt smoke for the §4 streaming chunk writers + §2 read Protocol."""

    container = None
    driver: Neo4jDriver | None = None
    repository: Neo4jGraphRepository | None = None
    BUILD_FP = "test-bf-e2e"

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from testcontainers.neo4j import Neo4jContainer  # type: ignore
        except ImportError as exc:  # pragma: no cover - gated
            raise unittest.SkipTest(
                "testcontainers[neo4j] not installed. "
                "uv run --with 'testcontainers[neo4j]' …"
            ) from exc

        # ``neo4j:5-community`` matches the production-managed image
        # tag in xauditor's default Neo4jConfig.
        cls.container = Neo4jContainer("neo4j:5-community")
        try:
            cls.container.start()
        except Exception as exc:  # pragma: no cover - gated
            raise unittest.SkipTest(
                f"Neo4j container failed to start (Docker unreachable?): "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc

        # ``Neo4jContainer.get_connection_url()`` returns a bolt:// URI
        # against a randomised host port. We pull the host + port out
        # so we can hand them to the existing Neo4jDriver constructor
        # which expects a ``Neo4jConfig`` + ``host`` pair.
        bolt_url = cls.container.get_connection_url()
        # bolt://host:port → split off the scheme + parse host/port.
        without_scheme = bolt_url.split("://", 1)[1]
        host, port_str = without_scheme.rsplit(":", 1)
        port = int(port_str)

        cls.config = Neo4jConfig(
            password=cls.container.password
            if hasattr(cls.container, "password")
            else "test",
            bolt_port=port,
        )
        cls.driver = Neo4jDriver(cls.config, host=host)
        # ``verify_connectivity`` is the same ping the production
        # preflight uses. Failure here means the container is up but
        # not authenticating — surface as a clean test error, not a
        # generic Bolt traceback.
        cls.driver.verify_connectivity()
        cls.repository = Neo4jGraphRepository(
            config=cls.config, driver=cls.driver
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.driver is not None:
            try:
                cls.driver.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        if cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        # Wipe the database between tests so each method's writes are
        # observed against a clean state. ``MATCH (n) DETACH DELETE n``
        # is the canonical Neo4j-side reset.
        assert self.driver is not None
        self.driver.run_write("MATCH (n) DETACH DELETE n")

    # ------------------------------------------------------------------
    # write_function_chunk → iter_functions / lookup_function_by_id
    # ------------------------------------------------------------------

    def test_function_chunk_writes_and_iter_reads_back_round_trip(self) -> None:
        assert self.repository is not None
        records = [
            FunctionRecord(
                function_id=f"fn-{idx}",
                name=f"f{idx}",
                qualified_name=f"mod.f{idx}",
                file_path=f"src/mod_{idx}.py",
                module_name=f"mod_{idx}",
                start_line=1,
                end_line=10,
                source=f"def f{idx}(): pass",
                summary=f"summary {idx}",
                business_context="",
                trust_boundary="",
            )
            for idx in range(7)
        ]
        self.repository.write_function_chunk(self.BUILD_FP, records)

        source = Neo4jAuditGraphSource(
            repository=self.repository,
            build_fingerprint=self.BUILD_FP,
            repo_root=Path("/tmp/repo"),
        )
        readback = list(source.iter_functions())
        ids = sorted(fn.function_id for fn in readback)
        self.assertEqual(ids, [f"fn-{idx}" for idx in range(7)])
        # Per-record fields round-trip on at least one entry.
        fn3 = source.lookup_function_by_id("fn-3")
        self.assertIsNotNone(fn3)
        self.assertEqual(fn3.qualified_name, "mod.f3")
        self.assertEqual(fn3.file_path, "src/mod_3.py")
        self.assertEqual(fn3.summary, "summary 3")
        # Cache verification: second call uses the cached dict, no
        # extra Bolt round-trip needed for correctness.
        self.assertIs(source.lookup_function_by_id("fn-3"), fn3)

    def test_function_chunk_idempotent_under_repeated_writes(self) -> None:
        assert self.repository is not None
        records = [
            FunctionRecord(
                function_id="fn-stable",
                name="stable",
                qualified_name="mod.stable",
                file_path="src/mod.py",
                module_name="mod",
                start_line=1,
                end_line=5,
                source="def stable(): pass",
            ),
        ]
        # Write twice — UNWIND/MERGE semantics from §4.2 SHALL converge.
        self.repository.write_function_chunk(self.BUILD_FP, records)
        self.repository.write_function_chunk(self.BUILD_FP, records)

        source = Neo4jAuditGraphSource(
            repository=self.repository,
            build_fingerprint=self.BUILD_FP,
            repo_root=Path("/tmp/repo"),
        )
        readback = list(source.iter_functions())
        # Exactly one node, not two — UNWIND MERGE didn't double-up.
        self.assertEqual(len(readback), 1)
        self.assertEqual(readback[0].function_id, "fn-stable")

    # ------------------------------------------------------------------
    # write_path_chunk → iter_paths / lookup_path_by_fingerprint
    # ------------------------------------------------------------------

    def test_path_chunk_writes_and_lookup_by_fingerprint(self) -> None:
        assert self.repository is not None
        # Need the function nodes the path references (FK edges in the
        # Cypher), so seed two functions first.
        self.repository.write_function_chunk(
            self.BUILD_FP,
            [
                FunctionRecord(
                    function_id="fn-entry",
                    name="entry",
                    qualified_name="mod.entry",
                    file_path="src/mod.py",
                    module_name="mod",
                    start_line=1,
                    end_line=3,
                    source="def entry(): inner()",
                ),
                FunctionRecord(
                    function_id="fn-inner",
                    name="inner",
                    qualified_name="mod.inner",
                    file_path="src/mod.py",
                    module_name="mod",
                    start_line=5,
                    end_line=6,
                    source="def inner(): pass",
                ),
            ],
        )

        path = PathRecord(
            entry_function="mod.entry",
            function_names=("mod.entry", "mod.inner"),
            file_paths=("src/mod.py", "src/mod.py"),
            path_fingerprint="path-abc-123",
            function_ids=("fn-entry", "fn-inner"),
            business_context="",
            trust_boundary="",
        )
        self.repository.write_path_chunk(self.BUILD_FP, [path])

        source = Neo4jAuditGraphSource(
            repository=self.repository,
            build_fingerprint=self.BUILD_FP,
            repo_root=Path("/tmp/repo"),
        )
        paths = list(source.iter_paths())
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].path_fingerprint, "path-abc-123")
        self.assertEqual(paths[0].entry_function, "mod.entry")

        # lookup_path_by_fingerprint resolves the same record.
        looked_up = source.lookup_path_by_fingerprint("path-abc-123")
        self.assertIsNotNone(looked_up)
        self.assertEqual(looked_up.path_fingerprint, "path-abc-123")
        # Negative — unknown fingerprint returns None per the
        # AuditGraphSource Protocol contract.
        self.assertIsNone(source.lookup_path_by_fingerprint("path-does-not-exist"))

    # ------------------------------------------------------------------
    # Multi-chunk writes (covers §4.5 chunk-size handling against real Bolt)
    # ------------------------------------------------------------------

    def test_multi_chunk_writes_total_count_matches_input(self) -> None:
        assert self.repository is not None
        # Three chunks of 250 each — well above any single Cypher
        # statement's reasonable batch but below testcontainers'
        # default Neo4j heap. Cumulative: 750 unique function nodes.
        for chunk_idx in range(3):
            base = chunk_idx * 250
            chunk = [
                FunctionRecord(
                    function_id=f"fn-multi-{base + i}",
                    name=f"f{base + i}",
                    qualified_name=f"mod.f{base + i}",
                    file_path="src/mod.py",
                    module_name="mod",
                    start_line=1,
                    end_line=2,
                    source="pass",
                )
                for i in range(250)
            ]
            self.repository.write_function_chunk(self.BUILD_FP, chunk)

        source = Neo4jAuditGraphSource(
            repository=self.repository,
            build_fingerprint=self.BUILD_FP,
            repo_root=Path("/tmp/repo"),
        )
        readback = list(source.iter_functions())
        self.assertEqual(len(readback), 750)
        # Spot-check that the spread is correct (no off-by-chunk).
        ids = {fn.function_id for fn in readback}
        self.assertIn("fn-multi-0", ids)
        self.assertIn("fn-multi-249", ids)  # last in chunk 0
        self.assertIn("fn-multi-250", ids)  # first in chunk 1
        self.assertIn("fn-multi-749", ids)  # last in chunk 2

    # ------------------------------------------------------------------
    # iter_edges — the §1.3 wrap of the existing _load_edges
    # ------------------------------------------------------------------

    def test_edge_chunk_writes_and_iter_edges_round_trip(self) -> None:
        assert self.repository is not None
        # Edges need their endpoint function nodes to exist.
        self.repository.write_function_chunk(
            self.BUILD_FP,
            [
                FunctionRecord(
                    function_id="fn-a",
                    name="a",
                    qualified_name="mod.a",
                    file_path="src/mod.py",
                    module_name="mod",
                    start_line=1,
                    end_line=2,
                    source="pass",
                ),
                FunctionRecord(
                    function_id="fn-b",
                    name="b",
                    qualified_name="mod.b",
                    file_path="src/mod.py",
                    module_name="mod",
                    start_line=3,
                    end_line=4,
                    source="pass",
                ),
            ],
        )
        edges = [
            EdgeRecord(
                source_function="fn-a",
                target_function="fn-b",
                edge_type="calls",
                provenance=GraphProvenance(
                    file_path="src/mod.py",
                    line_number=2,
                    evidence="inner()",
                ),
                confidence=ConfidenceLevel.HIGH,
            )
        ]
        self.repository.write_edge_chunk(self.BUILD_FP, edges)

        source = Neo4jAuditGraphSource(
            repository=self.repository,
            build_fingerprint=self.BUILD_FP,
            repo_root=Path("/tmp/repo"),
        )
        readback = list(source.iter_edges())
        self.assertEqual(len(readback), 1)
        self.assertEqual(readback[0].source_function, "fn-a")
        self.assertEqual(readback[0].target_function, "fn-b")
        self.assertEqual(readback[0].edge_type, "calls")


if __name__ == "__main__":
    unittest.main()
