"""Add `duplicate` feedback label support.

Revision ID: 0007_duplicate_feedback_label
Revises: 0006_audit_runs_resume_state
Create Date: 2026-04-30

Implements ``add-duplicate-feedback-label``. Adds:

- ``feedback.finding_annotations.duplicate_of_finding_id`` UUID
  column (nullable, FK ``report.findings(id)`` ``ON DELETE SET NULL``).
- ``feedback.finding_annotations`` CHECK constraint
  ``ck_finding_annotations_duplicate_pointer`` enforcing the
  biconditional ``label = 'duplicate'`` ↔
  ``duplicate_of_finding_id IS NOT NULL``.
- ``feedback.annotation_history.previous_duplicate_of`` and
  ``new_duplicate_of`` UUID columns mirroring the canonical
  pointer so the audit trail captures pointer changes.
- ``report.audit_runs.duplicate_findings`` integer count
  populated by ``_sync_snapshot``.

Backfill is implicit: existing annotation rows have
``label`` ∈ {true_positive, false_positive, unlabeled}, so
``duplicate_of_finding_id IS NULL`` satisfies the CHECK
biconditional. Existing ``audit_runs`` rows get ``0`` for
``duplicate_findings`` via the column default (NOT NULL +
``DEFAULT 0``).

Idempotent ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF
EXISTS`` so re-running on an already-migrated DB no-ops.
"""

from __future__ import annotations

from alembic import op


revision: str = "0007_duplicate_feedback_label"
down_revision: str | None = "0006_audit_runs_resume_state"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE feedback.finding_annotations
        ADD COLUMN IF NOT EXISTS duplicate_of_finding_id UUID NULL
        REFERENCES report.findings(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_finding_annotations_duplicate_of_finding_id
        ON feedback.finding_annotations(duplicate_of_finding_id)
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.finding_annotations
        DROP CONSTRAINT IF EXISTS ck_finding_annotations_duplicate_pointer
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.finding_annotations
        ADD CONSTRAINT ck_finding_annotations_duplicate_pointer
        CHECK (
            (label = 'duplicate' AND duplicate_of_finding_id IS NOT NULL)
            OR
            (label <> 'duplicate' AND duplicate_of_finding_id IS NULL)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.annotation_history
        ADD COLUMN IF NOT EXISTS previous_duplicate_of UUID NULL
        REFERENCES report.findings(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.annotation_history
        ADD COLUMN IF NOT EXISTS new_duplicate_of UUID NULL
        REFERENCES report.findings(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE report.audit_runs
        ADD COLUMN IF NOT EXISTS duplicate_findings INTEGER NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.audit_runs
        DROP COLUMN IF EXISTS duplicate_findings
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.annotation_history
        DROP COLUMN IF EXISTS new_duplicate_of
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.annotation_history
        DROP COLUMN IF EXISTS previous_duplicate_of
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.finding_annotations
        DROP CONSTRAINT IF EXISTS ck_finding_annotations_duplicate_pointer
        """
    )
    op.execute(
        """
        DROP INDEX IF EXISTS feedback.ix_finding_annotations_duplicate_of_finding_id
        """
    )
    op.execute(
        """
        ALTER TABLE feedback.finding_annotations
        DROP COLUMN IF EXISTS duplicate_of_finding_id
        """
    )
