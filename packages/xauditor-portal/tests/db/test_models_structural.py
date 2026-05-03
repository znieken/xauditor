from __future__ import annotations

import unittest

from xauditor_portal.db.base import DB_SCHEMAS, build_engine_url
from xauditor_portal.db.models import Base, config, feedback, report


EXPECTED_REPORT_TABLES = {
    "audit_runs",
    "audit_run_admin_actions",
    "findings",
    "finding_source_references",
    "referenced_symbols",
    "coverage_modules",
    "coverage_files",
    "coverage_functions",
    "analyzer_subagent_records",
    "validator_subagent_records",
    "exploiter_subagent_records",
    "validator_debates",
    "no_finding_paths",
    "path_raw_outputs",
    "progress_events",
    "coder_findings",
    "coder_finding_evidence",
}

EXPECTED_CONFIG_TABLES = {"config_snapshots", "config_overrides"}
EXPECTED_AUTH_TABLES = {"users", "password_history", "revoked_sessions"}
EXPECTED_FEEDBACK_TABLES = {"finding_annotations", "annotation_history"}


def _tables_in_schema(schema: str) -> set[str]:
    return {
        t.name
        for t in Base.metadata.tables.values()
        if t.schema == schema
    }


class SchemaCompositionTests(unittest.TestCase):
    def test_four_schemas_are_declared(self) -> None:
        self.assertEqual(DB_SCHEMAS, ("report", "config", "auth", "feedback"))

    def test_expected_tables_per_schema(self) -> None:
        self.assertEqual(_tables_in_schema("report"), EXPECTED_REPORT_TABLES)
        self.assertEqual(_tables_in_schema("config"), EXPECTED_CONFIG_TABLES)
        self.assertEqual(_tables_in_schema("auth"), EXPECTED_AUTH_TABLES)
        self.assertEqual(_tables_in_schema("feedback"), EXPECTED_FEEDBACK_TABLES)

    def test_every_table_belongs_to_a_known_schema(self) -> None:
        for table in Base.metadata.tables.values():
            self.assertIn(table.schema, DB_SCHEMAS, f"{table} has unexpected schema")


class FindingModelTests(unittest.TestCase):
    def test_finding_columns_cover_every_markdown_field(self) -> None:
        finding_cols = {c.name for c in report.Finding.__table__.columns}
        # Every field mandated by the finding-reporting contract must map to a column.
        for expected in (
            "finding_id",
            "finding_name",
            "finding_description",
            "confidence_level",
            "analyzer_status",
            "evidence_strength",
            "analysis",
            "reason",
            "context",
            "business_context",
            "context_notes",
            "suspect_function_id",
            "suspect_line",
            "exploitation_status",
            "exploitation_steps",
            "validation_status",
            "validation_analysis",
        ):
            self.assertIn(expected, finding_cols, f"Finding is missing {expected}")

    def test_finding_is_fk_linked_to_audit_run(self) -> None:
        fk_targets = {
            fk.target_fullname
            for fk in report.Finding.__table__.foreign_keys
        }
        self.assertIn("report.audit_runs.id", fk_targets)


class CoderModelTests(unittest.TestCase):
    def test_coder_findings_columns(self) -> None:
        cols = {c.name for c in report.CoderFinding.__table__.columns}
        for expected in (
            "id",
            "run_id",
            "finding_ref",
            "status",
            "analysis",
            "reason",
            "dispatched_at",
            "completed_at",
            "duration_ms",
            "cli_exit_code",
            "cli_stderr",
            "created_at",
        ):
            self.assertIn(expected, cols, f"CoderFinding is missing {expected}")

    def test_coder_findings_unique_constraint_on_run_finding_ref(self) -> None:
        constraints = {
            c.name for c in report.CoderFinding.__table__.constraints if getattr(c, "name", "")
        }
        self.assertIn("uq_coder_findings_run_finding", constraints)

    def test_coder_findings_fk_to_audit_runs(self) -> None:
        fks = {fk.target_fullname for fk in report.CoderFinding.__table__.foreign_keys}
        self.assertIn("report.audit_runs.id", fks)

    def test_coder_finding_evidence_columns_and_fk(self) -> None:
        cols = {c.name for c in report.CoderFindingEvidence.__table__.columns}
        for expected in (
            "id",
            "coder_finding_id",
            "file_path",
            "function_name",
            "snippet",
            "language",
            "role",
            "ordinal",
        ):
            self.assertIn(expected, cols, f"CoderFindingEvidence is missing {expected}")
        fks = {
            fk.target_fullname for fk in report.CoderFindingEvidence.__table__.foreign_keys
        }
        self.assertIn("report.coder_findings.id", fks)


class CrossSchemaForeignKeyTests(unittest.TestCase):
    def test_finding_annotation_points_at_report_schema(self) -> None:
        fks = {
            fk.target_fullname
            for fk in feedback.FindingAnnotation.__table__.foreign_keys
        }
        self.assertIn("report.audit_runs.id", fks)
        self.assertIn("report.findings.id", fks)
        self.assertIn("auth.users.id", fks)

    def test_annotation_history_points_at_feedback_and_auth(self) -> None:
        fks = {
            fk.target_fullname
            for fk in feedback.AnnotationHistory.__table__.foreign_keys
        }
        self.assertIn("feedback.finding_annotations.id", fks)
        self.assertIn("auth.users.id", fks)

    def test_config_snapshot_points_at_auth_users(self) -> None:
        fks = {
            fk.target_fullname
            for fk in config.ConfigSnapshot.__table__.foreign_keys
        }
        self.assertIn("auth.users.id", fks)


class EngineUrlBuilderTests(unittest.TestCase):
    def test_local_url_uses_asyncpg_driver(self) -> None:
        from xauditor.config import ReportDBConfig

        url = build_engine_url(ReportDBConfig())
        self.assertTrue(url.startswith("postgresql+asyncpg://"))
        self.assertIn("@127.0.0.1:", url)
        self.assertIn("/xauditor_reportdb", url)

    def test_remote_postgresql_url_is_rewritten_to_asyncpg(self) -> None:
        from xauditor.config import RemoteConnectionConfig, ReportDBConfig

        reportdb = ReportDBConfig(
            remote=RemoteConnectionConfig(url="postgresql://u:p@h:5432/db")
        )
        self.assertEqual(
            build_engine_url(reportdb), "postgresql+asyncpg://u:p@h:5432/db"
        )

    def test_remote_postgres_url_is_rewritten_to_asyncpg(self) -> None:
        from xauditor.config import RemoteConnectionConfig, ReportDBConfig

        reportdb = ReportDBConfig(
            remote=RemoteConnectionConfig(url="postgres://u:p@h:5432/db")
        )
        self.assertEqual(
            build_engine_url(reportdb), "postgresql+asyncpg://u:p@h:5432/db"
        )

    def test_asyncpg_url_is_passthrough(self) -> None:
        from xauditor.config import RemoteConnectionConfig, ReportDBConfig

        reportdb = ReportDBConfig(
            remote=RemoteConnectionConfig(url="postgresql+asyncpg://u:p@h:5432/db")
        )
        self.assertEqual(
            build_engine_url(reportdb), "postgresql+asyncpg://u:p@h:5432/db"
        )


class MigrationWrapperTests(unittest.TestCase):
    def test_upgrade_to_head_is_importable(self) -> None:
        from xauditor_portal.db.migrations import upgrade_to_head, downgrade_to_base

        self.assertTrue(callable(upgrade_to_head))
        self.assertTrue(callable(downgrade_to_base))

    def test_alembic_env_file_exists(self) -> None:
        from pathlib import Path

        from xauditor_portal.db import migrations as migrations_module

        env_path = Path(migrations_module.__file__).parent / "env.py"
        self.assertTrue(env_path.exists())
        versions_dir = env_path.parent / "versions"
        initial = versions_dir / "0001_initial.py"
        self.assertTrue(initial.exists())


if __name__ == "__main__":
    unittest.main()
