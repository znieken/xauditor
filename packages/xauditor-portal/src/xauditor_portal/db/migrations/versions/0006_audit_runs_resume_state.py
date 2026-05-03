"""Add report.audit_runs.resume_state JSONB column for in-DB resume support.

Revision ID: 0006_audit_runs_resume_state
Revises: 0005_coder_findings
Create Date: 2026-04-29

Phase 2 of openspec/changes/make-postgres-the-canonical-sink moves the
audit-resume payload off disk (``<report_dir>/resume-state.json``) and
into Postgres so xauditor 0.5.0 can drop the run-time Markdown sink and
the legacy ``<report_dir>/`` layout. The column is JSONB and nullable —
existing rows from xauditor 0.4.x stay valid (NULL means
"no in-DB resume state"; the new resume code surfaces a clear error
asking the operator to re-run from scratch).

Idempotent ALTER so re-running the migration on a database that already
has the column no-ops.
"""

from __future__ import annotations

from alembic import op


revision: str = "0006_audit_runs_resume_state"
down_revision: str | None = "0005_coder_findings"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.audit_runs
        ADD COLUMN IF NOT EXISTS resume_state JSONB NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.audit_runs
        DROP COLUMN IF EXISTS resume_state
        """
    )
