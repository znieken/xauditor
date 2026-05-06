"""Add `report.findings.agentic_transcript` JSONB column.

Revision ID: 0013_findings_agentic_transcript
Revises: 0012_findings_reconciliation
Create Date: 2026-05-04

Implements `agentic-stage-runner-real` Phase 2.

Adds a nullable JSONB column `agentic_transcript` to
`report.findings` for the per-finding tool-call telemetry the
real `AgenticStageRunner` produces. The column carries a
JSON list of `{tool, input, output}` records so PSIRT can
replay the agent's evidence-collection trail when triaging
findings.

The column SHALL be NULL on prompt-form findings (no
agentic transcript exists) and on findings produced before
this migration ships (no backfill — historical findings
have no transcript to recover).

Idempotent `ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF
EXISTS` so re-running on an already-migrated DB no-ops.
Forward-only by intent — the downgrade drops the column;
JSONB data is unrecoverable.
"""

from __future__ import annotations

from alembic import op


revision: str = "0013_findings_agentic_transcript"
down_revision: str | None = "0012_findings_reconciliation"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.findings
        ADD COLUMN IF NOT EXISTS agentic_transcript JSONB NULL
        """
    )


def downgrade() -> None:
    # Forward-only by intent. JSONB transcript data is unrecoverable
    # once dropped.
    op.execute(
        """
        ALTER TABLE report.findings
        DROP COLUMN IF EXISTS agentic_transcript
        """
    )
