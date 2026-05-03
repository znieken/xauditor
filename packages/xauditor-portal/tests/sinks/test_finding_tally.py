"""Hermetic unit tests for the finding-tally predicates that drive
``audit_runs.valid_findings`` / ``false_positives`` / ``unlabeled_findings``.

Regression for "portal Unlabeled metric stuck on 0": the
``_finding_is_valid`` predicate previously included
``Inconclusive`` in the valid bucket, which made
``valid + false_positives == total``, leaving
``unlabeled = total - valid - false_positives`` permanently zero.

The contract: ``Inconclusive`` is the LLM's "needs human review"
verdict and SHALL fall through to the ``unlabeled`` bucket so the
operator's actionable backlog metric works.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "packages" / "xauditor-portal" / "src")
)

from xauditor.models import ValidationStatus  # noqa: E402
from xauditor_portal.sinks.postgres_sink import (  # noqa: E402
    _finding_is_false_positive,
    _finding_is_valid,
)


@dataclass
class _StubFinding:
    validation_status: object


class FindingValidPredicateTests(unittest.TestCase):
    def test_valid_status_counts_as_valid(self) -> None:
        f = _StubFinding(validation_status=ValidationStatus.VALID)
        self.assertTrue(_finding_is_valid(f))
        self.assertFalse(_finding_is_false_positive(f))

    def test_partial_valid_status_counts_as_valid(self) -> None:
        f = _StubFinding(validation_status=ValidationStatus.PARTIAL_VALID)
        self.assertTrue(_finding_is_valid(f))
        self.assertFalse(_finding_is_false_positive(f))

    def test_inconclusive_does_NOT_count_as_valid(self) -> None:
        # The bug-fix regression: Inconclusive must NOT be in the
        # valid bucket. It falls through subtraction into
        # ``unlabeled_findings`` so the dashboard's "Unlabeled"
        # metric reflects the operator-actionable backlog.
        f = _StubFinding(validation_status=ValidationStatus.INCONCLUSIVE)
        self.assertFalse(_finding_is_valid(f))
        self.assertFalse(_finding_is_false_positive(f))

    def test_false_positive_status_counts_as_false_positive(self) -> None:
        f = _StubFinding(validation_status=ValidationStatus.FALSE_POSITIVE)
        self.assertFalse(_finding_is_valid(f))
        self.assertTrue(_finding_is_false_positive(f))

    def test_predicates_accept_raw_string_status(self) -> None:
        # Defensively accept the str form too — some serialization
        # paths flatten the enum into its `.value`.
        for raw in ("Valid", "Partial Valid"):
            with self.subTest(raw=raw):
                f = _StubFinding(validation_status=raw)
                self.assertTrue(_finding_is_valid(f))
        for raw in ("Inconclusive", "False Positive"):
            with self.subTest(raw=raw):
                f = _StubFinding(validation_status=raw)
                self.assertFalse(_finding_is_valid(f))


class FindingTallyMathTests(unittest.TestCase):
    """Reproduces the ``_sync_snapshot`` math:
    ``unlabeled = total - valid - false_positives``."""

    def _tally(self, findings):
        total = len(findings)
        valid = sum(1 for f in findings if _finding_is_valid(f))
        fp = sum(1 for f in findings if _finding_is_false_positive(f))
        unlabeled = total - valid - fp
        return total, valid, fp, max(0, unlabeled)

    def test_inconclusive_lands_in_unlabeled(self) -> None:
        findings = [
            _StubFinding(ValidationStatus.VALID),
            _StubFinding(ValidationStatus.INCONCLUSIVE),
            _StubFinding(ValidationStatus.INCONCLUSIVE),
            _StubFinding(ValidationStatus.FALSE_POSITIVE),
        ]
        total, valid, fp, unlabeled = self._tally(findings)
        self.assertEqual(total, 4)
        self.assertEqual(valid, 1)
        self.assertEqual(fp, 1)
        self.assertEqual(unlabeled, 2)

    def test_no_inconclusive_leaves_unlabeled_zero(self) -> None:
        findings = [
            _StubFinding(ValidationStatus.VALID),
            _StubFinding(ValidationStatus.PARTIAL_VALID),
            _StubFinding(ValidationStatus.FALSE_POSITIVE),
        ]
        total, valid, fp, unlabeled = self._tally(findings)
        self.assertEqual(total, 3)
        self.assertEqual(valid, 2)
        self.assertEqual(fp, 1)
        self.assertEqual(unlabeled, 0)


if __name__ == "__main__":
    unittest.main()
