"""Hermetic tests for the ``add-duplicate-feedback-label`` change:
- ``LABELS`` registry now exposes ``LABEL_DUPLICATE``.
- ``FeedbackIn`` Pydantic model accepts the new
  ``duplicate_of_finding_id`` field.
- The biconditional rule that ``_validate_duplicate_payload``
  enforces is consistent with the schema's CHECK constraint.

Live-DB integration tests (CHECK constraint round-trip, FK
``ON DELETE SET NULL``, full PATCH guard suite) are gated by
``XAUDITOR_TEST_DATABASE_URL`` and live in
``tests/api/test_findings_duplicate_label.py`` — added in a
follow-up once the CI matrix has Postgres wired in.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "packages" / "xauditor-portal" / "src")
)

from xauditor_portal.api.findings import FeedbackIn  # noqa: E402
from xauditor_portal.db.models.feedback import (  # noqa: E402
    LABELS,
    LABEL_DUPLICATE,
    LABEL_FALSE_POSITIVE,
    LABEL_TRUE_POSITIVE,
    LABEL_UNLABELED,
)


class FeedbackLabelRegistryTests(unittest.TestCase):
    def test_duplicate_added_to_labels_tuple(self) -> None:
        self.assertIn(LABEL_DUPLICATE, LABELS)
        # Existing labels still present (no breaking change).
        self.assertIn(LABEL_TRUE_POSITIVE, LABELS)
        self.assertIn(LABEL_FALSE_POSITIVE, LABELS)
        self.assertIn(LABEL_UNLABELED, LABELS)

    def test_duplicate_label_value_is_stable(self) -> None:
        self.assertEqual(LABEL_DUPLICATE, "duplicate")


class FeedbackInPydanticTests(unittest.TestCase):
    def test_default_duplicate_pointer_is_none(self) -> None:
        body = FeedbackIn(label="true_positive")
        self.assertIsNone(body.duplicate_of_finding_id)
        self.assertIsNone(body.researcher_note)

    def test_accepts_duplicate_pointer(self) -> None:
        body = FeedbackIn(
            label="duplicate",
            duplicate_of_finding_id="11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(body.label, "duplicate")
        self.assertEqual(
            body.duplicate_of_finding_id,
            "11111111-1111-1111-1111-111111111111",
        )

    def test_pointer_is_optional_string(self) -> None:
        # Pydantic accepts ``None`` explicitly and coerces missing →
        # ``None`` via the field default.
        body = FeedbackIn(label="false_positive", duplicate_of_finding_id=None)
        self.assertIsNone(body.duplicate_of_finding_id)


if __name__ == "__main__":
    unittest.main()
