"""GET /api/runs/{id}/findings filter integration tests.

Gated by ``XAUDITOR_TEST_DATABASE_URL``. Verifies that:

- Single-value query params (``?validation_status=Valid``) keep working.
- Repeated query params (``?validation_status=Valid&validation_status=Inconclusive``)
  apply ``Finding.<col> IN (...)`` for ``confidence``, ``validation_status``,
  ``exploitation_status``.
- Multi-value ``feedback_label`` applies the post-filter against the list.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.app import create_app
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.feedback import FindingAnnotation
from xauditor_portal.db.models.report import AuditRun, Finding


class FindingsFilterTests(LiveDatabaseTestCase):
    async def _seed(self) -> tuple[str, dict[str, str]]:
        """Insert a run with five findings spanning every filter dimension.

        Returns ``(run_id, finding_id_by_finding_id)`` so callers can wire
        annotations to specific rows.
        """

        async with self.session_factory() as session:
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-1",
                mode="single",
                status="completed",
                valid_findings=0,
                false_positives=0,
                total_candidates=5,
            )
            session.add(run)
            await session.flush()
            rows = [
                Finding(
                    run_id=run.id,
                    finding_id="F-1",
                    finding_name="auth-bypass",
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
                ),
                Finding(
                    run_id=run.id,
                    finding_id="F-2",
                    finding_name="auth-bypass-mid",
                    finding_description="",
                    confidence_level="Medium",
                    analysis="",
                    reason="",
                    context="",
                    business_context="",
                    exploitation_status="uncertain",
                    exploitation_steps="",
                    validation_status="Inconclusive",
                    validation_analysis="",
                ),
                Finding(
                    run_id=run.id,
                    finding_id="F-3",
                    finding_name="low-noise",
                    finding_description="",
                    confidence_level="Low",
                    analysis="",
                    reason="",
                    context="",
                    business_context="",
                    exploitation_status="not_exploitable",
                    exploitation_steps="",
                    validation_status="Partial Valid",
                    validation_analysis="",
                ),
                Finding(
                    run_id=run.id,
                    finding_id="F-4",
                    finding_name="agent-fp",
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
                ),
                Finding(
                    run_id=run.id,
                    finding_id="F-5",
                    finding_name="another-valid",
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
                ),
            ]
            session.add_all(rows)
            await session.commit()
            return str(run.id), {row.finding_id: str(row.id) for row in rows}

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

    async def test_single_value_validation_status_still_works(self) -> None:
        run_id, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params={"validation_status": "Valid"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        finding_ids = {row["finding_id"] for row in body}
        self.assertEqual(finding_ids, {"F-1", "F-5"})

    async def test_multi_value_validation_status_applies_in_predicate(
        self,
    ) -> None:
        run_id, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                # httpx repeats the key for list values.
                params=[
                    ("validation_status", "Valid"),
                    ("validation_status", "Inconclusive"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        self.assertEqual(finding_ids, {"F-1", "F-2", "F-5"})

    async def test_multi_value_confidence_applies_in_predicate(self) -> None:
        run_id, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("confidence", "High"),
                    ("confidence", "Medium"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        self.assertEqual(finding_ids, {"F-1", "F-2", "F-4", "F-5"})

    async def test_multi_value_exploitation_status_applies_in_predicate(
        self,
    ) -> None:
        run_id, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("exploitation_status", "exploitable"),
                    ("exploitation_status", "uncertain"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        self.assertEqual(finding_ids, {"F-1", "F-2", "F-5"})

    async def test_multi_value_feedback_label_applies_post_filter(self) -> None:
        run_id, by_id = await self._seed()
        async with self.session_factory() as session:
            user = (await session.execute(select(User))).scalar_one()
            session.add_all(
                [
                    FindingAnnotation(
                        run_id=uuid.UUID(run_id),
                        finding_id=uuid.UUID(by_id["F-1"]),
                        label="true_positive",
                        reviewer_user_id=user.id,
                    ),
                    FindingAnnotation(
                        run_id=uuid.UUID(run_id),
                        finding_id=uuid.UUID(by_id["F-2"]),
                        label="false_positive",
                        reviewer_user_id=user.id,
                    ),
                    FindingAnnotation(
                        run_id=uuid.UUID(run_id),
                        finding_id=uuid.UUID(by_id["F-3"]),
                        label="duplicate",
                        duplicate_of_finding_id=uuid.UUID(by_id["F-3"]),
                        reviewer_user_id=user.id,
                    ),
                ]
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("feedback_label", "true_positive"),
                    ("feedback_label", "false_positive"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        self.assertEqual(finding_ids, {"F-1", "F-2"})

    async def test_combined_validation_and_confidence_filters(self) -> None:
        run_id, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}/findings",
                params=[
                    ("validation_status", "Valid"),
                    ("validation_status", "Partial Valid"),
                    ("confidence", "High"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        finding_ids = {row["finding_id"] for row in response.json()}
        # Valid + Partial Valid restricts to F-1, F-3, F-5; High further
        # restricts to F-1 and F-5.
        self.assertEqual(finding_ids, {"F-1", "F-5"})


if __name__ == "__main__":
    import unittest

    unittest.main()
