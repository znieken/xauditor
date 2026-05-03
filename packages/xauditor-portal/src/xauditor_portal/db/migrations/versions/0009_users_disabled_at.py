"""Add `disabled_at` to `auth.users`.

Revision ID: 0009_users_disabled_at
Revises: 0008_users_role
Create Date: 2026-05-02

Implements ``portal-user-disable-and-ux-fixes``. Adds:

- ``auth.users.disabled_at TIMESTAMPTZ NULL`` — when non-NULL, the user is
  soft-disabled. The ``current_user`` dependency rejects requests for any
  user whose ``disabled_at IS NOT NULL`` with HTTP 401, and the login
  endpoint refuses authentication attempts for disabled users with HTTP 403.
  The "at least one enabled admin" invariant counts only rows where
  ``role = 'admin' AND disabled_at IS NULL``.

Backfill: none required — existing rows default to ``disabled_at = NULL``
which is the "enabled" state, matching the prior implicit behaviour.

Idempotent ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS`` so
re-running on an already-migrated DB no-ops.
"""

from __future__ import annotations

from alembic import op


revision: str = "0009_users_disabled_at"
down_revision: str | None = "0008_users_role"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth.users
        ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth.users
        DROP COLUMN IF EXISTS disabled_at
        """
    )
