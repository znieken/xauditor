"""Add report.findings.path_fingerprint so debate/subagent rows can link to findings.

Revision ID: 0004_findings_path_fingerprint
Revises: 0003_coverage_status_indexes
Create Date: 2026-04-19

The portal's finding card renders a per-round validator-debate section when
`has_debate` is true, but the sink historically wrote
`validator_debates.finding_ref = <path_fingerprint>::f<idx>` while the portal
compared that against `findings.finding_id = F-NNNN` — so `has_debate` was
always false. We fix the join at the sink boundary by translating the
per-path key to the owning `Finding.finding_id`, and we persist the
originating `path_fingerprint` on the finding row so the translation map has
a durable anchor.

Upgrade uses `ADD COLUMN IF NOT EXISTS` so the migration is idempotent:
rolling back `alembic_version` and re-running the upgrade does not crash
when the column is already present, and a live DB whose schema got ahead of
its alembic-version stamp (e.g. via a partial manual migrate) heals itself
instead of crash-looping the backend container on startup.
"""

from __future__ import annotations

from alembic import op


revision: str = "0004_findings_path_fingerprint"
down_revision: str | None = "0003_coverage_status_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE report.findings "
        "ADD COLUMN IF NOT EXISTS path_fingerprint VARCHAR(128)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE report.findings DROP COLUMN IF EXISTS path_fingerprint"
    )
