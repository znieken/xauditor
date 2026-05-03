"""Regression guard for the per-row UPSERT streaming pattern.

Skipped by default; opt in with ``XAUDITOR_RUN_PERF_BENCH=1`` so the
benchmark only runs when an operator is actively investigating sink
write counts.

What this guards: the 0.4.10 streaming path emitted a full ``AuditRun``
snapshot on every coder settle, which translated to ``O(N)`` Postgres
UPSERTs per settle and ``O(N**2)`` over a run of N findings. The 0.4.11
``upsert_finding`` path replaces that with one per-row UPSERT per
settle. This test drives 500 settles through the workflow's streaming
callback against an instrumented sink and asserts the resulting write
shape is per-row, not full-snapshot, so a future regression cannot
silently reintroduce the quadratic pattern.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.coder import CoderResult
from xauditor.models import (
    CODER_STATUS_PENDING,
    CODER_STATUS_VERIFIED,
    ConfidenceLevel,
    Finding,
    ValidationStatus,
)


def _stub_finding(finding_id: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        finding_name="x",
        finding_description="x",
        confidence_level=ConfidenceLevel.HIGH,
        source_references=(),
        analysis="",
        reason="",
        context="",
        business_context="",
        exploitation_status="",
        exploitation_steps="",
        validation_status=ValidationStatus.VALID,
        validation_analysis="",
        path_fingerprint="p",
        coder_status=CODER_STATUS_PENDING,
    )


@unittest.skipUnless(
    os.environ.get("XAUDITOR_RUN_PERF_BENCH"),
    "Perf bench is gated on XAUDITOR_RUN_PERF_BENCH=1",
)
class StreamingUpsertCallShapeBench(unittest.TestCase):
    def test_500_settle_storm_writes_per_row_not_per_snapshot(self) -> None:
        # We don't need a real graph build — directly exercise
        # ``_make_stream_callback`` on a workflow shell. The stream
        # callback uses only ``self._cancelling`` and the closure args,
        # so a minimally-initialised workflow is enough.
        from xauditor.audit.workflow import AuditWorkflow

        workflow = AuditWorkflow.__new__(AuditWorkflow)
        import threading

        workflow._cancelling = threading.Event()
        workflow.logger = None

        n = 500
        findings: list[Finding] = [_stub_finding(f"F-{i:04d}") for i in range(n)]
        pending: set[str] = {f.finding_id for f in findings}

        upsert_count = {"n": 0}
        snapshot_emits = {"n": 0}

        def _on_finding_upsert(_finding: Finding) -> None:
            upsert_count["n"] += 1

        def _on_progress(_snapshot) -> None:
            snapshot_emits["n"] += 1

        def _snapshot_factory():
            # The post-fix path SHALL NOT call this. Increment so a
            # regression that re-enables the snapshot path is caught.
            snapshot_emits["n"] += 1

            class _S:
                pass

            return _S()

        callback = workflow._make_stream_callback(
            findings=findings,
            pending_coder=pending,
            on_progress=_on_progress,
            snapshot_factory=_snapshot_factory,
            on_finding_upsert=_on_finding_upsert,
        )

        for finding in findings:
            callback(
                finding.finding_id,
                CoderResult(
                    status=CODER_STATUS_VERIFIED,
                    analysis="ok",
                    reason="ok",
                    evidence=(),
                    cli_exit_code=0,
                    cli_stderr=None,
                    duration_ms=10,
                ),
            )

        # O(N): one upsert per settle, exactly N total.
        self.assertEqual(upsert_count["n"], n)
        # The legacy snapshot emit path SHALL NOT fire when
        # ``on_finding_upsert`` is supplied (otherwise we are back to
        # O(N^2) writes downstream).
        self.assertEqual(snapshot_emits["n"], 0)


if __name__ == "__main__":
    unittest.main()
