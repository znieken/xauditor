"""Add `role` and `sessions_invalid_before` to `auth.users`.

Revision ID: 0008_users_role
Revises: 0007_duplicate_feedback_label
Create Date: 2026-05-01

Implements ``add-portal-rbac-users-tab``. Adds:

- ``auth.users.role VARCHAR(16) NOT NULL DEFAULT 'viewer'``.
- ``auth.users.sessions_invalid_before TIMESTAMPTZ NULL`` — used by the
  auth dependency to invalidate every JWT issued before the timestamp,
  closing the cookie window after a role change or admin-initiated
  password reset without enumerating outstanding ``jti`` values.
- CHECK constraint ``ck_users_role`` enforcing
  ``role IN ('admin', 'auditor', 'viewer')``.

Backfill: every pre-existing row is updated to ``role = 'admin'`` so
legacy single-user installs (whose only account predates the role
column) retain administrative access. The seed code in
``xauditor_portal.db.seed`` now writes ``admin/admin`` instead of
``auditor/auditor`` for fresh installs, but ``seed_default_user`` is a
no-op against a non-empty users table so existing installations keep
their existing accounts.

Idempotent ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS`` so
re-running on an already-migrated DB no-ops.
"""

from __future__ import annotations

from alembic import op


revision: str = "0008_users_role"
down_revision: str | None = "0007_duplicate_feedback_label"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth.users
        ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'viewer'
        """
    )
    op.execute(
        """
        ALTER TABLE auth.users
        ADD COLUMN IF NOT EXISTS sessions_invalid_before TIMESTAMPTZ NULL
        """
    )
    # Backfill — promote every pre-existing user to admin so an upgrading
    # operator does not get locked out of their own portal. Fresh installs
    # have an empty users table at this point (seed runs in 0001 only when
    # the table is empty), so this UPDATE affects 0 rows on a fresh chain.
    op.execute(
        """
        UPDATE auth.users SET role = 'admin' WHERE role = 'viewer'
        """
    )
    op.execute(
        """
        ALTER TABLE auth.users
        DROP CONSTRAINT IF EXISTS ck_users_role
        """
    )
    op.execute(
        """
        ALTER TABLE auth.users
        ADD CONSTRAINT ck_users_role
        CHECK (role IN ('admin', 'auditor', 'viewer'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth.users
        DROP CONSTRAINT IF EXISTS ck_users_role
        """
    )
    op.execute(
        """
        ALTER TABLE auth.users
        DROP COLUMN IF EXISTS sessions_invalid_before
        """
    )
    op.execute(
        """
        ALTER TABLE auth.users
        DROP COLUMN IF EXISTS role
        """
    )
