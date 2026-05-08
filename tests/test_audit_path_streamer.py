"""Unit tests for ``audit/streamer.py``.

Covers ``audit-stream-path-loading`` D5/D6 contracts:

- The fetcher yields ``AuditUnit`` instances in
  ``path_fingerprint`` order across batches.
- Per-fingerprint dedup is preserved exactly as the eager
  ``plan_audit_paths`` did.
- A fetcher exception surfaces in the consumer's thread on the
  next ``next_or_none()`` (no silent drop).
- ``close(wait=True)`` joins the fetcher cleanly even when the
  consumer abandons mid-stream.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Avoid the slow heavy-import path; we only need the source's path
# surface. This keeps the test fast and side-effect-free.
sys.path.insert(0, str(Path(__file__).parent))

from xauditor.audit.streamer import EagerPathStreamer, PathStreamer  # noqa: E402
from xauditor.models import AuditUnit, PathRecord  # noqa: E402


def _make_path(idx: int, *, prefix: str = "p") -> PathRecord:
    return PathRecord(
        entry_function=f"entry_{idx}",
        function_names=(f"f_{idx}",),
        file_paths=(f"file_{idx}.py",),
        path_fingerprint=f"{prefix}_{idx:05d}",
        function_ids=(f"fn_{idx}",),
        business_context=f"ctx_{idx}",
        trust_boundary=f"tb_{idx}",
    )


class _FakeSource:
    """Minimal duck-typed source for streamer tests.

    Real ``_TestOnlyGraphSource`` works too but pulls a heavy import
    closure; this stub keeps the test sub-second.
    """

    def __init__(
        self,
        paths: list[PathRecord],
        *,
        page_size_observed: list[int] | None = None,
        raise_at: int | None = None,
    ) -> None:
        self._paths = paths
        self._page_size_observed = page_size_observed
        self._raise_at = raise_at

    def iter_paths(self, *, page_size: int = 100):
        if self._page_size_observed is not None:
            self._page_size_observed.append(page_size)
        for i, path in enumerate(self._paths):
            if self._raise_at is not None and i >= self._raise_at:
                raise RuntimeError(f"injected at index {i}")
            yield path

    def path_count(self) -> int:
        return len(self._paths)


class PathStreamerHappyPathTests(unittest.TestCase):
    def _drain(self, streamer):
        out: list[AuditUnit] = []
        while True:
            unit = streamer.next_or_none()
            if unit is None:
                break
            out.append(unit)
        return out

    def test_yields_every_path_in_order_across_batches(self) -> None:
        paths = [_make_path(i) for i in range(250)]
        source = _FakeSource(paths)
        streamer = PathStreamer(source, batch_size=50)
        streamer.open()
        try:
            units = self._drain(streamer)
        finally:
            streamer.close()
        self.assertEqual(len(units), 250)
        self.assertEqual(
            [u.path.path_fingerprint for u in units],
            [p.path_fingerprint for p in paths],
        )
        self.assertEqual(streamer.total, 250)
        self.assertEqual(streamer.yielded, 250)
        self.assertTrue(streamer.exhausted)

    def test_page_size_is_propagated_to_source(self) -> None:
        observed: list[int] = []
        paths = [_make_path(i) for i in range(10)]
        streamer = PathStreamer(
            _FakeSource(paths, page_size_observed=observed), batch_size=2000
        )
        streamer.open()
        try:
            self._drain(streamer)
        finally:
            streamer.close()
        self.assertEqual(observed, [2000])

    def test_dedup_filters_duplicate_fingerprints(self) -> None:
        # Two distinct rows share fingerprint p_00007 — only the
        # first occurrence reaches the consumer (parity with the
        # eager ``plan_audit_paths`` body).
        paths = [_make_path(i) for i in range(10)]
        paths.append(_make_path(7, prefix="p"))  # duplicate of paths[7]
        streamer = PathStreamer(_FakeSource(paths), batch_size=4)
        streamer.open()
        try:
            units = self._drain(streamer)
        finally:
            streamer.close()
        self.assertEqual(len(units), 10)
        seen_count = sum(
            1 for u in units if u.path.path_fingerprint == "p_00007"
        )
        self.assertEqual(seen_count, 1)


class PathStreamerErrorTests(unittest.TestCase):
    def test_fetcher_exception_surfaces_on_next(self) -> None:
        paths = [_make_path(i) for i in range(20)]
        source = _FakeSource(paths, raise_at=5)
        streamer = PathStreamer(source, batch_size=4)
        streamer.open()
        try:
            collected = []
            with self.assertRaises(RuntimeError) as ctx:
                while True:
                    unit = streamer.next_or_none()
                    if unit is None:
                        break
                    collected.append(unit)
            self.assertIn("injected at index 5", str(ctx.exception))
            # Streamer marks itself exhausted after raising.
            self.assertTrue(streamer.exhausted)
            self.assertIsNone(streamer.next_or_none())
        finally:
            streamer.close()


class PathStreamerCloseTests(unittest.TestCase):
    def test_close_wait_true_joins_fetcher_when_consumer_abandons(self) -> None:
        # 1000 paths but the consumer only pulls 3, then closes.
        # Without proper close semantics the fetcher would block on
        # ``queue.put(block=True)`` forever; we want it to observe
        # the stop flag and exit cleanly.
        paths = [_make_path(i) for i in range(1000)]
        streamer = PathStreamer(_FakeSource(paths), batch_size=8)
        streamer.open()
        for _ in range(3):
            streamer.next_or_none()
        # Capture the fetcher thread id; it must be gone after
        # ``close(wait=True)`` returns.
        fetcher = streamer._thread  # noqa: SLF001 - test introspection
        assert fetcher is not None
        streamer.close(wait=True)
        self.assertFalse(fetcher.is_alive())

    def test_close_wait_false_returns_immediately(self) -> None:
        paths = [_make_path(i) for i in range(1000)]
        streamer = PathStreamer(_FakeSource(paths), batch_size=8)
        streamer.open()
        # Don't drain at all; ``close(wait=False)`` must not block.
        streamer.close(wait=False)
        self.assertTrue(streamer.exhausted)

    def test_close_before_open_is_safe(self) -> None:
        paths = [_make_path(i) for i in range(3)]
        streamer = PathStreamer(_FakeSource(paths), batch_size=4)
        streamer.close(wait=True)  # no thread started; should not raise
        self.assertTrue(streamer.exhausted)


class PathStreamerInputValidationTests(unittest.TestCase):
    def test_invalid_batch_size_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PathStreamer(_FakeSource([]), batch_size=0)

    def test_total_before_open_raises(self) -> None:
        streamer = PathStreamer(_FakeSource([]), batch_size=1)
        with self.assertRaises(RuntimeError):
            _ = streamer.total

    def test_double_open_rejected(self) -> None:
        streamer = PathStreamer(_FakeSource([_make_path(0)]), batch_size=1)
        streamer.open()
        try:
            with self.assertRaises(RuntimeError):
                streamer.open()
        finally:
            streamer.close()


class EagerPathStreamerTests(unittest.TestCase):
    """Sanity tests for the in-memory adapter used by tests + the
    legacy ``workflow.run(plan=...)`` shim."""

    def test_drains_in_order_no_threading(self) -> None:
        units = tuple(
            AuditUnit(path=_make_path(i), function_ids=(f"fn_{i}",))
            for i in range(5)
        )
        eager = EagerPathStreamer(units)
        self.assertEqual(eager.total, 5)
        self.assertEqual(eager.batch_size, 5)
        self.assertFalse(eager.exhausted)
        out: list[AuditUnit] = []
        while True:
            u = eager.next_or_none()
            if u is None:
                break
            out.append(u)
        self.assertEqual(out, list(units))
        self.assertTrue(eager.exhausted)
        self.assertEqual(eager.yielded, 5)

    def test_close_is_idempotent(self) -> None:
        eager = EagerPathStreamer(tuple())
        eager.close()
        eager.close()
        self.assertTrue(eager.exhausted)


if __name__ == "__main__":
    unittest.main()
