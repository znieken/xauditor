"""RunDetail feedback-breakdown integration tests (task 13.6).

Gated by ``XAUDITOR_TEST_DATABASE_URL``. Asserts that:

- A run with no annotations returns zero-delta breakdowns.
- Labeling a validator-FP finding as ``true_positive`` increments
  ``valid_findings_breakdown.added_by_feedback`` by one and
  ``false_positives_breakdown.removed_by_feedback`` by one in the same
  response.
- Scalar ``valid_findings`` / ``false_positives`` fields keep their
  validator-base values (list endpoints don't gain the breakdown).
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


class RunDetailBreakdownTests(LiveDatabaseTestCase):
    async def _seed(self) -> tuple[str, str, str, str]:
        """Insert a run with one Valid finding and one False-Positive finding.

        Returns ``(run_id, valid_finding_row_id, fp_finding_row_id, user_id)``.
        """

        async with self.session_factory() as session:
            user = (await session.execute(select(User))).scalar_one()
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-1",
                mode="single",
                status="completed",
                valid_findings=1,
                false_positives=1,
                total_candidates=2,
            )
            session.add(run)
            await session.flush()
            valid = Finding(
                run_id=run.id,
                finding_id="F-VALID",
                finding_name="valid",
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
            fp = Finding(
                run_id=run.id,
                finding_id="F-FP",
                finding_name="fp",
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
            session.add_all([valid, fp])
            await session.commit()
            return str(run.id), str(valid.id), str(fp.id), str(user.id)

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

    async def test_no_annotations_produces_zero_deltas(self) -> None:
        run_id, _, _, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}",
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        valid = body["valid_findings_breakdown"]
        fp = body["false_positives_breakdown"]
        self.assertEqual(valid["base"], 1)
        self.assertEqual(valid["added_by_feedback"], 0)
        self.assertEqual(valid["removed_by_feedback"], 0)
        self.assertEqual(valid["duplicates_in_bucket"], 0)
        self.assertEqual(valid["net"], 1)
        self.assertEqual(fp["base"], 1)
        self.assertEqual(fp["added_by_feedback"], 0)
        self.assertEqual(fp["removed_by_feedback"], 0)
        self.assertEqual(fp["duplicates_in_bucket"], 0)
        self.assertEqual(fp["net"], 1)
        # Scalar fields equal the breakdown nets (live JOIN, not stale columns).
        self.assertEqual(body["valid_findings"], 1)
        self.assertEqual(body["false_positives"], 1)
        # No annotations => both findings still in the review queue.
        self.assertEqual(body["unlabeled_findings"], 2)
        self.assertEqual(body["duplicate_findings"], 0)
        # 2 findings, 0 duplicates, valid_net = 1 => valid_rate = 0.5.
        self.assertAlmostEqual(body["valid_rate"], 0.5, places=4)

    async def test_true_positive_on_fp_moves_one_into_valid(self) -> None:
        run_id, _, fp_row_id, user_id = await self._seed()
        async with self.session_factory() as session:
            # Human labels the validator-FP finding as true_positive.
            session.add(
                FindingAnnotation(
                    run_id=uuid.UUID(run_id),
                    finding_id=uuid.UUID(fp_row_id),
                    label="true_positive",
                    reviewer_user_id=uuid.UUID(user_id),
                )
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}",
            )
        body = response.json()
        valid = body["valid_findings_breakdown"]
        fp = body["false_positives_breakdown"]
        # validator base unchanged on both buckets.
        self.assertEqual(valid["base"], 1)
        self.assertEqual(fp["base"], 1)
        # overlay moves one finding.
        self.assertEqual(valid["added_by_feedback"], 1)
        self.assertEqual(fp["removed_by_feedback"], 1)
        # nets reflect the move.
        self.assertEqual(valid["net"], 2)
        self.assertEqual(fp["net"], 0)

    async def test_false_positive_on_valid_moves_one_out_of_valid(self) -> None:
        run_id, valid_row_id, _, user_id = await self._seed()
        async with self.session_factory() as session:
            session.add(
                FindingAnnotation(
                    run_id=uuid.UUID(run_id),
                    finding_id=uuid.UUID(valid_row_id),
                    label="false_positive",
                    reviewer_user_id=uuid.UUID(user_id),
                )
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}",
            )
        body = response.json()
        valid = body["valid_findings_breakdown"]
        fp = body["false_positives_breakdown"]
        self.assertEqual(valid["removed_by_feedback"], 1)
        self.assertEqual(fp["added_by_feedback"], 1)
        self.assertEqual(valid["net"], 0)
        self.assertEqual(fp["net"], 2)

    async def test_duplicate_on_valid_subtracts_from_valid_bucket(self) -> None:
        run_id, valid_row_id, _, user_id = await self._seed()
        async with self.session_factory() as session:
            session.add(
                FindingAnnotation(
                    run_id=uuid.UUID(run_id),
                    finding_id=uuid.UUID(valid_row_id),
                    label="duplicate",
                    duplicate_of_finding_id=uuid.UUID(valid_row_id),
                    reviewer_user_id=uuid.UUID(user_id),
                )
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        valid = body["valid_findings_breakdown"]
        fp = body["false_positives_breakdown"]
        self.assertEqual(valid["base"], 1)
        self.assertEqual(valid["duplicates_in_bucket"], 1)
        self.assertEqual(valid["net"], 0)
        self.assertEqual(fp["duplicates_in_bucket"], 0)
        self.assertEqual(fp["net"], 1)
        self.assertEqual(body["valid_findings"], 0)
        self.assertEqual(body["false_positives"], 1)
        self.assertEqual(body["duplicate_findings"], 1)
        self.assertEqual(body["unlabeled_findings"], 1)
        # total=2, duplicates=1, denom=1, valid_net=0 => valid_rate=0.0
        self.assertEqual(body["valid_rate"], 0.0)

    async def test_duplicate_on_fp_subtracts_from_fp_bucket(self) -> None:
        run_id, _, fp_row_id, user_id = await self._seed()
        async with self.session_factory() as session:
            session.add(
                FindingAnnotation(
                    run_id=uuid.UUID(run_id),
                    finding_id=uuid.UUID(fp_row_id),
                    label="duplicate",
                    duplicate_of_finding_id=uuid.UUID(fp_row_id),
                    reviewer_user_id=uuid.UUID(user_id),
                )
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        valid = body["valid_findings_breakdown"]
        fp = body["false_positives_breakdown"]
        self.assertEqual(fp["base"], 1)
        self.assertEqual(fp["duplicates_in_bucket"], 1)
        self.assertEqual(fp["net"], 0)
        self.assertEqual(valid["duplicates_in_bucket"], 0)
        self.assertEqual(valid["net"], 1)
        self.assertEqual(body["duplicate_findings"], 1)
        self.assertEqual(body["unlabeled_findings"], 1)
        # total=2, duplicates=1, denom=1, valid_net=1 => valid_rate=1.0
        self.assertEqual(body["valid_rate"], 1.0)

    async def test_explicit_unlabeled_label_counts_as_review_queue(self) -> None:
        run_id, valid_row_id, _, user_id = await self._seed()
        async with self.session_factory() as session:
            session.add(
                FindingAnnotation(
                    run_id=uuid.UUID(run_id),
                    finding_id=uuid.UUID(valid_row_id),
                    label="unlabeled",
                    reviewer_user_id=uuid.UUID(user_id),
                )
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        # An explicit ``label='unlabeled'`` row counts the same as no row:
        # the finding is still in the review queue.
        self.assertEqual(body["unlabeled_findings"], 2)
        # The Valid bucket still includes the explicitly-unlabeled finding
        # (the breakdown's ``base_valid`` is independent of the human label).
        self.assertEqual(body["valid_findings"], 1)
        self.assertEqual(body["false_positives"], 1)

    async def test_valid_rate_null_when_every_finding_is_duplicate(self) -> None:
        run_id, valid_row_id, fp_row_id, user_id = await self._seed()
        async with self.session_factory() as session:
            session.add_all(
                [
                    FindingAnnotation(
                        run_id=uuid.UUID(run_id),
                        finding_id=uuid.UUID(valid_row_id),
                        label="duplicate",
                        duplicate_of_finding_id=uuid.UUID(valid_row_id),
                        reviewer_user_id=uuid.UUID(user_id),
                    ),
                    FindingAnnotation(
                        run_id=uuid.UUID(run_id),
                        finding_id=uuid.UUID(fp_row_id),
                        label="duplicate",
                        duplicate_of_finding_id=uuid.UUID(fp_row_id),
                        reviewer_user_id=uuid.UUID(user_id),
                    ),
                ]
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        # total=2, duplicates_total=2, denom=0 -> valid_rate is null.
        self.assertIsNone(body["valid_rate"])
        self.assertEqual(body["duplicate_findings"], 2)
        self.assertEqual(body["unlabeled_findings"], 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
