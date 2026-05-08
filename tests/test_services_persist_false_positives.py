"""Tests for the FP persistence-boundary filter helper.

Covers ``short-circuit-validator-fp``: when
``audit.persist_false_positives`` is False (the default), FP findings
SHALL NOT reach ``Neo4jAdapter.persist_audit_run``. When the operator
opts in (knob = True), today's behaviour is preserved.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.models import (
    AuditRun,
    ConfidenceLevel,
    CoverageInventory,
    Finding,
    ValidationStatus,
)
from xauditor.services import _audit_run_for_persistence


def _finding(finding_id: str, status: ValidationStatus) -> Finding:
    return Finding(
        finding_id=finding_id,
        finding_name="Command Injection",
        finding_description="reaches subprocess.run(shell=True)",
        confidence_level=ConfidenceLevel.HIGH,
        source_references=(),
        analysis="analyzer notes",
        reason="reaches sink",
        context="ctx",
        business_context="bc",
        exploitation_status="ready",
        exploitation_steps="exploit steps",
        validation_status=status,
        validation_analysis="validator notes",
        path_fingerprint="path-x",
        function_names=("main",),
        suspect_function_id="main",
        suspect_line=4,
    )


def _run_with(*findings: Finding) -> AuditRun:
    return AuditRun(
        build_fingerprint="bf-x",
        findings=tuple(findings),
        coverage=CoverageInventory(),
    )


class AuditRunPersistenceFilterTests(unittest.TestCase):
    def test_knob_true_returns_run_unchanged(self) -> None:
        original = _run_with(
            _finding("F-1", ValidationStatus.VALID),
            _finding("F-2", ValidationStatus.FALSE_POSITIVE),
        )
        result = _audit_run_for_persistence(
            original, persist_false_positives=True
        )
        self.assertIs(result, original)
        self.assertEqual(len(result.findings), 2)

    def test_knob_false_drops_false_positives(self) -> None:
        original = _run_with(
            _finding("F-1", ValidationStatus.VALID),
            _finding("F-2", ValidationStatus.FALSE_POSITIVE),
            _finding("F-3", ValidationStatus.PARTIAL_VALID),
            _finding("F-4", ValidationStatus.FALSE_POSITIVE),
            _finding("F-5", ValidationStatus.INCONCLUSIVE),
        )
        result = _audit_run_for_persistence(
            original, persist_false_positives=False
        )
        self.assertEqual(
            [f.finding_id for f in result.findings], ["F-1", "F-3", "F-5"]
        )
        # In-memory original SHALL NOT be mutated.
        self.assertEqual(len(original.findings), 5)

    def test_knob_false_with_only_fps_yields_empty_findings(self) -> None:
        original = _run_with(
            _finding("F-1", ValidationStatus.FALSE_POSITIVE),
            _finding("F-2", ValidationStatus.FALSE_POSITIVE),
        )
        result = _audit_run_for_persistence(
            original, persist_false_positives=False
        )
        self.assertEqual(result.findings, ())

    def test_knob_false_with_no_fps_returns_same_findings(self) -> None:
        original = _run_with(
            _finding("F-1", ValidationStatus.VALID),
            _finding("F-2", ValidationStatus.PARTIAL_VALID),
        )
        result = _audit_run_for_persistence(
            original, persist_false_positives=False
        )
        # Same findings; build_fingerprint and coverage carried through.
        self.assertEqual(
            [f.finding_id for f in result.findings], ["F-1", "F-2"]
        )
        self.assertEqual(result.build_fingerprint, "bf-x")


if __name__ == "__main__":
    unittest.main()
