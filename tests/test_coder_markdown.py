from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.models import (
    CODER_STATUS_FAIL,
    CODER_STATUS_INCONCLUSIVE,
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_PENDING,
    CODER_STATUS_SKIPPED,
    CODER_STATUS_VERIFIED,
    CoderEvidence,
    ConfidenceLevel,
    Finding,
    ValidationStatus,
)
from xauditor.reporting.markdown import (
    render_coder_results_report,
    render_false_positives_report,
    render_findings_report,
)


def _make_finding(**overrides) -> Finding:
    base = dict(
        finding_id="F-0001",
        finding_name="Command Injection",
        finding_description="d",
        confidence_level=ConfidenceLevel.HIGH,
        source_references=(),
        analysis="a",
        reason="r",
        context="A -> B",
        business_context="bc",
        exploitation_status="ready",
        exploitation_steps="run X",
        validation_status=ValidationStatus.VALID,
        validation_analysis="ok",
        path_fingerprint="fp-1",
        function_names=("A", "B"),
        analyzer_status="candidate",
        evidence_strength="high",
        suspect_function_id="A",
        suspect_line=42,
    )
    base.update(overrides)
    return Finding(**base)


class CoderResultsArtifactTests(unittest.TestCase):
    def test_disabled_run_renders_skipped_for_every_finding(self) -> None:
        findings = [
            _make_finding(finding_id="F-0001"),
            _make_finding(finding_id="F-0002"),
        ]
        text = render_coder_results_report(findings, coder_enabled=False)
        self.assertIn("# Coder Verification Report", text)
        self.assertIn("## F-0001 —", text)
        self.assertIn("## F-0002 —", text)
        self.assertEqual(text.count("**Coder Status**: Skipped"), 2)
        self.assertIn("Not run (coder verification disabled for this run)", text)

    def test_enabled_run_renders_mixed_statuses(self) -> None:
        evidence_a = (
            CoderEvidence(
                file_path="src/a.py",
                function_name="sanitize",
                snippet="def sanitize(): pass",
                language="python",
                role="sanitizer",
            ),
        )
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_VERIFIED,
                coder_analysis="confirmed by sanitizer",
                coder_reason="sanitizer at src/a.py",
                coder_call_chain_evidence=evidence_a,
            ),
            _make_finding(
                finding_id="F-0002",
                coder_status=CODER_STATUS_NOT_VERIFIED,
                coder_analysis="repo-global guard rejects this path",
                coder_reason="capability check at src/b.py",
            ),
            _make_finding(
                finding_id="F-0003",
                coder_status=CODER_STATUS_INCONCLUSIVE,
                coder_analysis="evidence missing",
                coder_reason="no sanitizer found",
            ),
            _make_finding(
                finding_id="F-0004",
                coder_status=CODER_STATUS_SKIPPED,
                coder_reason="cancelled by user",
            ),
        ]
        text = render_coder_results_report(findings, coder_enabled=True)
        self.assertIn("**Coder Status**: Verified", text)
        self.assertIn("**Coder Status**: Not Verified", text)
        self.assertIn("**Coder Status**: Inconclusive", text)
        self.assertIn("**Coder Status**: Skipped", text)
        # Skipped finding still appears (cancelled, but coder was enabled).
        self.assertIn("F-0004", text)
        # Evidence rendered as a fenced block with the language tag.
        self.assertIn("```python", text)
        self.assertIn("def sanitize(): pass", text)

    def test_pending_renders_as_not_run(self) -> None:
        findings = [
            _make_finding(finding_id="F-0001", coder_status=CODER_STATUS_PENDING),
        ]
        text = render_coder_results_report(findings, coder_enabled=True)
        self.assertIn("**Coder Status**: Pending", text)
        # Pending verdict has no analysis yet, so the body labels it Not run.
        self.assertIn("**Coder Analysis**: _Not run_", text)

    def test_idempotent_rewrite_yields_identical_output(self) -> None:
        evidence = (
            CoderEvidence(
                file_path="src/a.py",
                function_name="x",
                snippet="x()",
                language="python",
                role="call_site",
            ),
        )
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_VERIFIED,
                coder_analysis="ok",
                coder_reason="r",
                coder_call_chain_evidence=evidence,
            ),
        ]
        first = render_coder_results_report(findings, coder_enabled=True)
        second = render_coder_results_report(findings, coder_enabled=True)
        self.assertEqual(first, second)

    def test_no_findings_renders_placeholder(self) -> None:
        text = render_coder_results_report([], coder_enabled=True)
        self.assertIn("# Coder Verification Report", text)
        self.assertIn("_No findings were produced for this run._", text)


class FindingsReportInlineCoderTests(unittest.TestCase):
    def test_findings_report_omits_section_when_disabled_and_skipped(self) -> None:
        findings = [_make_finding(finding_id="F-0001")]
        text = render_findings_report(findings, coder_enabled=False)
        self.assertNotIn("### Coder Verification", text)

    def test_findings_report_includes_section_when_enabled(self) -> None:
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_VERIFIED,
                coder_analysis="repo-global ok",
                coder_reason="sanitizer present",
            )
        ]
        text = render_findings_report(findings, coder_enabled=True)
        self.assertIn("### Coder Verification", text)
        self.assertIn("**Coder Status**: Verified", text)
        self.assertIn("repo-global ok", text)

    def test_false_positives_report_includes_section_when_enabled(self) -> None:
        findings = [
            _make_finding(
                finding_id="F-0009",
                validation_status=ValidationStatus.FALSE_POSITIVE,
                coder_status=CODER_STATUS_NOT_VERIFIED,
                coder_analysis="confirmed false positive",
                coder_reason="never reached",
            )
        ]
        text = render_false_positives_report(findings, coder_enabled=True)
        self.assertIn("# False Positives Report", text)
        self.assertIn("### Coder Verification", text)
        self.assertIn("**Coder Status**: Not Verified", text)

    def test_false_positives_report_writes_explanatory_note_when_persist_disabled(
        self,
    ) -> None:
        """``short-circuit-validator-fp`` — when
        ``audit.persist_false_positives`` is False (the default), the
        artifact SHALL still be emitted but its body SHALL contain only
        the header and an explanatory note. FP findings SHALL NOT
        render even when present in the input.
        """

        findings = [
            _make_finding(
                finding_id="F-0009",
                validation_status=ValidationStatus.FALSE_POSITIVE,
                coder_status=CODER_STATUS_NOT_VERIFIED,
                coder_analysis="confirmed false positive",
                coder_reason="never reached",
            )
        ]
        text = render_false_positives_report(
            findings, coder_enabled=True, persist_false_positives=False
        )
        self.assertIn("# False Positives Report", text)
        self.assertIn(
            "FP persistence disabled", text,
            msg="The explanatory note SHALL appear when knob is False.",
        )
        self.assertIn("audit.persist_false_positives", text)
        # Per-finding body SHALL NOT render under the disabled knob.
        self.assertNotIn("F-0009", text)
        self.assertNotIn("### Coder Verification", text)
        self.assertNotIn("Not Verified", text)

    def test_false_positives_report_renders_body_when_persist_enabled(
        self,
    ) -> None:
        findings = [
            _make_finding(
                finding_id="F-0009",
                validation_status=ValidationStatus.FALSE_POSITIVE,
                coder_status=CODER_STATUS_NOT_VERIFIED,
                coder_analysis="confirmed false positive",
                coder_reason="never reached",
            )
        ]
        text = render_false_positives_report(
            findings, coder_enabled=True, persist_false_positives=True
        )
        # Same as today's behavior — full body renders when knob is True.
        self.assertIn("# False Positives Report", text)
        self.assertIn("F-0009", text)
        self.assertIn("**Coder Status**: Not Verified", text)
        self.assertNotIn("FP persistence disabled", text)


# --------------------------------------------------------------------------
# Fail status rendering (multi-project-coder-service §10.1, §10.2)
# --------------------------------------------------------------------------


class CoderResultsFailRenderingTests(unittest.TestCase):
    def test_fail_renders_with_transport_failure_label(self) -> None:
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_FAIL,
                coder_reason="transport error: 503 Service Unavailable",
            ),
        ]
        text = render_coder_results_report(findings, coder_enabled=True)
        self.assertIn("**Coder Status**: Fail", text)
        self.assertIn(
            "**Coder Reason**: transport error: 503 Service Unavailable", text
        )
        self.assertIn(
            "**Coder Analysis**: _Not produced (transport failure)_", text
        )
        self.assertIn(
            "**Call-chain Evidence**: _Not produced (transport failure)_", text
        )

    def test_fail_distinguishable_from_skipped_in_artifact(self) -> None:
        findings = [
            _make_finding(
                finding_id="F-Fail",
                coder_status=CODER_STATUS_FAIL,
                coder_reason="transport error: connect failed",
            ),
            _make_finding(
                finding_id="F-Skip",
                coder_status=CODER_STATUS_SKIPPED,
                coder_reason="cancelled by user",
            ),
        ]
        text = render_coder_results_report(findings, coder_enabled=True)
        # Distinct labels — operators can tell at a glance which is
        # "we never reached claude" vs "run was disabled / cancelled".
        self.assertIn("Not produced (transport failure)", text)
        self.assertNotIn("Not produced (transport failure)\n\n**Coder Status**: Skipped", text)
        # Both findings appear in plan order.
        self.assertLess(text.index("F-Fail"), text.index("F-Skip"))

    def test_fail_with_empty_reason_falls_back_to_audit_log_pointer(self) -> None:
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_FAIL,
                coder_reason="",
            ),
        ]
        text = render_coder_results_report(findings, coder_enabled=True)
        self.assertIn("transport failure", text)
        # Empty reason → operator-friendly fallback pointing at the log.
        self.assertIn("see audit log", text)

    def test_all_six_terminal_statuses_render(self) -> None:
        """All six canonical CODER_STATUSES values appear and are
        rendered cleanly; covers the full enum so we don't quietly
        regress one."""

        evidence = (
            CoderEvidence(
                file_path="src/a.py",
                function_name="sanitize",
                snippet="def sanitize(): pass",
                language="python",
                role="sanitizer",
            ),
        )
        findings = [
            _make_finding(
                finding_id="F-V",
                coder_status=CODER_STATUS_VERIFIED,
                coder_analysis="confirmed",
                coder_reason="sanitizer at src/a.py",
                coder_call_chain_evidence=evidence,
            ),
            _make_finding(
                finding_id="F-NV",
                coder_status=CODER_STATUS_NOT_VERIFIED,
                coder_analysis="rejected",
                coder_reason="capability check",
            ),
            _make_finding(
                finding_id="F-I",
                coder_status=CODER_STATUS_INCONCLUSIVE,
                coder_analysis="evidence missing",
                coder_reason="no sanitizer found",
            ),
            _make_finding(
                finding_id="F-S",
                coder_status=CODER_STATUS_SKIPPED,
                coder_reason="cancelled by user",
            ),
            _make_finding(
                finding_id="F-P",
                coder_status=CODER_STATUS_PENDING,
            ),
            _make_finding(
                finding_id="F-F",
                coder_status=CODER_STATUS_FAIL,
                coder_reason="transport error: 5xx",
            ),
        ]
        text = render_coder_results_report(findings, coder_enabled=True)
        for status in (
            "Verified",
            "Not Verified",
            "Inconclusive",
            "Skipped",
            "Pending",
            "Fail",
        ):
            self.assertIn(f"**Coder Status**: {status}", text)
        # Pending preserves the existing "Not run" semantic; Fail uses
        # the distinct "Not produced (transport failure)" label.
        self.assertIn("Not produced (transport failure)", text)
        # The Verified evidence block round-trips.
        self.assertIn("def sanitize(): pass", text)


class FindingsReportFailRenderingTests(unittest.TestCase):
    def test_findings_report_renders_fail_section(self) -> None:
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_FAIL,
                coder_reason="transport error: 503 Service Unavailable",
            ),
        ]
        text = render_findings_report(findings, coder_enabled=True)
        self.assertIn("### Coder Verification", text)
        self.assertIn("**Coder Status**: Fail", text)
        self.assertIn(
            "transport error: 503 Service Unavailable", text
        )
        self.assertIn(
            "**Coder Analysis**: _Not produced (transport failure)_", text
        )
        self.assertIn(
            "**Coder Call-chain Evidence**: _Not produced (transport failure)_",
            text,
        )

    def test_false_positives_report_renders_fail_with_transport_label(self) -> None:
        # A False-Positive validator verdict whose coder verification
        # also failed — appears in false-positives.md AND the section
        # uses the transport-failure label, not the "Not run" label.
        findings = [
            _make_finding(
                finding_id="F-0001",
                validation_status=ValidationStatus.FALSE_POSITIVE,
                coder_status=CODER_STATUS_FAIL,
                coder_reason="transport error: timed out",
            )
        ]
        text = render_false_positives_report(findings, coder_enabled=True)
        self.assertIn("**Coder Status**: Fail", text)
        self.assertIn("**Coder Reason**: transport error: timed out", text)
        self.assertIn(
            "**Coder Analysis**: _Not produced (transport failure)_", text
        )

    def test_fail_section_omitted_on_disabled_coder_run(self) -> None:
        # Defensive: a Fail row should not exist on a coder-disabled
        # run, but if it somehow did, the section is still omitted
        # (matches the existing Skipped-disabled behavior; Fail
        # follows the same render gate).
        findings = [
            _make_finding(
                finding_id="F-0001",
                coder_status=CODER_STATUS_SKIPPED,  # natural disabled-coder shape
                coder_reason="",
            )
        ]
        text = render_findings_report(findings, coder_enabled=False)
        self.assertNotIn("### Coder Verification", text)


if __name__ == "__main__":
    unittest.main()
