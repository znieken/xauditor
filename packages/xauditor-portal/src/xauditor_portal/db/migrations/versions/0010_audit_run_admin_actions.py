"""Create `report.audit_run_admin_actions` audit-log table.

Revision ID: 0010_audit_run_admin_actions
Revises: 0009_users_disabled_at
Create Date: 2026-05-02

Implements ``portal-user-disable-and-ux-fixes``. Records every admin-initiated
run-management action (cancel / complete / delete) with actor, run id,
status transition, and optional reason. Intentionally has no FK back to
``audit_runs.id`` because a ``delete`` action removes the run row but we
want the audit-log entry to survive (it is the only remaining record that
the action happened). The ``actor_user_id`` FK uses ``ON DELETE SET NULL``
so deleting the actor preserves history with a tombstoned actor field.

Idempotent ``CREATE TABLE IF NOT EXISTS`` / ``DROP TABLE IF EXISTS`` so
re-running on an already-migrated DB no-ops.
"""

from __future__ import annotations

from alembic import op


revision: str = "0010_audit_run_admin_actions"
down_revision: str | None = "0009_users_disabled_at"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report.audit_run_admin_actions (
            id UUID PRIMARY KEY,
            actor_user_id UUID NULL REFERENCES auth.users(id) ON DELETE SET NULL,
            run_id UUID NOT NULL,
            action VARCHAR(16) NOT NULL,
            previous_status VARCHAR(32) NULL,
            new_status VARCHAR(32) NOT NULL,
            reason TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_audit_run_admin_actions_run
        ON report.audit_run_admin_actions (run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_audit_run_admin_actions_created
        ON report.audit_run_admin_actions (created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS report.audit_run_admin_actions")
