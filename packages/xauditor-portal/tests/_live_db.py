"""Shared base class for integration tests that require a live PostgreSQL DB.

Set ``XAUDITOR_TEST_DATABASE_URL`` to an async SQLAlchemy DSN pointing at
a *disposable* test database before running the suite::

    export XAUDITOR_TEST_DATABASE_URL=postgresql+asyncpg://xauditor:xauditor-password@127.0.0.1:5432/xauditor_reportdb_test
    python -m unittest discover -s tests -t .

Tests subclassing :class:`LiveDatabaseTestCase` ``skipTest`` cleanly when
the env var is absent, so normal hermetic runs stay green. Each test
gets a freshly truncated database; schemas are not dropped (avoids
re-seeding the default ``auditor`` user on every test).
"""

from __future__ import annotations

import os
import unittest
from typing import Any


DB_URL_ENV = "XAUDITOR_TEST_DATABASE_URL"

_TRUNCATE_SQL = (
    "TRUNCATE TABLE "
    "report.audit_runs, "
    "report.findings, "
    "report.finding_source_references, "
    "report.referenced_symbols, "
    "report.coverage_modules, "
    "report.coverage_files, "
    "report.coverage_functions, "
    "report.analyzer_subagent_records, "
    "report.validator_subagent_records, "
    "report.exploiter_subagent_records, "
    "report.validator_debates, "
    "report.no_finding_paths, "
    "report.path_raw_outputs, "
    "report.progress_events, "
    "feedback.finding_annotations, "
    "feedback.annotation_history "
    "RESTART IDENTITY CASCADE"
)


def live_db_url() -> str | None:
    return os.environ.get(DB_URL_ENV)


def _sync_url_from(async_url: str) -> str:
    """Translate an async DSN to a psycopg (blocking) DSN for Alembic."""

    if async_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + async_url[len("postgresql+asyncpg://") :]
    if async_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + async_url[len("postgresql://") :]
    return async_url


class LiveDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """Async unittest base that provisions a clean DB session per test.

    Subclasses access the configured database via ``self.session_factory``.
    ``self.async_url`` (async DSN) and ``self.sync_url`` (blocking DSN)
    are available for tests that need them directly (for example, the
    PostgresReportSink which is sync and takes the blocking DSN).
    """

    async_url: str = ""
    sync_url: str = ""
    _engine: Any = None
    session_factory: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        url = live_db_url()
        if not url:
            raise unittest.SkipTest(
                f"{DB_URL_ENV} is not set; skipping live-DB integration test."
            )
        cls.async_url = url
        cls.sync_url = _sync_url_from(url)

    async def asyncSetUp(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from xauditor_portal.db.base import make_async_session_factory
        from xauditor_portal.db.migrations import upgrade_to_head

        engine = create_async_engine(self.async_url, future=True, pool_pre_ping=True)
        upgrade_to_head(self.sync_url)
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE_SQL))
            # Seed user keeps its bcrypt hash but we clear the
            # must_change_password flag so tests can log in without
            # re-running the forced-rotation flow on every test. Tests
            # that specifically cover the forced-rotation behaviour
            # reset it back to True in their own setUp.
            await conn.execute(
                text(
                    "UPDATE auth.users SET must_change_password = false "
                    "WHERE username = 'auditor'"
                )
            )
        self._engine = engine
        self.session_factory = make_async_session_factory(engine)

    async def asyncTearDown(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self.session_factory = None
