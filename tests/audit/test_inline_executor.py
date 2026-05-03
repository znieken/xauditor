"""Unit tests for ``InlineExecutor`` — the synchronous-on-master
``WorkerPool`` used when ``audit.worker_count == 1``.

Covers ``consolidate-on-worker-count`` task 2.x:
- ``submit_path(runner, index, unit)`` returns a resolved future
  carrying ``runner(index, unit)``'s value.
- Exceptions raised by the runner propagate via the future's
  ``exception()``; the executor itself does NOT raise.
- ``cancel_all()`` and ``shutdown(wait=True/False)`` are no-ops.
- The class satisfies the ``WorkerPool`` Protocol at runtime
  (``isinstance(executor, WorkerPool)`` is true).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit.worker_pool import (
    InlineExecutor,
    PathOutcome,
    PathResult,
    WorkerPool,
)


def _make_runner_returning(result: PathResult):
    def runner(index: int, unit) -> PathResult:
        del index, unit
        return result

    return runner


def _make_runner_raising(exc: BaseException):
    def runner(index: int, unit) -> PathResult:
        del index, unit
        raise exc

    return runner


class InlineExecutorTests(unittest.TestCase):
    def test_submit_path_runs_synchronously_and_returns_resolved_future(self) -> None:
        executor = InlineExecutor()
        expected = PathResult(
            index=1, path_fingerprint="fp", outcome=PathOutcome.COMPLETED
        )
        future = executor.submit_path(
            _make_runner_returning(expected), 1, object()
        )
        self.assertTrue(future.done())
        self.assertIs(future.result(timeout=0), expected)

    def test_runner_exception_propagates_through_future(self) -> None:
        executor = InlineExecutor()
        future = executor.submit_path(
            _make_runner_raising(RuntimeError("boom")), 1, object()
        )
        self.assertTrue(future.done())
        self.assertIsInstance(future.exception(timeout=0), RuntimeError)

    def test_cancel_all_is_a_noop(self) -> None:
        executor = InlineExecutor()
        # Should be safely callable any number of times, including
        # before any submit_path call.
        executor.cancel_all()
        executor.cancel_all()
        # And it must not affect a subsequent submit_path call.
        expected = PathResult(
            index=2, path_fingerprint="fp2", outcome=PathOutcome.COMPLETED
        )
        future = executor.submit_path(
            _make_runner_returning(expected), 2, object()
        )
        self.assertIs(future.result(timeout=0), expected)

    def test_shutdown_is_a_noop(self) -> None:
        executor = InlineExecutor()
        executor.shutdown(wait=True)
        executor.shutdown(wait=False)
        # Repeat after shutdown — still a no-op, the executor stays
        # usable since there's no underlying executor to shut down.
        expected = PathResult(
            index=3, path_fingerprint="fp3", outcome=PathOutcome.COMPLETED
        )
        future = executor.submit_path(
            _make_runner_returning(expected), 3, object()
        )
        self.assertIs(future.result(timeout=0), expected)

    def test_satisfies_worker_pool_protocol(self) -> None:
        # @runtime_checkable Protocol — the duck-type isinstance
        # call ensures InlineExecutor exposes the Protocol surface.
        self.assertIsInstance(InlineExecutor(), WorkerPool)


if __name__ == "__main__":
    unittest.main()
