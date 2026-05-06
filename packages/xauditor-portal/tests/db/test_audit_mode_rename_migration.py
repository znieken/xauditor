"""Round-trip data integrity test for migration ``0011_audit_mode_rename``.

Inserts ``audit_runs`` rows with the legacy ``mode = 'single'`` /
``'team'`` values, runs the migration, and asserts that the rows are
rewritten to ``'fast'`` / ``'deep'`` while pre-existing data
(including the new ``coverage_gaps`` JSONB column) lands in the
expected shape.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` like the other live-DB tests.
The migration's idempotency is also verified — running it twice
leaves the table in the same shape.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


class AuditModeRenameMigrationTests(LiveDatabaseTestCase):
    async def test_legacy_mode_values_rewritten_to_new_names(self) -> None:
        """Insert legacy mode rows, run migration head, verify rewrite."""

        # Step 1: Downgrade to one revision before 0011 so we can
        # insert legacy data.
        cfg = _alembic_config(self.sync_url)
        await self._downgrade_to("0010_audit_run_admin_actions", cfg)

        # Step 2: Insert legacy rows directly (bypassing ORM enum coercion).
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO report.audit_runs "
                    "(id, repo_root, project_name, build_fingerprint, mode, "
                    "status, progress_percent, total_candidates, valid_findings, "
                    "false_positives, unlabeled_findings, duplicate_findings, "
                    "llm_providers_used, started_at) "
                    "VALUES "
                    "(:id1, '/tmp/r1', 'r1', 'bf-1', 'single', 'completed', "
                    "100, 0, 0, 0, 0, 0, '{}'::jsonb, :ts), "
                    "(:id2, '/tmp/r2', 'r2', 'bf-2', 'team', 'completed', "
                    "100, 0, 0, 0, 0, 0, '{}'::jsonb, :ts)"
                ),
                {
                    "id1": uuid.uuid4(),
                    "id2": uuid.uuid4(),
                    "ts": datetime.now(timezone.utc),
                },
            )

        # Step 3: Run the migration to head (applies 0011).
        command.upgrade(cfg, "head")

        # Step 4: Verify rewrite + new column shape.
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT mode, coverage_gaps "
                        "FROM report.audit_runs "
                        "ORDER BY repo_root"
                    )
                )
            ).all()
        self.assertEqual(len(rows), 2)
        modes = sorted(r[0] for r in rows)
        self.assertEqual(modes, ["deep", "fast"])
        for row in rows:
            self.assertIsNone(
                row[1],
                "coverage_gaps starts NULL on pre-Phase-5 rows",
            )

        # Step 5: Idempotency — running the migration again is a no-op.
        command.upgrade(cfg, "head")
        async with self._engine.begin() as conn:
            modes_after = sorted(
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT mode FROM report.audit_runs ORDER BY repo_root")
                    )
                ).all()
            )
        self.assertEqual(modes_after, ["deep", "fast"])

    async def _downgrade_to(self, revision: str, cfg: AlembicConfig) -> None:
        """Downgrade synchronously (alembic is sync)."""

        # alembic.command runs synchronously; OK to call from an async
        # test because we do not await within it.
        command.downgrade(cfg, revision)


if __name__ == "__main__":
    unittest.main()
