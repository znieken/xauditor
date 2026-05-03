"""End-to-end shutdown tests for the audit pipeline (phase 9 of
``fix-audit-ctrl-c-shutdown``).

Each test arms a small ``RunCancellation``, exercises one of the
long-running waits the audit pipeline owns under controlled stalled
conditions, and asserts the wait exits within the configured
deadline plus a small grace.

The tests deliberately avoid spawning real audit subprocesses (which
would require a Neo4j instance + LLM credentials) — instead they
target the helpers that the workflow uses, in isolation.
"""

from __future__ import annotations

import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit._cancellation import RunCancellation
from xauditor.audit.agents import _run_subagents_with_cancel


class StalledSubagentTests(unittest.TestCase):
    """Phase 9.2 — stalled-subagent scenario.

    Patch the analyzer subagent to ``time.sleep(60)``, set cancel,
    assert ``_run_subagents_with_cancel`` returns within ≤ 1.5 s
    via raised ``KeyboardInterrupt``.
    """

    def test_run_subagents_exits_within_one_second_of_cancel(self) -> None:
        cancellation = RunCancellation(timeout_seconds=5.0)
        pool = ThreadPoolExecutor(max_workers=2)

        def stalled_work() -> str:
            time.sleep(60)
            return "never"

        futures = [pool.submit(stalled_work) for _ in range(2)]

        # Cancel from another thread after a short delay.
        import threading

        threading.Timer(0.1, cancellation.set_cancelled).start()

        start = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            _run_subagents_with_cancel(pool, futures, cancellation)
        elapsed = time.monotonic() - start

        self.assertLess(
            elapsed,
            1.5,
            f"Expected exit within 1.5s of cancel, got {elapsed:.2f}s",
        )

    def test_run_subagents_exits_when_deadline_reached_without_explicit_cancel(
        self,
    ) -> None:
        cancellation = RunCancellation(timeout_seconds=0.3)
        cancellation.set_cancelled()  # arms the deadline
        pool = ThreadPoolExecutor(max_workers=2)

        def stalled_work() -> str:
            time.sleep(60)
            return "never"

        futures = [pool.submit(stalled_work) for _ in range(2)]

        start = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            _run_subagents_with_cancel(pool, futures, cancellation)
        elapsed = time.monotonic() - start

        # Deadline was 0.3s; helper polls every 0.5s — first iteration
        # already shows cancellation set, so should be near-instant.
        self.assertLess(
            elapsed,
            1.0,
            f"Expected exit ~immediately when deadline already reached, got {elapsed:.2f}s",
        )

    def test_run_subagents_with_no_cancellation_drains_normally(self) -> None:
        """Backward-compat: ``cancellation=None`` falls back to original
        ``as_completed`` path so unit-test code that doesn't construct a
        ``RunCancellation`` keeps working."""
        pool = ThreadPoolExecutor(max_workers=2)

        def fast_work(n: int) -> int:
            return n * 10

        futures = [pool.submit(fast_work, i) for i in range(5)]
        results = _run_subagents_with_cancel(pool, futures, None)
        # Order isn't guaranteed (as_completed), but all values are present.
        self.assertEqual(sorted(results), [0, 10, 20, 30, 40])
        pool.shutdown(wait=True)


class StalledCoderDrainTests(unittest.TestCase):
    """Phase 9.3 — stalled-coder scenario.

    Construct a ``CoderDispatcher`` with a stub agent whose ``submit``
    blocks indefinitely; arm a cancellation; assert ``drain(timeout=5)``
    exits within ≤ 5.5 s.
    """

    def test_drain_exits_when_cancellation_set_mid_poll(self) -> None:
        # Lazy import to avoid coder agent import side effects when only
        # the subagent test runs.
        from concurrent.futures import Future
        from xauditor.audit.coder import CoderDispatcher, _PendingTask

        # Construct a dispatcher with a no-op agent and inject a fake
        # pending task that never completes.
        class _FakeAgent:
            pass

        dispatcher = CoderDispatcher.__new__(CoderDispatcher)
        dispatcher.agent = _FakeAgent()  # type: ignore[assignment]
        dispatcher.concurrency = 1
        dispatcher.logger = None
        import threading

        dispatcher._lock = threading.Lock()
        dispatcher._pending = {}
        dispatcher._closed = False
        dispatcher._cancellation = None
        from concurrent.futures import ThreadPoolExecutor as _TPE

        dispatcher._executor = _TPE(max_workers=1)

        cancellation = RunCancellation(timeout_seconds=10.0)
        dispatcher.set_cancellation(cancellation)

        # Inject a pending task whose future never completes.
        forever_future: Future[object] = Future()
        dispatcher._pending["F-test"] = _PendingTask(  # type: ignore[arg-type]
            finding_id="F-test",
            future=forever_future,
        )

        # Cancel after 200 ms.
        threading.Timer(0.2, cancellation.set_cancelled).start()

        start = time.monotonic()
        # 5s timeout — but cancellation should fire much sooner.
        results = dispatcher.drain(timeout=5.0)
        elapsed = time.monotonic() - start

        self.assertEqual(results, [])
        # Should exit within 0.5s of the cancel firing (200ms + ≤ 100ms
        # poll interval response + scheduling jitter).
        self.assertLess(
            elapsed,
            1.0,
            f"Expected drain to exit within 1s of cancel, got {elapsed:.2f}s",
        )
        # Cleanup the never-completing future.
        forever_future.cancel()
        dispatcher._executor.shutdown(wait=False)

    def test_drain_honours_timeout_without_cancellation(self) -> None:
        """drain(timeout=N) returns within N + small grace even when
        no cancellation is configured."""
        from concurrent.futures import Future
        from xauditor.audit.coder import CoderDispatcher, _PendingTask
        import threading

        class _FakeAgent:
            pass

        dispatcher = CoderDispatcher.__new__(CoderDispatcher)
        dispatcher.agent = _FakeAgent()  # type: ignore[assignment]
        dispatcher.concurrency = 1
        dispatcher.logger = None
        dispatcher._lock = threading.Lock()
        dispatcher._pending = {}
        dispatcher._closed = False
        dispatcher._cancellation = None
        from concurrent.futures import ThreadPoolExecutor as _TPE

        dispatcher._executor = _TPE(max_workers=1)

        forever_future: Future[object] = Future()
        dispatcher._pending["F-test"] = _PendingTask(  # type: ignore[arg-type]
            finding_id="F-test",
            future=forever_future,
        )

        start = time.monotonic()
        results = dispatcher.drain(timeout=0.3)
        elapsed = time.monotonic() - start

        self.assertEqual(results, [])
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(
            elapsed,
            1.0,
            f"Expected drain to honour 0.3s timeout, got {elapsed:.2f}s",
        )
        forever_future.cancel()
        dispatcher._executor.shutdown(wait=False)

    def test_drain_rejects_missing_timeout(self) -> None:
        """Phase 9 / spec scenario: drain() without timeout raises TypeError."""
        from xauditor.audit.coder import CoderDispatcher

        dispatcher = CoderDispatcher.__new__(CoderDispatcher)
        with self.assertRaises(TypeError):
            dispatcher.drain()  # type: ignore[call-arg]


class WorkerPoolShortCircuitTests(unittest.TestCase):
    """Phase 9.4-equivalent: ``LocalSubprocessPool.shutdown`` collapses
    its 10 s graceful join window when cancel is observed.

    We simulate the path with a fake pool whose worker objects emulate
    ``join(timeout=...)``.
    """

    def test_shutdown_short_circuits_grace_when_cancelled(self) -> None:
        from xauditor.audit.worker_pool import LocalSubprocessPool

        # Build a pool stub by hand — full LocalSubprocessPool needs
        # multiprocessing infra. We exercise the shutdown branch with a
        # tiny fake worker.
        class _FakeWorker:
            def __init__(self) -> None:
                self.join_calls: list[float | None] = []
                self.alive = False  # join always succeeds → no escalation

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

        pool = LocalSubprocessPool.__new__(LocalSubprocessPool)
        pool._shutdown_called = False
        pool._workers = [_FakeWorker(), _FakeWorker()]  # type: ignore[assignment]

        class _FakeQ:
            def put(self, *a: object, **k: object) -> None:
                pass

            def close(self) -> None:
                pass

            def join_thread(self) -> None:
                pass

        pool._request_q = _FakeQ()  # type: ignore[assignment]
        import threading as _t

        pool._closed = _t.Event()

        class _FakeMonitor:
            def join(self, timeout: float | None = None) -> None:
                pass

        pool._monitor = _FakeMonitor()  # type: ignore[assignment]

        cancellation = RunCancellation(timeout_seconds=5.0)
        cancellation.set_cancelled()
        pool.set_cancellation(cancellation)

        pool.shutdown(wait=True)

        # Each worker's first join should have a 0.5s grace, not 10.0s.
        for worker in pool._workers:  # type: ignore[attr-defined]
            self.assertEqual(worker.join_calls[0], 0.5)

    def test_shutdown_keeps_full_grace_without_cancellation(self) -> None:
        from xauditor.audit.worker_pool import LocalSubprocessPool

        class _FakeWorker:
            def __init__(self) -> None:
                self.join_calls: list[float | None] = []
                self.alive = False

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

        pool = LocalSubprocessPool.__new__(LocalSubprocessPool)
        pool._shutdown_called = False
        pool._workers = [_FakeWorker()]  # type: ignore[assignment]

        class _FakeQ:
            def put(self, *a: object, **k: object) -> None:
                pass

            def close(self) -> None:
                pass

            def join_thread(self) -> None:
                pass

        pool._request_q = _FakeQ()  # type: ignore[assignment]
        import threading as _t

        pool._closed = _t.Event()

        class _FakeMonitor:
            def join(self, timeout: float | None = None) -> None:
                pass

        pool._monitor = _FakeMonitor()  # type: ignore[assignment]
        pool._cancellation = None

        pool.shutdown(wait=True)

        worker = pool._workers[0]  # type: ignore[attr-defined]
        self.assertEqual(worker.join_calls[0], 10.0)


if __name__ == "__main__":
    unittest.main()
