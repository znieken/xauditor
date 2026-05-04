"""Live-DB integration tests for ``GET /api/projects.running_audit_runs``.

These tests exercise the conditional ``SUM(CASE …)`` we add to the
projects aggregate: only ``status == "in_progress"`` rows count, and the
field is computed inside the same group-by pass — no per-row follow-up
query.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` like the other live-DB tests.
"""

from __future__ import annotations

import re
import unittest

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.app import create_app
from xauditor_portal.db.models.report import AuditRun


def _row_for(body: dict, project_name: str) -> dict:
    for row in body["items"]:
        if row["project_name"] == project_name:
            return row
    raise AssertionError(
        f"project {project_name} not in list response: {body}"
    )


class ProjectsRunningCountTests(LiveDatabaseTestCase):
    def _make_client(self) -> AsyncClient:
        app = create_app()
        app.state.session_factory = self.session_factory
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://portal.test")

    async def _login(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/auth/login",
            json={"username": "auditor", "password": "auditor"},
        )
        assert response.status_code == 200, response.text

    async def _seed(self, project_name: str, statuses: list[str]) -> None:
        async with self.session_factory() as session:
            for i, status in enumerate(statuses):
                session.add(
                    AuditRun(
                        repo_root=f"/tmp/{project_name}",
                        project_name=project_name,
                        build_fingerprint=f"bf-{project_name}-{i}",
                        mode="single",
                        status=status,
                    )
                )
            await session.commit()

    async def test_running_audit_runs_zero_when_only_terminal_statuses(
        self,
    ) -> None:
        await self._seed(
            "terminal-only",
            ["completed", "failed", "cancelled", "completed"],
        )
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get("/api/projects")
        self.assertEqual(response.status_code, 200, response.text)
        row = _row_for(response.json(), "terminal-only")
        self.assertEqual(row["running_audit_runs"], 0)
        self.assertEqual(row["total_audit_runs"], 4)

    async def test_running_audit_runs_counts_only_in_progress_rows(
        self,
    ) -> None:
        await self._seed(
            "mixed",
            [
                "in_progress",
                "in_progress",
                "in_progress",
                "completed",
                "failed",
                "cancelled",
            ],
        )
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get("/api/projects")
        self.assertEqual(response.status_code, 200, response.text)
        row = _row_for(response.json(), "mixed")
        self.assertEqual(row["running_audit_runs"], 3)
        self.assertEqual(row["total_audit_runs"], 6)

    async def test_running_audit_runs_isolated_per_project(self) -> None:
        await self._seed("alpha", ["in_progress", "completed"])
        await self._seed("beta", ["completed", "completed"])
        await self._seed("gamma", ["in_progress", "in_progress", "failed"])
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get("/api/projects")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(_row_for(body, "alpha")["running_audit_runs"], 1)
        self.assertEqual(_row_for(body, "beta")["running_audit_runs"], 0)
        self.assertEqual(_row_for(body, "gamma")["running_audit_runs"], 2)

    async def test_running_count_uses_a_single_aggregate_query(
        self,
    ) -> None:
        """The ``running`` column SHALL be a column on the same aggregate
        group-by, not an extra per-row follow-up query.

        We count statements that touch ``report.audit_runs`` and contain
        a ``GROUP BY`` clause while serving ``GET /api/projects``: there
        should be exactly two — the ``COUNT(*) over subquery`` for
        pagination total, and the ``SELECT … GROUP BY repo_root,
        project_name`` for the page items.
        """

        await self._seed("solo", ["in_progress", "completed", "completed"])

        engine: AsyncEngine = self._engine
        statements: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture(
            conn, cursor, statement, parameters, context, executemany
        ):  # noqa: ANN001 — SQLAlchemy event signature
            statements.append(statement)

        try:
            async with self._make_client() as client:
                await self._login(client)
                response = await client.get("/api/projects")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

        self.assertEqual(response.status_code, 200, response.text)

        aggregate_statements = [
            s
            for s in statements
            if re.search(
                r"audit_runs[\s\S]+GROUP BY",
                s,
                re.IGNORECASE,
            )
        ]
        self.assertEqual(
            len(aggregate_statements),
            2,
            f"expected 2 aggregate statements (count + items), got "
            f"{len(aggregate_statements)}: {aggregate_statements}",
        )
        for s in aggregate_statements:
            self.assertRegex(
                s,
                r"(?i)sum\(\s*case",
                "expected the aggregate to include the SUM(CASE ...) "
                "running-count column; instead got: " + s,
            )


if __name__ == "__main__":
    unittest.main()
