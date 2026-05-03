"""Per-path claim/route protocol unit tests.

Covers ``support-neo4j-source-in-subprocess-pool`` task 3.x — the
master-side orphan-routing logic that closes Phase 3 ``tasks/6.5``.
The full integration path (real ``multiprocessing.spawn`` + a
worker that crashes mid-claim) is operator-side (task V2 / V4 in
the spec); these tests exercise the master's bookkeeping in
isolation by constructing a ``LocalSubprocessPool`` via
``__new__`` and wiring the relevant fields by hand. That keeps
test runtime sub-second and avoids the
"how do I make a real worker crash deterministically without a
real Neo4j" infrastructure problem.

Updated by ``flatten-path-concurrency-into-worker-count`` (0.8.0):
``_claims`` is now ``dict[str, int]`` keyed by worker_id (D2),
and a worker holds at most ONE in-flight claim at any instant
(D3). Tests that previously had a single worker owning multiple
paths are split into per-worker fixtures.
"""

from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit.worker_pool import (
    LocalSubprocessPool,
    PathOutcome,
    PathResult,
)


@dataclass
class _FakeWorker:
    """Drop-in for ``mp.Process`` covering the attributes
    ``LocalSubprocessPool._check_dead_workers`` reads:
    ``name``, ``is_alive()``, ``exitcode``."""

    name: str
    _alive: bool = True
    exitcode: int | None = None

    def is_alive(self) -> bool:
        return self._alive

    def die(self, *, exitcode: int = -9) -> None:
        self._alive = False
        self.exitcode = exitcode


def _make_pool_with_fake_workers(
    *, worker_names: list[str]
) -> LocalSubprocessPool:
    """Build a ``LocalSubprocessPool`` instance bypassing ``__init__``
    so the fake workers don't actually spawn. Wires the fields the
    routing logic reads."""
    pool = LocalSubprocessPool.__new__(LocalSubprocessPool)
    pool._workers = [_FakeWorker(name=n) for n in worker_names]
    pool._futures = {}
    pool._claims = {}
    pool._futures_lock = threading.Lock()
    pool._dead_workers_seen = set()
    pool._closed = threading.Event()
    pool._shutdown_called = True  # never invoke real shutdown
    return pool


class CheckDeadWorkersTests(unittest.TestCase):
    def test_orphan_routes_to_worker_crashed_when_owner_dies(self) -> None:
        # Two workers, each holding one in-flight claim (D2: at
        # most one claim per worker since 0.8.0). Worker 0 dies →
        # only its claimed path orphans.
        pool = _make_pool_with_fake_workers(
            worker_names=["audit-worker-0", "audit-worker-1"]
        )
        f1: Future[PathResult] = Future()
        f2: Future[PathResult] = Future()
        pool._futures = {1: f1, 2: f2}
        pool._claims = {
            "audit-worker-0": 1,
            "audit-worker-1": 2,
        }

        worker0 = pool._workers[0]
        assert isinstance(worker0, _FakeWorker)
        worker0.die(exitcode=-9)

        pool._check_dead_workers()

        # Path 1 resolves with WORKER_CRASHED.
        self.assertTrue(f1.done(), "future 1 should be resolved")
        result = f1.result()
        self.assertEqual(result.outcome, PathOutcome.WORKER_CRASHED)
        self.assertEqual(result.index, 1)
        self.assertIn("audit-worker-0", result.error_info["reason"])
        self.assertEqual(result.error_info["worker_id"], "audit-worker-0")
        self.assertEqual(result.error_info["exit_code"], "-9")

        # Path 2's future is untouched (worker 1 still alive).
        self.assertFalse(f2.done())
        # Bookkeeping: orphaned worker_id and its future are
        # popped; the survivor stays.
        self.assertNotIn("audit-worker-0", pool._claims)
        self.assertIn("audit-worker-1", pool._claims)
        self.assertNotIn(1, pool._futures)
        self.assertIn(2, pool._futures)

    def test_idempotent_across_ticks(self) -> None:
        pool = _make_pool_with_fake_workers(
            worker_names=["audit-worker-0"]
        )
        f1: Future[PathResult] = Future()
        pool._futures = {1: f1}
        pool._claims = {"audit-worker-0": 1}
        worker = pool._workers[0]
        assert isinstance(worker, _FakeWorker)
        worker.die(exitcode=137)

        pool._check_dead_workers()
        self.assertTrue(f1.done())
        self.assertEqual(f1.result().outcome, PathOutcome.WORKER_CRASHED)

        # A second tick after the same worker is still dead must
        # not touch the (already-resolved) future or attempt to
        # re-resolve it. ``_dead_workers_seen`` is the gate.
        pool._check_dead_workers()
        # No exception, future state unchanged.
        self.assertTrue(f1.done())

    def test_alive_workers_never_route_orphans(self) -> None:
        pool = _make_pool_with_fake_workers(
            worker_names=["audit-worker-0", "audit-worker-1"]
        )
        f1: Future[PathResult] = Future()
        pool._futures = {1: f1}
        pool._claims = {"audit-worker-0": 1}

        # No worker has died.
        pool._check_dead_workers()
        self.assertFalse(f1.done())
        self.assertIn("audit-worker-0", pool._claims)

    def test_dead_worker_with_no_claims_is_safe(self) -> None:
        pool = _make_pool_with_fake_workers(
            worker_names=["audit-worker-0"]
        )
        worker = pool._workers[0]
        assert isinstance(worker, _FakeWorker)
        worker.die(exitcode=0)

        # No futures or claims — must not crash, must not loop.
        pool._check_dead_workers()
        self.assertEqual(pool._futures, {})
        self.assertEqual(pool._claims, {})
        self.assertIn("audit-worker-0", pool._dead_workers_seen)


class FailOutstandingTests(unittest.TestCase):
    def test_clears_claims_alongside_futures(self) -> None:
        # Two workers, each holding a claim. ``_fail_outstanding``
        # is the master-side bulk fallback path used when the
        # "all workers dead" detector fires. It must clear both
        # the claims dict and the futures dict so no stale state
        # lingers.
        pool = _make_pool_with_fake_workers(
            worker_names=["w0", "w1"]
        )
        f1: Future[PathResult] = Future()
        f2: Future[PathResult] = Future()
        pool._futures = {1: f1, 2: f2}
        pool._claims = {"w0": 1, "w1": 2}

        pool._fail_outstanding("all subprocess workers exited")

        self.assertTrue(f1.done())
        self.assertTrue(f2.done())
        self.assertEqual(f1.result().outcome, PathOutcome.WORKER_CRASHED)
        self.assertEqual(f2.result().outcome, PathOutcome.WORKER_CRASHED)
        self.assertEqual(pool._futures, {})
        self.assertEqual(pool._claims, {})


if __name__ == "__main__":
    unittest.main()
