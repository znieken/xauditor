"""Round-trip integrity test for migration ``0014_audit_run_stages_form``.

Inserts ``audit_runs`` rows BEFORE the migration applies, runs the
migration, and asserts that the new ``stages_form`` column lands on
existing rows with the default ``'prompt'`` value, that the CHECK
constraint rejects out-of-enum values, and that downgrade removes the
column cleanly.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` like the other live-DB tests.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from tests._live_db import LiveDatabaseTestCase


_PORTAL_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "xauditor_portal"
_ALEMBIC_INI = _PORTAL_PACKAGE / "db" / "migrations" / "alembic.ini"


def _alembic_config(sync_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(_ALEMBIC_INI)) if _ALEMBIC_INI.exists() else AlembicConfig()
    cfg.set_main_option(
        "script_location",
        str(_PORTAL_PACKAGE / "db" / "migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


class AuditRunStagesFormMigrationTests(LiveDatabaseTestCase):
    async def test_existing_rows_backfill_to_prompt_and_check_enforced(self) -> None:
        cfg = _alembic_config(self.sync_url)

        # Step 1: roll back to the revision just before 0014 so we can
        # observe the column appearing on a populated table.
        await self._downgrade_to("0013_findings_agentic_transcript", cfg)

        # Step 2: insert two pre-migration rows. They have no
        # `stages_form` column yet.
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO report.audit_runs "
                    "(id, repo_root, project_name, build_fingerprint, mode, "
                    "status, progress_percent, total_candidates, valid_findings, "
                    "false_positives, unlabeled_findings, duplicate_findings, "
                    "llm_providers_used, started_at) "
                    "VALUES "
                    "(:id1, '/tmp/r1', 'r1', 'bf-1', 'fast', 'completed', "
                    "100, 0, 0, 0, 0, 0, '{}'::jsonb, :ts), "
                    "(:id2, '/tmp/r2', 'r2', 'bf-2', 'deep', 'completed', "
                    "100, 0, 0, 0, 0, 0, '{}'::jsonb, :ts)"
                ),
                {
                    "id1": uuid.uuid4(),
                    "id2": uuid.uuid4(),
                    "ts": datetime.now(timezone.utc),
                },
            )

        # Step 3: apply 0014.
        command.upgrade(cfg, "head")

        # Step 4: every existing row receives the default 'prompt'.
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT mode, stages_form FROM report.audit_runs "
                        "ORDER BY repo_root"
                    )
                )
            ).all()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row[1], "prompt")

        # Step 5: a fresh INSERT without `stages_form` picks up the
        # column default.
        new_id = uuid.uuid4()
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO report.audit_runs "
                    "(id, repo_root, project_name, build_fingerprint, mode, "
                    "status, progress_percent, total_candidates, valid_findings, "
                    "false_positives, unlabeled_findings, duplicate_findings, "
                    "llm_providers_used, started_at) "
                    "VALUES "
                    "(:id, '/tmp/r3', 'r3', 'bf-3', 'fast', 'completed', "
                    "100, 0, 0, 0, 0, 0, '{}'::jsonb, :ts)"
                ),
                {"id": new_id, "ts": datetime.now(timezone.utc)},
            )
            value = (
                await conn.execute(
                    text(
                        "SELECT stages_form FROM report.audit_runs WHERE id = :id"
                    ),
                    {"id": new_id},
                )
            ).scalar_one()
        self.assertEqual(value, "prompt")

        # Step 6: an INSERT specifying 'agentic' is accepted.
        agentic_id = uuid.uuid4()
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO report.audit_runs "
                    "(id, repo_root, project_name, build_fingerprint, mode, "
                    "stages_form, status, progress_percent, total_candidates, "
                    "valid_findings, false_positives, unlabeled_findings, "
                    "duplicate_findings, llm_providers_used, started_at) "
                    "VALUES "
                    "(:id, '/tmp/r4', 'r4', 'bf-4', 'deep', 'agentic', "
                    "'in_progress', 50, 0, 0, 0, 0, 0, '{}'::jsonb, :ts)"
                ),
                {"id": agentic_id, "ts": datetime.now(timezone.utc)},
            )
            value = (
                await conn.execute(
                    text(
                        "SELECT stages_form FROM report.audit_runs WHERE id = :id"
                    ),
                    {"id": agentic_id},
                )
            ).scalar_one()
        self.assertEqual(value, "agentic")

        # Step 7: an INSERT with an invalid value violates the CHECK.
        with self.assertRaises(sqlalchemy.exc.IntegrityError):
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO report.audit_runs "
                        "(id, repo_root, project_name, build_fingerprint, mode, "
                        "stages_form, status, progress_percent, total_candidates, "
                        "valid_findings, false_positives, unlabeled_findings, "
                        "duplicate_findings, llm_providers_used, started_at) "
                        "VALUES "
                        "(:id, '/tmp/r5', 'r5', 'bf-5', 'fast', 'bogus', "
                        "'completed', 100, 0, 0, 0, 0, 0, '{}'::jsonb, :ts)"
                    ),
                    {"id": uuid.uuid4(), "ts": datetime.now(timezone.utc)},
                )

        # Step 8: idempotency — re-running the migration is a no-op.
        command.upgrade(cfg, "head")
        async with self._engine.begin() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM report.audit_runs "
                        "WHERE stages_form NOT IN ('prompt', 'agentic')"
                    )
                )
            ).scalar_one()
        self.assertEqual(count, 0)

    async def test_downgrade_removes_column(self) -> None:
        cfg = _alembic_config(self.sync_url)

        # Make sure the column exists post-head.
        command.upgrade(cfg, "head")
        async with self._engine.begin() as conn:
            present = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'report' "
                        "AND table_name = 'audit_runs' "
                        "AND column_name = 'stages_form'"
                    )
                )
            ).scalar_one_or_none()
        self.assertEqual(present, "stages_form")

        # Downgrade strips it.
        command.downgrade(cfg, "0013_findings_agentic_transcript")
        async with self._engine.begin() as conn:
            present_after = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'report' "
                        "AND table_name = 'audit_runs' "
                        "AND column_name = 'stages_form'"
                    )
                )
            ).scalar_one_or_none()
        self.assertIsNone(present_after)

        # Restore for downstream tests in the suite.
        command.upgrade(cfg, "head")

    async def _downgrade_to(self, revision: str, cfg: AlembicConfig) -> None:
        command.downgrade(cfg, revision)


if __name__ == "__main__":
    unittest.main()
