"""Composite (run_id, status) indexes for coverage pagination and filtering.

Revision ID: 0003_coverage_status_indexes
Revises: 0002_project_index
Create Date: 2026-04-19

The paginated coverage endpoints filter by `run_id` and optionally by
`status`; a composite index makes both the status-filtered SELECT and the
summary COUNT … GROUP BY status query cheap on large runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0003_coverage_status_indexes"
down_revision: str | None = "0002_project_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_coverage_modules_run_status", "coverage_modules"),
    ("ix_coverage_files_run_status", "coverage_files"),
    ("ix_coverage_functions_run_status", "coverage_functions"),
)


def upgrade() -> None:
    for index_name, table_name in _INDEXES:
        op.execute(
            sa.text(
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f"ON report.{table_name} (run_id, status)"
            )
        )


def downgrade() -> None:
    for index_name, _ in reversed(_INDEXES):
        op.execute(sa.text(f'DROP INDEX IF EXISTS report."{index_name}"'))
