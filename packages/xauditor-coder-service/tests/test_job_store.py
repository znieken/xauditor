from __future__ import annotations

import sys
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor_coder_service.job import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    JobStore,
)


class JobStoreSubmitTests(unittest.TestCase):
    def test_first_submit_creates_a_pending_job(self) -> None:
        store = JobStore()
        job, created = store.submit("run-1::F-0001")
        self.assertTrue(created)
        self.assertEqual(job.status, STATUS_PENDING)
        self.assertEqual(job.idempotency_key, "run-1::F-0001")
        self.assertEqual(store.in_flight(), 1)

    def test_idempotent_resubmit_while_pending_returns_same_job(self) -> None:
        store = JobStore()
        first, _ = store.submit("k1")
        second, created = store.submit("k1")
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(created)

    def test_idempotent_resubmit_after_done_returns_same_job(self) -> None:
        store = JobStore()
        first, _ = store.submit("k1")
        store.mark_terminal(first.job_id, status=STATUS_DONE, result={"status": "Verified"})
        second, created = store.submit("k1")
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(created)

    def test_resubmit_after_error_creates_new_job(self) -> None:
        store = JobStore()
        first, _ = store.submit("k1")
        store.mark_terminal(first.job_id, status=STATUS_ERROR, error="boom")
        second, created = store.submit("k1")
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertTrue(created)
        # First job is gone — it was discarded by the resubmit.
        self.assertIsNone(store.get(first.job_id))

    def test_resubmit_after_cancelled_creates_new_job(self) -> None:
        store = JobStore()
        first, _ = store.submit("k1")
        store.mark_terminal(first.job_id, status=STATUS_CANCELLED, error="cancelled")
        second, created = store.submit("k1")
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertTrue(created)


class JobStoreLifecycleTests(unittest.TestCase):
    def test_mark_terminal_is_idempotent(self) -> None:
        store = JobStore()
        job, _ = store.submit("k")
        first = store.mark_terminal(job.job_id, status=STATUS_DONE, result={"ok": True})
        second = store.mark_terminal(job.job_id, status=STATUS_ERROR, error="late")
        # Second call should NOT overwrite a terminal status.
        self.assertEqual(first, second)
        self.assertEqual(second.status, STATUS_DONE)
        self.assertEqual(second.error, None)

    def test_in_flight_excludes_terminal(self) -> None:
        store = JobStore()
        a, _ = store.submit("k1")
        b, _ = store.submit("k2")
        c, _ = store.submit("k3")
        self.assertEqual(store.in_flight(), 3)
        store.mark_terminal(a.job_id, status=STATUS_DONE)
        store.mark_terminal(b.job_id, status=STATUS_CANCELLED)
        self.assertEqual(store.in_flight(), 1)
        self.assertEqual(store.get(c.job_id).status, STATUS_PENDING)

    def test_mark_terminal_with_invalid_status_raises(self) -> None:
        store = JobStore()
        job, _ = store.submit("k")
        with self.assertRaises(ValueError):
            store.mark_terminal(job.job_id, status="not-a-status")


class JobStoreTTLTests(unittest.TestCase):
    def test_reaper_evicts_stale_terminal_jobs(self) -> None:
        store = JobStore(terminal_ttl_seconds=0.0)
        a, _ = store.submit("k1")
        b, _ = store.submit("k2")
        store.mark_terminal(a.job_id, status=STATUS_DONE)
        # b is still pending, must NOT be reaped.
        reaped = store.reap_terminal(now=monotonic() + 1.0)
        self.assertIn(a.job_id, reaped)
        self.assertNotIn(b.job_id, reaped)
        self.assertIsNone(store.get(a.job_id))
        self.assertIsNotNone(store.get(b.job_id))

    def test_reaper_skips_recent_terminal(self) -> None:
        store = JobStore(terminal_ttl_seconds=3600.0)
        a, _ = store.submit("k1")
        store.mark_terminal(a.job_id, status=STATUS_DONE)
        reaped = store.reap_terminal()
        self.assertEqual(reaped, [])
        self.assertIsNotNone(store.get(a.job_id))


if __name__ == "__main__":
    unittest.main()
