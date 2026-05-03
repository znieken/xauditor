"""Add report.coder_findings and report.coder_finding_evidence tables.

Revision ID: 0005_coder_findings
Revises: 0004_findings_path_fingerprint
Create Date: 2026-04-27

Introduces the two tables that mirror the repo-global coder verification
verdict for each finding (see openspec/changes/add-coder-agent). The
parent table ``report.coder_findings`` carries one row per
``(run_id, finding_ref)`` tuple; the child table
``report.coder_finding_evidence`` carries the call-chain evidence items.

Upgrade uses ``CREATE TABLE IF NOT EXISTS`` so the migration is
idempotent: re-running the upgrade against a database that already has
the tables (e.g. after a partial manual migrate) heals the alembic
stamp instead of crashing.
"""

from __future__ import annotations

from alembic import op


revision: str = "0005_coder_findings"
down_revision: str | None = "0004_findings_path_fingerprint"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report.coder_findings (
            id            UUID PRIMARY KEY,
            run_id        UUID NOT NULL REFERENCES report.audit_runs(id) ON DELETE CASCADE,
            finding_ref   VARCHAR(128) NOT NULL,
            status        VARCHAR(32) NOT NULL,
            analysis      TEXT NOT NULL DEFAULT '',
            reason        TEXT NOT NULL DEFAULT '',
            dispatched_at TIMESTAMPTZ NULL,
            completed_at  TIMESTAMPTZ NULL,
            duration_ms   INTEGER NULL,
            cli_exit_code INTEGER NULL,
            cli_stderr    TEXT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_coder_findings_run_finding UNIQUE (run_id, finding_ref)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_coder_findings_run_status
            ON report.coder_findings (run_id, status)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report.coder_finding_evidence (
            id                UUID PRIMARY KEY,
            coder_finding_id  UUID NOT NULL REFERENCES report.coder_findings(id) ON DELETE CASCADE,
            file_path         VARCHAR(2048) NOT NULL,
            function_name     VARCHAR(512) NULL,
            snippet           TEXT NOT NULL,
            language          VARCHAR(64) NULL,
            role              VARCHAR(64) NOT NULL DEFAULT 'supporting',
            ordinal           INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_coder_finding_evidence_parent
            ON report.coder_finding_evidence (coder_finding_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS report.coder_finding_evidence CASCADE")
    op.execute("DROP TABLE IF EXISTS report.coder_findings CASCADE")
