"""Indexes on report.audit_runs for project/build aggregations and per-run lookup.

Revision ID: 0002_project_index
Revises: 0001_initial
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0002_project_index"
down_revision: str | None = "0001_initial"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_PROJECT_INDEX = "ix_audit_runs_project_build"
_REPORT_DIR_INDEX = "ix_audit_runs_report_dir"


def upgrade() -> None:
    op.execute(
        sa.text(
            f'CREATE INDEX IF NOT EXISTS "{_PROJECT_INDEX}" '
            'ON report.audit_runs (repo_root, project_name, build_fingerprint)'
        )
    )
    # PostgresReportSink keys an audit run by report_dir (the timestamped
    # `.xauditor/reports/<stamp>/` directory). Index it so per-run UPSERTs
    # stay cheap once thousands of runs accumulate.
    op.execute(
        sa.text(
            f'CREATE INDEX IF NOT EXISTS "{_REPORT_DIR_INDEX}" '
            'ON report.audit_runs (report_dir)'
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f'DROP INDEX IF EXISTS report."{_REPORT_DIR_INDEX}"'))
    op.execute(sa.text(f'DROP INDEX IF EXISTS report."{_PROJECT_INDEX}"'))
