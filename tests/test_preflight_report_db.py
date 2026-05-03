"""Pre-flight tests for the mandatory report-database probe (Phase 2 §9).

The probe is wired into ``services.run_audit`` / ``services.resume_audit``
via ``ApplicationServices.report_db_preflight``. These tests cover the
individual branches (connection refused / auth failed / schema below
minimum / happy path) at the helper-function level so we don't need a
live Postgres for unit coverage. End-to-end live-DB coverage lives
alongside the existing postgres-sink integration suite.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.preflight import (
    _REPORT_DB_MINIMUM_REVISION,
    _ensure_minimum_revision,
    _raise_authn_or_connect_error,
    _safe_endpoint,
    check_report_database,
)
from xauditor.errors import PreflightError


class SafeEndpointTests(unittest.TestCase):
    def test_password_is_stripped(self) -> None:
        url = "postgresql+psycopg://xauditor:supersecret@127.0.0.1:5432/xauditor"
        masked = _safe_endpoint(url)
        self.assertNotIn("supersecret", masked)
        self.assertIn("127.0.0.1", masked)
        self.assertIn("5432", masked)

    def test_no_credentials_pass_through(self) -> None:
        url = "postgresql+psycopg://127.0.0.1:5432/xauditor"
        self.assertEqual(_safe_endpoint(url), url)


class EnsureMinimumRevisionTests(unittest.TestCase):
    def test_none_revision_raises_with_init_recommendation(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            _ensure_minimum_revision("postgresql://host:5432/db", None)
        message = str(ctx.exception)
        self.assertIn("xauditor reportdb init", message)
        self.assertIn(_REPORT_DB_MINIMUM_REVISION, message)

    def test_head_revision_passes_silently(self) -> None:
        # Should NOT raise.
        _ensure_minimum_revision(
            "postgresql://host:5432/db", _REPORT_DB_MINIMUM_REVISION
        )

    def test_older_known_ancestor_raises_with_migrate_recommendation(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            # 0001_initial is the very first migration, definitely
            # an ancestor of 0006.
            _ensure_minimum_revision("postgresql://host:5432/db", "0001_initial")
        message = str(ctx.exception)
        self.assertIn("migrate", message.lower())
        self.assertIn("0001_initial", message)


class RaiseAuthnOrConnectErrorTests(unittest.TestCase):
    def test_connection_refused_recommends_reportdb_start(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            _raise_authn_or_connect_error(
                "postgresql://localhost:5432/db",
                Exception("connection refused"),
            )
        message = str(ctx.exception)
        self.assertIn("xauditor reportdb start", message)

    def test_authentication_failed_recommends_credentials_review(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            _raise_authn_or_connect_error(
                "postgresql://localhost:5432/db",
                Exception("password authentication failed for user"),
            )
        message = str(ctx.exception)
        self.assertIn("reportdb.connection", message)

    def test_unknown_error_falls_back_to_generic_remediation(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            _raise_authn_or_connect_error(
                "postgresql://localhost:5432/db",
                Exception("some weird error"),
            )
        message = str(ctx.exception)
        self.assertIn("xauditor reportdb start", message)


class CheckReportDatabaseTests(unittest.TestCase):
    """Exercise the full ``check_report_database`` flow with the engine
    layer mocked. Asserts the function chooses the right error branch
    for each failure mode without needing a live Postgres."""

    def _config(self):
        cfg = MagicMock()
        cfg.username = "xauditor"
        cfg.password = "secret"
        cfg.port = 5432
        cfg.database = "xauditor"
        cfg.remote = None
        return cfg

    def test_connection_refused_raises_with_reportdb_start_hint(self) -> None:
        from xauditor_portal.sinks.postgres_sink import _sync_url_for as real_sync

        engine = MagicMock()
        connect_cm = MagicMock()
        conn = MagicMock()
        connect_cm.__enter__ = MagicMock(
            side_effect=Exception("connection refused")
        )
        connect_cm.__exit__ = MagicMock(return_value=False)
        engine.connect.return_value = connect_cm
        with patch("sqlalchemy.create_engine", return_value=engine):
            with patch(
                "xauditor_portal.sinks.postgres_sink._sync_url_for",
                side_effect=real_sync,
            ):
                with self.assertRaises(PreflightError) as ctx:
                    check_report_database(self._config())
        self.assertIn("xauditor reportdb start", str(ctx.exception))

    def test_happy_path_no_error_when_revision_matches(self) -> None:
        from xauditor_portal.sinks.postgres_sink import _sync_url_for as real_sync

        engine = MagicMock()
        connect_cm = MagicMock()
        conn = MagicMock()
        # ``conn.execute("SELECT 1")`` succeeds; alembic query returns
        # the head revision.
        execute_results = []

        def _execute(stmt):
            text = str(stmt).strip()
            if "SELECT 1" in text:
                return MagicMock()
            if "alembic_version" in text:
                row = MagicMock()
                row.first.return_value = (_REPORT_DB_MINIMUM_REVISION,)
                return row
            return MagicMock()

        conn.execute.side_effect = _execute
        connect_cm.__enter__ = MagicMock(return_value=conn)
        connect_cm.__exit__ = MagicMock(return_value=False)
        engine.connect.return_value = connect_cm
        with patch("sqlalchemy.create_engine", return_value=engine):
            with patch(
                "xauditor_portal.sinks.postgres_sink._sync_url_for",
                side_effect=real_sync,
            ):
                # Should NOT raise.
                check_report_database(self._config())


if __name__ == "__main__":
    unittest.main()
