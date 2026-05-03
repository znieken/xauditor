"""Finding / debate linkage API integration tests.

Regression tests for the fix-portal-finding-debate-linkage change. These
assert that with the sink-side translation in place:

- ``GET /api/runs/{run_id}/findings`` marks ``has_debate=true`` for every
  finding whose ``report.validator_debates.finding_ref`` equals its own
  ``finding_id`` (and false for the others).
- ``GET /api/runs/{run_id}/findings/{finding_id}/debate`` returns 200 with
  the persisted debate JSON when one exists, and 404 otherwise — and the
  404 is specifically the "no debate recorded" path, not a join-key miss.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` via ``LiveDatabaseTestCase``.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.app import create_app
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.report import AuditRun, Finding, ValidatorDebate


class FindingDebateLinkageTests(LiveDatabaseTestCase):
    async def _seed(self) -> tuple[str, str, str]:
        """Insert a team-mode run with two findings and one debate.

        Returns ``(run_id, finding_with_debate_id, finding_without_debate_id)``
        where both *_id values are the ``finding_id`` strings (``F-NNNN``),
        not the UUID row ids.
        """

        async with self.session_factory() as session:
            await session.execute(select(User))  # sanity: bootstrap user exists
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-debate",
                mode="team",
                status="completed",
                valid_findings=1,
                false_positives=1,
                total_candidates=2,
            )
            session.add(run)
            await session.flush()
            with_debate = Finding(
                run_id=run.id,
                finding_id="F-0001",
                path_fingerprint="path-debate",
                finding_name="has debate",
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
            without_debate = Finding(
                run_id=run.id,
                finding_id="F-0002",
                path_fingerprint="path-no-debate",
                finding_name="no debate",
                finding_description="",
                confidence_level="Medium",
                analysis="",
                reason="",
                context="",
                business_context="",
                exploitation_status="not_exploitable",
                exploitation_steps="",
                validation_status="False Positive",
                validation_analysis="",
            )
            session.add_all([with_debate, without_debate])
            await session.flush()
            session.add(
                ValidatorDebate(
                    run_id=run.id,
                    path_fingerprint="path-debate",
                    finding_ref="F-0001",
                    rounds=[
                        {
                            "round_index": 0,
                            "turns": [
                                {
                                    "subagent_index": 0,
                                    "provider_name": "prov-a",
                                    "verdict": "Valid",
                                    "rebuttal": "agree",
                                    "raw_response": "{}",
                                    "system_prompt": "",
                                    "user_message": "",
                                }
                            ],
                        }
                    ],
                    final_verdict="Valid",
                    convergence_state="converged",
                    configured_round_cap=3,
                )
            )
            await session.commit()
            return str(run.id), "F-0001", "F-0002"

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

    async def _row_id_for(self, finding_id: str) -> str:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Finding).where(Finding.finding_id == finding_id)
                )
            ).scalar_one()
            return str(row.id)

    async def test_list_findings_marks_has_debate_for_findings_with_a_debate(
        self,
    ) -> None:
        run_id, with_debate_id, without_debate_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}/findings")
        self.assertEqual(response.status_code, 200, response.text)
        by_finding_id = {item["finding_id"]: item for item in response.json()}
        self.assertTrue(by_finding_id[with_debate_id]["has_debate"])
        self.assertFalse(by_finding_id[without_debate_id]["has_debate"])

    async def test_finding_debate_endpoint_returns_200_when_debate_exists(
        self,
    ) -> None:
        run_id, with_debate_id, _ = await self._seed()
        row_id = await self._row_id_for(with_debate_id)
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings/{row_id}/debate"
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["finding_ref"], with_debate_id)
        self.assertEqual(body["final_verdict"], "Valid")
        self.assertEqual(body["convergence_state"], "converged")
        self.assertEqual(body["configured_round_cap"], 3)
        self.assertEqual(len(body["rounds"]), 1)
        self.assertEqual(body["rounds"][0]["round_index"], 0)

    async def test_finding_debate_endpoint_returns_404_when_no_debate(
        self,
    ) -> None:
        run_id, _, without_debate_id = await self._seed()
        row_id = await self._row_id_for(without_debate_id)
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings/{row_id}/debate"
            )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn(
            "no validator debate", response.json()["detail"].lower()
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
