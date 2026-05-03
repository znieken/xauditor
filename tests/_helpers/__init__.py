"""Test-only helpers for the xauditor unit/integration test suites.

**Production code MUST NOT import from this package.** A CI lint
step (``tools/check_no_test_helpers_imported.py``) enforces this
rule by failing the build if any ``src/`` file references
``tests._helpers``.

The package exists because some unit tests need an in-memory
``AuditGraphSource`` to construct minimal-but-valid fixtures
without spinning up a Neo4j daemon. Pre-0.9.0 those tests
constructed ``GraphBuildResult(...)`` directly and wrapped in
``InMemoryAuditGraphSource``; both classes were removed in
``consolidate-on-neo4j-source``. ``_TestOnlyGraphSource`` here is
the replacement test double — a thin Protocol-conforming class
backed by simple dict storage. It deliberately lives outside
``src/`` so production code paths cannot reach it.

For end-to-end smoke tests that exercise real Bolt round-trips
(subprocess descriptor pickling, build → Neo4j → audit), use
``tests/integration/test_neo4j_e2e.py`` (testcontainers-driven,
gated behind ``@pytest.mark.integration``).
"""

from tests._helpers.test_only_graph_source import _TestOnlyGraphSource

__all__ = ["_TestOnlyGraphSource"]
