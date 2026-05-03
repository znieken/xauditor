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
    CODER_STATUSES,
    CoderEvidence,
    ConfidenceLevel,
    Finding,
    ValidationStatus,
    normalize_coder_status,
)


class CoderStatusNormalizationTests(unittest.TestCase):
    def test_canonical_values_pass_through(self) -> None:
        # Every claude-producible canonical status round-trips. Fail is
        # excluded — it is reserved for transport-level failures the
        # audit assigns directly and is NEVER produced by claude, so a
        # literal "Fail" string from claude normalises to Inconclusive.
        for status in CODER_STATUSES:
            if status == CODER_STATUS_FAIL:
                self.assertEqual(
                    normalize_coder_status(status), CODER_STATUS_INCONCLUSIVE
                )
                continue
            self.assertEqual(normalize_coder_status(status), status)

    def test_synonyms_are_normalised(self) -> None:
        cases = {
            "verified": CODER_STATUS_VERIFIED,
            "confirmed": CODER_STATUS_VERIFIED,
            "valid": CODER_STATUS_VERIFIED,
            "true_positive": CODER_STATUS_VERIFIED,
            "true positive": CODER_STATUS_VERIFIED,
            "false_positive": CODER_STATUS_NOT_VERIFIED,
            "false positive": CODER_STATUS_NOT_VERIFIED,
            "not_verified": CODER_STATUS_NOT_VERIFIED,
            "rejected": CODER_STATUS_NOT_VERIFIED,
            "uncertain": CODER_STATUS_INCONCLUSIVE,
            "unknown": CODER_STATUS_INCONCLUSIVE,
            "in_progress": CODER_STATUS_PENDING,
            "skipped": CODER_STATUS_SKIPPED,
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_coder_status(raw), expected)

    def test_case_and_whitespace_are_normalised(self) -> None:
        self.assertEqual(normalize_coder_status("  VERIFIED  "), CODER_STATUS_VERIFIED)
        self.assertEqual(
            normalize_coder_status("False   Positive"), CODER_STATUS_NOT_VERIFIED
        )
        self.assertEqual(
            normalize_coder_status("not\tverified"), CODER_STATUS_NOT_VERIFIED
        )

    def test_unknown_value_falls_back_to_inconclusive(self) -> None:
        self.assertEqual(normalize_coder_status("definitely-not-a-status"), CODER_STATUS_INCONCLUSIVE)

    def test_empty_and_none_fall_back_to_inconclusive(self) -> None:
        self.assertEqual(normalize_coder_status(""), CODER_STATUS_INCONCLUSIVE)
        self.assertEqual(normalize_coder_status("   "), CODER_STATUS_INCONCLUSIVE)
        self.assertEqual(normalize_coder_status(None), CODER_STATUS_INCONCLUSIVE)


class FindingDataclassTests(unittest.TestCase):
    def _make_finding(self, **overrides: object) -> Finding:
        defaults: dict[str, object] = {
            "finding_id": "F-0001",
            "finding_name": "Example",
            "finding_description": "desc",
            "confidence_level": ConfidenceLevel.MEDIUM,
            "source_references": (),
            "analysis": "a",
            "reason": "r",
            "context": "c",
            "business_context": "bc",
            "exploitation_status": "uncertain",
            "exploitation_steps": "",
            "validation_status": ValidationStatus.INCONCLUSIVE,
            "validation_analysis": "",
            "path_fingerprint": "fp-1",
        }
        defaults.update(overrides)
        return Finding(**defaults)  # type: ignore[arg-type]

    def test_default_coder_fields(self) -> None:
        finding = self._make_finding()
        self.assertEqual(finding.coder_status, CODER_STATUS_SKIPPED)
        self.assertEqual(finding.coder_analysis, "")
        self.assertEqual(finding.coder_reason, "")
        self.assertEqual(finding.coder_call_chain_evidence, ())

    def test_evidence_dataclass_is_frozen(self) -> None:
        evidence = CoderEvidence(
            file_path="src/foo.py",
            function_name="foo",
            snippet="def foo(): ...",
            language="python",
            role="definition",
        )
        with self.assertRaises(AttributeError):
            evidence.role = "other"  # type: ignore[misc]

    def test_finding_carries_coder_fields_when_provided(self) -> None:
        evidence = (
            CoderEvidence(
                file_path="src/foo.py",
                function_name="foo",
                snippet="def foo(): ...",
                language="python",
                role="definition",
            ),
            CoderEvidence(
                file_path="src/bar.py",
                function_name=None,
                snippet="bar()",
                language=None,
                role="call_site",
            ),
        )
        finding = self._make_finding(
            coder_status=CODER_STATUS_VERIFIED,
            coder_analysis="repo-global ok",
            coder_reason="sanitizer present",
            coder_call_chain_evidence=evidence,
        )
        self.assertEqual(finding.coder_status, CODER_STATUS_VERIFIED)
        self.assertEqual(finding.coder_analysis, "repo-global ok")
        self.assertEqual(finding.coder_reason, "sanitizer present")
        self.assertEqual(len(finding.coder_call_chain_evidence), 2)
        self.assertEqual(finding.coder_call_chain_evidence[1].role, "call_site")


if __name__ == "__main__":
    unittest.main()
