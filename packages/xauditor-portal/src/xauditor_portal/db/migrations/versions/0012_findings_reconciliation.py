"""Add `report.findings.reconciliation` JSONB column.

Revision ID: 0012_findings_reconciliation
Revises: 0011_audit_mode_rename
Create Date: 2026-05-04

Implements `restructure-audit-modes-and-coverage` Phase 5A.

Adds a nullable JSONB column `reconciliation` to `report.findings`.
Phase 5A's `PassthroughReconciler` writes `NULL` for every Path-only
finding (single-unit groups have no reconciliation context to
record). When a future change ships real cross-unit enumeration
(`SinkAuditUnit` / `EntryAuditUnit` / etc. landing alongside the
graph-builder follow-up), the deep-mode reconciler stage starts
populating the column with `{per_unit_verdicts, consolidated_verdict,
consolidation_reasoning}` JSONB blobs that the portal's per-finding
"Per-Unit Verdicts" panel renders verbatim.

Idempotent `ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF EXISTS`
so re-running on an already-migrated DB no-ops. Forward-only by
intent — the downgrade path drops the column but the JSONB data
is unrecoverable, so operators rolling back lose any Phase 5+
reconciliation history. Documented as not-CI-exercised.
"""

from __future__ import annotations

from alembic import op


revision: str = "0012_findings_reconciliation"
down_revision: str | None = "0011_audit_mode_rename"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE report.findings
        ADD COLUMN IF NOT EXISTS reconciliation JSONB NULL
        """
    )


def downgrade() -> None:
    # Forward-only by intent. Documented in the docstring; not
    # exercised in CI.
    op.execute(
        """
        ALTER TABLE report.findings
        DROP COLUMN IF EXISTS reconciliation
        """
    )
