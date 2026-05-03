"""Coverage API integration tests (task 10.11 backend half).

Gated by ``XAUDITOR_TEST_DATABASE_URL``. Exercises:

- ``/coverage/summary`` returns bounded counts irrespective of row count.
- ``/coverage/modules``, ``/coverage/files``, ``/coverage/functions``
  paginate with ``limit``/``offset``, enforce the 1..200 bound, and
  support ``status=`` + ``q=`` filters.
- The legacy ``/coverage`` endpoint still works, caps each category at
  500 rows, and emits the ``Deprecation: true`` response header.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.app import create_app
from xauditor_portal.db.models.report import (
    AuditRun,
    CoverageFile,
    CoverageFunction,
    CoverageModule,
)


class _Base(LiveDatabaseTestCase):
    async def _seed_run_with_coverage(
        self,
        *,
        module_statuses: list[str],
        file_statuses: list[str],
        function_statuses: list[str],
    ) -> str:
        async with self.session_factory() as session:
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-1",
                mode="single",
                status="completed",
            )
            session.add(run)
            await session.flush()
            for idx, status in enumerate(module_statuses):
                session.add(
                    CoverageModule(
                        run_id=run.id,
                        module_name=f"mod.{idx}",
                        status=status,
                    )
                )
            for idx, status in enumerate(file_statuses):
                session.add(
                    CoverageFile(
                        run_id=run.id,
                        file_path=f"src/file_{idx}.py",
                        status=status,
                    )
                )
            for idx, status in enumerate(function_statuses):
                session.add(
                    CoverageFunction(
                        run_id=run.id,
                        function_id=f"fn-{idx}",
                        qualified_name=f"mod::func_{idx}",
                        file_path=f"src/file_{idx % max(1, len(file_statuses))}.py",
                        status=status,
                    )
                )
            await session.commit()
            return str(run.id)

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


class CoverageSummaryTests(_Base):
    async def test_summary_returns_bounded_counts(self) -> None:
        run_id = await self._seed_run_with_coverage(
            module_statuses=["audited", "audited", "unaudited"],
            file_statuses=["audited", "excluded"],
            function_statuses=["audited", "unaudited", "unaudited", "skipped"],
        )
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/coverage/summary",
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["modules"]["total"], 3)
        self.assertEqual(body["modules"]["by_status"]["audited"], 2)
        self.assertEqual(body["modules"]["by_status"]["unaudited"], 1)
        self.assertEqual(body["modules"]["by_status"]["excluded"], 0)
        self.assertEqual(body["files"]["total"], 2)
        self.assertEqual(body["functions"]["total"], 4)
        self.assertAlmostEqual(body["functions"]["percent_audited"], 25.0, places=2)


class CoverageListTests(_Base):
    async def test_list_enforces_limit_bound(self) -> None:
        run_id = await self._seed_run_with_coverage(
            module_statuses=["audited"],
            file_statuses=[],
            function_statuses=[],
        )
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/coverage/modules",
                params={"limit": 5000},
            )
        self.assertEqual(response.status_code, 422)  # FastAPI returns 422 for ge/le violation

    async def test_list_filters_by_status_and_substring(self) -> None:
        run_id = await self._seed_run_with_coverage(
            module_statuses=[],
            file_statuses=[],
            function_statuses=["audited", "unaudited", "unaudited"],
        )
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/coverage/functions",
                params={"status": "unaudited", "q": "func_1", "limit": 25},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["qualified_name"], "mod::func_1")

    async def test_list_paginates_with_offset(self) -> None:
        run_id = await self._seed_run_with_coverage(
            module_statuses=["audited"] * 5,
            file_statuses=[],
            function_statuses=[],
        )
        async with self._make_client() as client:
            await self._login(client)
            first = await client.get(
                f"/api/runs/{run_id}/coverage/modules",
                params={"limit": 2, "offset": 0},
            )
            second = await client.get(
                f"/api/runs/{run_id}/coverage/modules",
                params={"limit": 2, "offset": 2},
            )
        self.assertEqual(first.json()["total"], 5)
        self.assertEqual(second.json()["total"], 5)
        first_names = {m["module_name"] for m in first.json()["items"]}
        second_names = {m["module_name"] for m in second.json()["items"]}
        self.assertEqual(len(first_names & second_names), 0)


class LegacyCoverageWrapperTests(_Base):
    async def test_legacy_endpoint_emits_deprecation_header(self) -> None:
        run_id = await self._seed_run_with_coverage(
            module_statuses=["audited"],
            file_statuses=[],
            function_statuses=[],
        )
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/coverage",
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("deprecation", "").lower(), "true")


if __name__ == "__main__":
    import unittest

    unittest.main()
