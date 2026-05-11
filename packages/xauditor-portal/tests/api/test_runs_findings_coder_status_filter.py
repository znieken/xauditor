"""Integration tests for the new ``coder_status`` filter on
``GET /api/runs/{id}/findings``.

Covers ``portal-coder-status-filter`` task 1.5:

- Repeatable query param produces ``COALESCE(coder_status, 'Skipped') IN (...)``.
- Findings without a ``CoderFinding`` row surface as ``Skipped`` and are
  filterable as such.
- Filter is applied at the SQL layer (before ``LIMIT/OFFSET``), so
  pagination is honest under the filter.
- Explicit empty list (no ``coder_status`` query param at all) means
  "no filter" — every row returns regardless of coder verdict.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` via ``LiveDatabaseTestCase``.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.app import create_app
from xauditor_portal.db.models.report import (
    AuditRun,
    CoderFinding,
    Finding,
)


class CoderStatusFilterTests(LiveDatabaseTestCase):
    async def _seed(self) -> str:
        """Insert a run with five findings spanning every coder status.

        Layout:

        - F-1 → CoderFinding(status=Verified)
        - F-2 → CoderFinding(status=Not Verified)
        - F-3 → CoderFinding(status=Inconclusive)
        - F-4 → CoderFinding(status=Fail)
        - F-5 → no CoderFinding row (validator-FP path or
          ``coder.enabled=false``); should surface as Skipped via
          coalesce.
        """

        async with self.session_factory() as session:
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-coder-1",
                mode="single",
                status="completed",
                valid_findings=0,
                false_positives=0,
                total_candidates=5,
            )
            session.add(run)
            await session.flush()
            findings = [
                Finding(
                    run_id=run.id,
                    finding_id=f"F-{i}",
                    finding_name=f"finding-{i}",
                    finding_description="",
                    confidence_level="High",
                    analysis="",
                    reason="",
                    context="",
                    business_context="",
                    exploitation_status="exploitable",
                    exploitation_steps="",
                    validation_status="Valid",
                    validation_analysis="",
                )
                for i in range(1, 6)
            ]
            session.add_all(findings)
            await session.flush()
            session.add_all(
                [
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-1",
                        status="Verified",
                        analysis="",
                        reason="",
                    ),
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-2",
                        status="Not Verified",
                        analysis="",
                        reason="",
                    ),
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-3",
                        status="Inconclusive",
                        analysis="",
                        reason="",
                    ),
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-4",
                        status="Fail",
                        analysis="",
                        reason="",
                    ),
                    # F-5 deliberately omitted.
                ]
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

    async def test_no_filter_returns_every_finding(self) -> None:
        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}/findings")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            {row["finding_id"] for row in body},
            {"F-1", "F-2", "F-3", "F-4", "F-5"},
        )
        # And the per-row ``coder_status`` lands the right value, with the
        # absent row coalesced to ``Skipped``.
        status_by_id = {row["finding_id"]: row["coder_status"] for row in body}
        self.assertEqual(
            status_by_id,
            {
                "F-1": "Verified",
                "F-2": "Not Verified",
                "F-3": "Inconclusive",
                "F-4": "Fail",
                "F-5": "Skipped",
            },
        )

    async def test_single_value_filter_picks_one_status(self) -> None:
        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params={"coder_status": "Verified"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        self.assertEqual(finding_ids, {"F-1"})

    async def test_repeatable_query_param_applies_in_predicate(self) -> None:
        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("coder_status", "Verified"),
                    ("coder_status", "Inconclusive"),
                    ("coder_status", "Fail"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        self.assertEqual(finding_ids, {"F-1", "F-3", "F-4"})

    async def test_skipped_filter_matches_findings_without_coder_row(self) -> None:
        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params={"coder_status": "Skipped"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        # F-5 has no CoderFinding row; the COALESCE(...,'Skipped') in the
        # JOIN clause makes it match the Skipped filter.
        self.assertEqual(finding_ids, {"F-5"})

    async def test_filter_applies_before_limit_offset(self) -> None:
        # Default in the spec is "every status except Not Verified", which
        # leaves four matches (F-1, F-3, F-4, F-5). We drive ``limit=2``
        # to verify pagination operates on the FILTERED set, not the
        # full result before filtering. If the filter were applied after
        # ``LIMIT/OFFSET``, page 1 might be empty (depending on row order)
        # — that's the bug we're guarding against.
        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            page_one = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("coder_status", "Verified"),
                    ("coder_status", "Inconclusive"),
                    ("coder_status", "Fail"),
                    ("coder_status", "Pending"),
                    ("coder_status", "Skipped"),
                    ("limit", "2"),
                    ("offset", "0"),
                ],
            )
            page_two = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("coder_status", "Verified"),
                    ("coder_status", "Inconclusive"),
                    ("coder_status", "Fail"),
                    ("coder_status", "Pending"),
                    ("coder_status", "Skipped"),
                    ("limit", "2"),
                    ("offset", "2"),
                ],
            )
        self.assertEqual(page_one.status_code, 200, page_one.text)
        self.assertEqual(page_two.status_code, 200, page_two.text)
        page_one_ids = {row["finding_id"] for row in page_one.json()}
        page_two_ids = {row["finding_id"] for row in page_two.json()}
        # 4 matches, split 2 + 2 across pages, no overlap.
        self.assertEqual(len(page_one_ids), 2)
        self.assertEqual(len(page_two_ids), 2)
        self.assertEqual(page_one_ids & page_two_ids, set())
        self.assertEqual(
            page_one_ids | page_two_ids, {"F-1", "F-3", "F-4", "F-5"}
        )
