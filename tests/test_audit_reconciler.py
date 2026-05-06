"""Tests for the cross-unit reconciler (Phase 5A).

Covers `restructure-audit-modes-and-coverage` Phase 5 task 5.7.2:

- `PassthroughReconciler` groups findings by fingerprint and
  returns each group's first finding unchanged. For Path-only
  audits (today's planner output) every group has size 1, so
  this is a no-op pass.
- `ReconciledFinding.to_payload()` projects to the JSONB shape
  persisted in `findings.reconciliation`.
- The fingerprint key matches the dedup pipeline's exact-match
  key (`finding_name`, `suspect_function_id`, `suspect_line`).

Real LLM-driven `AgenticReconciler` ships alongside the
`agentic-stage-runner-real` follow-up (same SDK choice as
Phase 4 agentic stage runner); its tests land then.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.reconciler import (
    PassthroughReconciler,
    PerUnitVerdict,
    ReconciledFinding,
    Reconciler,
)
from xauditor.models import (
    ConfidenceLevel,
    Finding,
    SourceReference,
    ValidationStatus,
)


def _finding(
    *,
    finding_name: str = "SQLi in handler",
    suspect_line: int = 42,
    suspect_function_id: str = "fn::handler",
    finding_id: str = "F-0001",
    validation_status: ValidationStatus = ValidationStatus.VALID,
    validation_analysis: str = "Confirmed by path-unit analyzer",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        finding_name=finding_name,
        finding_description="user input flows into raw SQL",
        confidence_level=ConfidenceLevel.HIGH,
        source_references=(
            SourceReference(
                file_path="src/app.py",
                start_line=0,
                end_line=0,
                focus_lines=(),
                language="python",
                snippet="db.exec(payload)",
            ),
        ),
        analysis="path walkthrough",
        reason="payload not parameterised",
        context="entry → handler",
        business_context="login endpoint",
        exploitation_status="exploitable",
        exploitation_steps="step 1 ...",
        validation_status=validation_status,
        validation_analysis=validation_analysis,
        path_fingerprint="fp::path",
        function_names=("handler",),
        suspect_function_id=suspect_function_id,
        suspect_line=suspect_line,
    )


class PassthroughReconcilerTests(unittest.TestCase):
    def test_satisfies_protocol(self) -> None:
        # Reconciler is a Protocol — runtime check via duck typing.
        runner = PassthroughReconciler()
        self.assertTrue(hasattr(runner, "reconcile"))

    def test_single_unit_group_returns_finding_unchanged(self) -> None:
        f = _finding()
        result = PassthroughReconciler().reconcile([f])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0].finding, f)
        self.assertEqual(result[0].consolidated_verdict, "Valid")
        self.assertEqual(result[0].consolidation_reasoning, "")
        # One PerUnitVerdict referring back to the source finding.
        self.assertEqual(len(result[0].per_unit_verdicts), 1)
        self.assertEqual(result[0].per_unit_verdicts[0].verdict, "Valid")

    def test_multi_unit_findings_grouped_by_fingerprint(self) -> None:
        # Two findings sharing (name, function_id, line) fingerprint
        # represent the same underlying issue surfaced from two
        # unit kinds (a future possibility once Sink/Entry units
        # light up). Phase 5A's passthrough groups them.
        f1 = _finding(
            finding_id="F-PATH",
            validation_status=ValidationStatus.VALID,
            validation_analysis="path-unit verdict",
        )
        f2 = _finding(
            finding_id="F-SINK",
            validation_status=ValidationStatus.PARTIAL_VALID,
            validation_analysis="sink-unit verdict",
        )
        result = PassthroughReconciler().reconcile([f1, f2])
        self.assertEqual(len(result), 1)
        # First finding is the canonical one; both verdicts surface.
        self.assertEqual(result[0].finding.finding_id, "F-PATH")
        self.assertEqual(len(result[0].per_unit_verdicts), 2)
        self.assertEqual(result[0].per_unit_verdicts[0].verdict, "Valid")
        self.assertEqual(result[0].per_unit_verdicts[1].verdict, "Partial Valid")

    def test_distinct_findings_not_grouped(self) -> None:
        f1 = _finding(finding_name="SQLi", suspect_line=10)
        f2 = _finding(finding_name="SQLi", suspect_line=42)
        f3 = _finding(finding_name="XSS", suspect_line=10)
        result = PassthroughReconciler().reconcile([f1, f2, f3])
        # Three distinct fingerprints → three reconciled findings.
        self.assertEqual(len(result), 3)

    def test_empty_input_returns_empty_tuple(self) -> None:
        self.assertEqual(PassthroughReconciler().reconcile([]), ())

    def test_fingerprint_is_case_insensitive_on_finding_name(self) -> None:
        f1 = _finding(finding_name="SQLi", finding_id="F-1")
        f2 = _finding(finding_name="sqli", finding_id="F-2")
        result = PassthroughReconciler().reconcile([f1, f2])
        # Same fingerprint despite different case → grouped.
        self.assertEqual(len(result), 1)


class ReconciledFindingPayloadTests(unittest.TestCase):
    def test_to_payload_renders_jsonb_compatible_shape(self) -> None:
        per_unit = (
            PerUnitVerdict(
                unit_kind="path",
                unit_id="fp::path",
                verdict="Valid",
                analysis="path verdict",
            ),
            PerUnitVerdict(
                unit_kind="sink",
                unit_id="sink::db.execute",
                verdict="Refuted",
                analysis="sink verdict",
            ),
        )
        rec = ReconciledFinding(
            finding=_finding(),
            per_unit_verdicts=per_unit,
            consolidated_verdict="Partial Valid",
            consolidation_reasoning="Sink unit refuted; downgraded.",
        )
        payload = rec.to_payload()
        self.assertEqual(
            set(payload.keys()),
            {"per_unit_verdicts", "consolidated_verdict", "consolidation_reasoning"},
        )
        self.assertIsInstance(payload["per_unit_verdicts"], list)
        self.assertEqual(len(payload["per_unit_verdicts"]), 2)
        self.assertEqual(
            payload["per_unit_verdicts"][1]["unit_kind"], "sink"
        )
        self.assertEqual(payload["consolidated_verdict"], "Partial Valid")


if __name__ == "__main__":
    unittest.main()
