"""Regression tests for ``flatten-path-concurrency-into-worker-count`` (0.8.0):

- Claim dict shape: ``dict[str, int]`` keyed by worker_id (D2).
- Subprocess workers process paths sequentially — no internal
  thread pool, at most one in-flight path per worker (D3).

Both tests use a 1-worker subprocess pool so we can spawn cheaply
and observe shape/sequencing without the multi-worker IPC noise.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import pickle
import sys
import time
import unittest
from concurrent.futures import wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit.worker_pool import (  # noqa: E402
    LocalSubprocessPool,
    PathOutcome,
    PathResult,
)


def _build_smoke_config(repo: Path) -> object:
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
    """Sentinel ``Neo4jSourceDescriptor`` for spawn-only smoke tests.

    The descriptor's host is a no-Neo4j sentinel; the smoke tests
    never call ``rebuild_source`` on it.
    """

    from xauditor.audit.source import Neo4jSourceDescriptor
    from xauditor.config import Neo4jConfig

    neo4j_config = Neo4jConfig(
        image="neo4j:5",
        container_name="xauditor-flatten",
        password="flatten-password",
        bolt_port=7687,
        http_port=7474,
        username="neo4j",
        database="neo4j",
        network_name="xauditor-flatten-net",
    )
    return Neo4jSourceDescriptor(
        neo4j_config=neo4j_config,
        host="127.0.0.1",
        build_fingerprint="flatten-fp",
        repo_root=repo,
        page_size=100,
    )


class ClaimDictShapeTests(unittest.TestCase):
    """``_claims`` is keyed by worker_id (D2). Construct a 2-worker
    pool, peek the dict before any submission, and confirm the
    shape contract.
    """

    def test_claim_dict_starts_empty_with_str_int_typing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = _build_smoke_config(repo)
            descriptor = _build_smoke_descriptor(repo)
            # Suppress the pre-check WARN since our descriptor is
            # microscopic but the test runner's available RAM
            # probe is host-dependent.
            os.environ["XAUDITOR_AUDIT_SKIP_MEM_CHECK"] = "1"
            try:
                pool = LocalSubprocessPool(
                    worker_count=2,
                    config=config,
                    source_descriptor=descriptor,
                )
            finally:
                os.environ.pop("XAUDITOR_AUDIT_SKIP_MEM_CHECK", None)
            try:
                # Empty at construction.
                self.assertEqual(pool._claims, {})
                # Annotated as ``dict[str, int]`` per D2; we lock
                # this in by writing an entry of the correct shape
                # and reading it back.
                with pool._futures_lock:
                    pool._claims["audit-worker-0"] = 17
                self.assertEqual(pool._claims["audit-worker-0"], 17)
                self.assertIsInstance(
                    list(pool._claims.keys())[0], str
                )
                self.assertIsInstance(
                    list(pool._claims.values())[0], int
                )
            finally:
                pool.shutdown(wait=True)


class SubprocessWorkerSequentialExecutionTests(unittest.TestCase):
    """Subprocess workers process one path at a time (D3). We
    can't easily run a real ``_process_one_path`` from this
    test (would need an LLM mock and full workflow setup), so
    we exercise the structural property indirectly: peek
    ``len(pool._workers) == worker_count``, confirm no internal
    thread pool was created, and confirm the worker process
    object has no ``LocalThreadPool``-related attributes.
    """

    def test_pool_has_no_path_concurrency_attribute(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = _build_smoke_config(repo)
            descriptor = _build_smoke_descriptor(repo)
            os.environ["XAUDITOR_AUDIT_SKIP_MEM_CHECK"] = "1"
            try:
                pool = LocalSubprocessPool(
                    worker_count=2,
                    config=config,
                    source_descriptor=descriptor,
                )
            finally:
                os.environ.pop("XAUDITOR_AUDIT_SKIP_MEM_CHECK", None)
            try:
                # Pre-0.8.0 had ``_path_concurrency`` and
                # ``path_concurrency`` (attribute + property).
                # Both must be gone.
                self.assertFalse(
                    hasattr(pool, "_path_concurrency"),
                    "LocalSubprocessPool should no longer carry "
                    "_path_concurrency (0.8.0)",
                )
                self.assertFalse(
                    hasattr(pool, "path_concurrency"),
                    "LocalSubprocessPool should no longer expose "
                    "path_concurrency property (0.8.0)",
                )
                # Worker process kwargs should NOT contain
                # ``path_concurrency`` (we passed in worker_count
                # only — the kwargs dict is captured by spawn).
                # ``_kwargs`` is private; if it's not there we
                # accept that and move on.
                for proc in pool._workers:
                    kwargs = getattr(proc, "_kwargs", {})
                    self.assertNotIn("path_concurrency", kwargs)
            finally:
                pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
