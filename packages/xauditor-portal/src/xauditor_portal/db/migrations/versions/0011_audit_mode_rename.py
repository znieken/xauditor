"""Rename `audit_runs.mode` values + add `coverage_gaps` JSONB column.

Revision ID: 0011_audit_mode_rename
Revises: 0010_audit_run_admin_actions
Create Date: 2026-05-04

Implements ``restructure-audit-modes-and-coverage`` Phase 1.

Two changes:

1. Rewrite historical ``audit_runs.mode`` values from
   ``"single"``/``"team"`` to ``"fast"``/``"deep"`` to align with the
   new audit-mode terminology. The column type
   (``VARCHAR(16)``) is unchanged. The ``"team" → "deep"`` rewrite
   is a documented semantic approximation: a ``deep``-mode run after
   the full Phase 5 lands covers strictly more vulnerability classes
   than the historical ``team``-mode run did. Operators tracking
   precise lineage should consult ``engine_version`` (via the run's
   ``llm_providers_used`` payload or the application logs) to
   determine which capability set the deep-mode run actually
   exercised.

2. Add a nullable JSONB column ``coverage_gaps`` to ``audit_runs``.
   Phase 1 writes ``NULL`` (the column exists but the value is
   populated by Phase 5's ``CoverageGaps`` infrastructure). Pre-rename
   runs naturally have ``coverage_gaps = NULL`` and the portal renders
   an "n/a — pre-rename audit" placeholder for those rows.

Idempotent: every statement uses ``IF NOT EXISTS`` / equivalent
guards so re-running on an already-migrated DB no-ops. The mode
rewrite is idempotent because once executed it leaves no rows with
the legacy values to rewrite again.

Downgrade: provided as a courtesy but **not exercised in CI**. The
``'fast' → 'single'`` rewrite loses information when the deep-mode
capability ran more than the historical team-mode coverage; document
the tradeoff in CHANGELOG.
"""

from __future__ import annotations

from alembic import op


revision: str = "0011_audit_mode_rename"
down_revision: str | None = "0010_audit_run_admin_actions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE report.audit_runs
        SET mode = 'fast'
        WHERE mode = 'single'
        """
    )
    op.execute(
        """
        UPDATE report.audit_runs
        SET mode = 'deep'
        WHERE mode = 'team'
        """
    )
    op.execute(
        """
        ALTER TABLE report.audit_runs
        ADD COLUMN IF NOT EXISTS coverage_gaps JSONB NULL
        """
    )


def downgrade() -> None:
    # Forward-only by intent. The reverse data rewrite drops the
    # information that the run was a *post-rename* deep mode (which
    # covers strictly more classes than legacy team mode); operators
    # rolling back acknowledge that loss when running this.
    op.execute(
        """
        ALTER TABLE report.audit_runs
        DROP COLUMN IF EXISTS coverage_gaps
        """
    )
    op.execute(
        """
        UPDATE report.audit_runs
        SET mode = 'team'
        WHERE mode = 'deep'
        """
    )
    op.execute(
        """
        UPDATE report.audit_runs
        SET mode = 'single'
        WHERE mode = 'fast'
        """
    )
