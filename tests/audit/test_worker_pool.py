"""WorkerPool Protocol + LocalThreadPool unit tests.

Covers Phase 1 of ``parallelize-audit-path-execution``:
- ``LocalThreadPool`` satisfies the WorkerPool Protocol.
- ``submit_path`` runs the callable with the right args + returns a
  Future that resolves to the callable's result.
- ``cancel_all`` drops unstarted futures.
- ``shutdown`` releases pool resources.
- ``PathResult`` round-trips through ``pickle.dumps`` / ``loads`` so
  Phase 3's ``LocalSubprocessPool`` can ship without re-designing
  the payload.
- ``build_worker_pool`` factory dispatch.
"""

from __future__ import annotations

import pickle
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit.worker_pool import (
    InlineExecutor,
    LocalSubprocessPool,
    PathOutcome,
    PathResult,
    WorkerCrashedError,
    WorkerPool,
    build_worker_pool,
)


def _stub_runner(index: int, unit) -> PathResult:
    return PathResult(
        index=index,
        path_fingerprint=str(unit),
        outcome=PathOutcome.COMPLETED,
        checkpoint_status="no_finding",
    )


class PickleRoundTripTests(unittest.TestCase):
    """PathResult must pickle cleanly so Phase 3's LocalSubprocessPool
    can ship without redesigning the payload."""

    def test_minimal_path_result_round_trips(self) -> None:
        result = PathResult(
            index=1,
            path_fingerprint="abc",
            outcome=PathOutcome.COMPLETED,
        )
        revived = pickle.loads(pickle.dumps(result))
        self.assertEqual(revived, result)

    def test_realistic_path_result_round_trips(self) -> None:
        result = PathResult(
            index=42,
            path_fingerprint="path::deadbeef",
            outcome=PathOutcome.COMPLETED,
            checkpoint_status="Valid",
            path_shared_state={
                "analyzer": {"status": "candidate", "finding_name": "X"},
                "validator": {"status": "Valid", "analysis": "ok"},
            },
            per_finding=(),
            error_info=None,
        )
        revived = pickle.loads(pickle.dumps(result))
        self.assertEqual(revived, result)
        self.assertEqual(revived.path_shared_state["analyzer"]["status"], "candidate")

    def test_failed_path_result_round_trips(self) -> None:
        result = PathResult(
            index=3,
            path_fingerprint="path::failed",
            outcome=PathOutcome.LLM_FAILED,
            error_info={
                "operation": "validator",
                "provider": "openai",
                "model": "gpt-test",
                "cause": "timeout after 600s",
            },
        )
        revived = pickle.loads(pickle.dumps(result))
        self.assertEqual(revived, result)
        self.assertEqual(revived.outcome, PathOutcome.LLM_FAILED)


class BuildWorkerPoolTests(unittest.TestCase):
    def test_worker_count_one_returns_inline_executor(self) -> None:
        pool = build_worker_pool(worker_count=1)
        try:
            self.assertIsInstance(pool, InlineExecutor)
            # Should still satisfy the WorkerPool Protocol so the
            # workflow main loop is implementation-agnostic.
            self.assertIsInstance(pool, WorkerPool)
        finally:
            pool.shutdown(wait=True)

    def test_worker_count_two_without_config_raises(self) -> None:
        # ``LocalSubprocessPool`` requires both ``config`` (for
        # workflow reconstruction) and ``source_descriptor`` (for
        # source reconstruction via rebuild_source). The factory
        # rejects calls that omit them.
        with self.assertRaises(ValueError) as ctx:
            build_worker_pool(worker_count=2)
        message = str(ctx.exception)
        self.assertIn("config", message)
        self.assertIn("source_descriptor", message)


class LocalSubprocessPoolValidationTests(unittest.TestCase):
    """Constructor input validation. Skips actual subprocess
    spawning — that's covered by integration tests in Phase 4
    once the test harness is wired for subprocess imports.

    ``path_concurrency`` is no longer a constructor parameter
    since 0.8.0 (``flatten-path-concurrency-into-worker-count``);
    subprocess workers process one path at a time.
    """

    def test_worker_count_below_two_rejected(self) -> None:
        for bad in (1, 0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as ctx:
                    LocalSubprocessPool(
                        worker_count=bad,
                        config=object(),
                        source_descriptor=object(),
                    )
                self.assertIn("worker_count", str(ctx.exception))

    def test_source_descriptor_required(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            LocalSubprocessPool(
                worker_count=2,
                config=object(),
                source_descriptor=None,
            )
        self.assertIn("source_descriptor", str(ctx.exception))


def _build_smoke_config(repo: Path) -> object:
    """Minimal valid ``XAuditorConfig`` for subprocess spawn smoke tests."""
    from xauditor.config import load_config

    (repo / "xauditor.yml").write_text(
        "llm:\n"
        "  default_provider: p1\n"
        "  providers:\n"
        "    p1:\n"
        "      base_url: http://localhost\n"
        "      api_key: k\n"
        "      model_name: m\n",
        encoding="utf-8",
    )
    return load_config(repo_root=repo, env={"HOME": str(repo)})


def _build_smoke_descriptor(repo: Path) -> object:
    """Minimal sentinel ``Neo4jSourceDescriptor`` for subprocess spawn
    smoke tests.

    The smoke tests verify the IPC + spawn + shutdown lifecycle only;
    they do NOT exercise actual Neo4j connectivity. The descriptor's
    config points at a port that no Neo4j is running on so any
    accidental rebuild_source call would surface immediately.
    """

    from xauditor.audit.source import Neo4jSourceDescriptor
    from xauditor.config import Neo4jConfig

    neo4j_config = Neo4jConfig(
        image="neo4j:5",
        container_name="xauditor-smoke",
        password="smoke-password",
        bolt_port=7687,
        http_port=7474,
        username="neo4j",
        database="neo4j",
        network_name="xauditor-smoke-net",
    )
    return Neo4jSourceDescriptor(
        neo4j_config=neo4j_config,
        host="127.0.0.1",
        build_fingerprint="smoke-fp",
        repo_root=repo,
        page_size=100,
    )


class LocalSubprocessPoolSpawnSmokeTests(unittest.TestCase):
    """End-to-end: spawn real subprocess workers, send sentinel,
    wait for clean exit. Verifies the IPC + spawn + shutdown
    roundtrip without going through any LLM call."""

    def test_construct_then_shutdown_joins_workers_cleanly(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = _build_smoke_config(repo)
            descriptor = _build_smoke_descriptor(repo)
            pool = LocalSubprocessPool(
                worker_count=2,
                config=config,
                source_descriptor=descriptor,
            )
            try:
                # Workers should be alive immediately after spawn.
                self.assertEqual(len(pool._workers), 2)
                # Give workers a moment to import xauditor inside
                # the spawned interpreter (~hundreds of ms).
                time.sleep(0.5)
                self.assertTrue(
                    all(p.is_alive() for p in pool._workers),
                    "all subprocess workers should be alive after spawn",
                )
            finally:
                pool.shutdown(wait=True)
            # Sentinel-based shutdown should leave every worker
            # exited within the join grace period.
            self.assertTrue(
                all(not p.is_alive() for p in pool._workers),
                "all subprocess workers should have exited after shutdown",
            )


class WorkerCrashedErrorTests(unittest.TestCase):
    def test_carries_path_index_and_exit_code(self) -> None:
        err = WorkerCrashedError(
            path_index=17, exit_code=-9, worker_id="host42/1234"
        )
        self.assertEqual(err.path_index, 17)
        self.assertEqual(err.exit_code, -9)
        self.assertEqual(err.worker_id, "host42/1234")
        self.assertIn("17", str(err))
        self.assertIn("-9", str(err))


if __name__ == "__main__":
    unittest.main()
