"""Add `report.audit_runs.stages_form` column.

Revision ID: 0014_audit_run_stages_form
Revises: 0013_findings_agentic_transcript
Create Date: 2026-05-05

Implements `portal-show-stages-form`.

Adds a NOT NULL TEXT column `stages_form` to `report.audit_runs`
mirroring `audit.stages.form`. Default `'prompt'` so existing
rows backfill cleanly — no operator-facing portal DB has yet
produced an `'agentic'` run, so the backfill is historically
accurate. CHECK constraint mirrors `AUDIT_STAGES_FORM_VALUES`
in `xauditor.config`; widening to a third form is one alembic
revision away (`DROP CONSTRAINT … ADD CONSTRAINT …`).

Idempotent `ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF EXISTS`
so re-running on an already-migrated DB no-ops. Downgrade drops
the column.
"""

from __future__ import annotations

from alembic import op


revision: str = "0014_audit_run_stages_form"
down_revision: str | None = "0013_findings_agentic_transcript"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.audit_runs
        ADD COLUMN IF NOT EXISTS stages_form TEXT NOT NULL DEFAULT 'prompt'
        """
    )
    # Add the CHECK separately so it survives a partial rerun: if the
    # column already exists from a previous attempt, ADD CONSTRAINT IF
    # NOT EXISTS keeps the upgrade idempotent.
    op.execute(
        """
        ALTER TABLE report.audit_runs
        DROP CONSTRAINT IF EXISTS audit_runs_stages_form_check
        """
    )
    op.execute(
        """
        ALTER TABLE report.audit_runs
        ADD CONSTRAINT audit_runs_stages_form_check
        CHECK (stages_form IN ('prompt', 'agentic'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.audit_runs
        DROP CONSTRAINT IF EXISTS audit_runs_stages_form_check
        """
    )
    op.execute(
        """
        ALTER TABLE report.audit_runs
        DROP COLUMN IF EXISTS stages_form
        """
    )
