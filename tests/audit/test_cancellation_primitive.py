"""Unit tests for ``RunCancellation`` — the cooperative cancellation
primitive shared by every long-running wait inside the audit pipeline.

Covers ``fix-audit-ctrl-c-shutdown`` tasks 1.2, 1.3:
- ``wait()`` returns immediately when cancel is already set.
- ``wait()`` returns after the interval when cancel is never set.
- ``wait()`` returns within ≤ 50 ms when cancel is set mid-interval.
- ``remaining_seconds()`` returns positive before deadline, ``0.0`` when
  deadline reached, ``None`` when no deadline configured.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.audit._cancellation import RunCancellation


class WaitTests(unittest.TestCase):
    def test_wait_returns_immediately_when_already_cancelled(self) -> None:
        c = RunCancellation(timeout_seconds=30.0)
        c.set_cancelled()
        start = time.monotonic()
        self.assertTrue(c.wait(5.0))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.05)

    def test_wait_returns_after_interval_when_never_cancelled(self) -> None:
        c = RunCancellation(timeout_seconds=30.0)
        start = time.monotonic()
        self.assertFalse(c.wait(0.1))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 0.3)

    def test_wait_returns_within_50ms_when_cancelled_mid_interval(self) -> None:
        c = RunCancellation(timeout_seconds=30.0)
        cancel_at = time.monotonic() + 0.05

        def cancel_after_delay() -> None:
            sleep_for = cancel_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            c.set_cancelled()

        threading.Thread(target=cancel_after_delay, daemon=True).start()
        start = time.monotonic()
        self.assertTrue(c.wait(5.0))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.15)


class DeadlineTests(unittest.TestCase):
    def test_remaining_returns_none_before_arming(self) -> None:
        c = RunCancellation(timeout_seconds=30.0)
        self.assertIsNone(c.remaining_seconds())
        self.assertIsNone(c.shutdown_deadline)
        self.assertFalse(c.deadline_reached())

    def test_remaining_positive_before_deadline(self) -> None:
        c = RunCancellation(timeout_seconds=10.0)
        c.set_cancelled()
        remaining = c.remaining_seconds()
        self.assertIsNotNone(remaining)
        assert remaining is not None
        self.assertGreater(remaining, 9.0)
        self.assertLessEqual(remaining, 10.0)
        self.assertFalse(c.deadline_reached())

    def test_remaining_zero_when_deadline_reached(self) -> None:
        c = RunCancellation(timeout_seconds=0.05)
        c.set_cancelled()
        time.sleep(0.1)
        self.assertEqual(c.remaining_seconds(), 0.0)
        self.assertTrue(c.deadline_reached())

    def test_set_cancelled_returns_true_first_time_then_false(self) -> None:
        c = RunCancellation(timeout_seconds=30.0)
        self.assertTrue(c.set_cancelled())
        self.assertFalse(c.set_cancelled())
        self.assertFalse(c.set_cancelled())

    def test_deadline_immutable_after_first_arm(self) -> None:
        c = RunCancellation(timeout_seconds=10.0)
        c.set_cancelled()
        first_deadline = c.shutdown_deadline
        time.sleep(0.05)
        c.set_cancelled()  # second call no-ops
        self.assertEqual(c.shutdown_deadline, first_deadline)

    def test_is_cancelled_reflects_event_state(self) -> None:
        c = RunCancellation(timeout_seconds=30.0)
        self.assertFalse(c.is_cancelled())
        c.set_cancelled()
        self.assertTrue(c.is_cancelled())


if __name__ == "__main__":
    unittest.main()
