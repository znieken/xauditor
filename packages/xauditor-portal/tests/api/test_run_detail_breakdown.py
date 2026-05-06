"""RunDetail metrics integration tests.

Gated by ``XAUDITOR_TEST_DATABASE_URL``. Asserts that:

- The valid-side base+adjustment breakdown still exposes
  ``base / added_by_feedback / removed_by_feedback / duplicates_in_bucket / net``
  with the agent-base + human-feedback overlay shape.
- The ``false_positives`` scalar counts findings whose latest feedback
  annotation has ``label = 'false_positive'`` only — agent-written
  ``validation_status = 'False Positive'`` does not contribute. The
  response no longer carries a ``false_positives_breakdown`` block.
- ``duplicate`` annotations never contribute to ``false_positives``,
  regardless of the agent-written ``validation_status``.
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

    async def test_stages_form_round_trips_via_run_detail(self) -> None:
        """`portal-show-stages-form`: the API surfaces stages_form for both
        defaulted (`'prompt'`) and explicit (`'agentic'`) runs."""

        run_id, _, _, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        # Seed inserts the run without specifying stages_form, so the
        # column default is what the API returns.
        self.assertEqual(body["stages_form"], "prompt")

        # A second run inserted with stages_form='agentic' surfaces the
        # explicit value through the same endpoint.
        async with self.session_factory() as session:
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-2",
                mode="deep",
                stages_form="agentic",
                status="completed",
                total_candidates=0,
            )
            session.add(run)
            await session.commit()
            agentic_id = str(run.id)
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{agentic_id}")
        body = response.json()
        self.assertEqual(body["stages_form"], "agentic")

    async def test_no_annotations_produces_zero_fp_and_clean_valid_breakdown(
        self,
    ) -> None:
        """Spec scenario: agent-flagged FP with no feedback => FP count 0."""

        run_id, _, _, _ = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        valid = body["valid_findings_breakdown"]
        self.assertEqual(valid["base"], 1)
        self.assertEqual(valid["added_by_feedback"], 0)
        self.assertEqual(valid["removed_by_feedback"], 0)
        self.assertEqual(valid["duplicates_in_bucket"], 0)
        self.assertEqual(valid["net"], 1)
        self.assertEqual(body["valid_findings"], 1)
        # Agent verdict alone never contributes to false_positives.
        self.assertEqual(body["false_positives"], 0)
        self.assertNotIn("false_positives_breakdown", body)
        self.assertEqual(body["unlabeled_findings"], 2)
        self.assertEqual(body["duplicate_findings"], 0)
        # `portal-drop-candidates-and-rebase-positive-rate`: valid_rate
        # now means agent precision after feedback —
        # valid_net / base_valid. Fixture has 1 Valid + 1 FP finding,
        # no human feedback: base_valid = 1, valid_net = 1, rate = 1.0
        # (previously 1/2 = 0.5 with denom = total - duplicates).
        self.assertAlmostEqual(body["valid_rate"], 1.0, places=4)

    async def test_true_positive_on_fp_moves_one_into_valid(self) -> None:
        run_id, _, fp_row_id, user_id = await self._seed()
        async with self.session_factory() as session:
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
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        valid = body["valid_findings_breakdown"]
        self.assertEqual(valid["base"], 1)
        self.assertEqual(valid["added_by_feedback"], 1)
        self.assertEqual(valid["net"], 2)
        self.assertEqual(body["valid_findings"], 2)
        # No annotation has label='false_positive', so FP stays 0.
        self.assertEqual(body["false_positives"], 0)

    async def test_false_positive_on_valid_increments_fp_count(self) -> None:
        """Spec scenario: reviewer flags a Valid finding => FP count = 1."""

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
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        valid = body["valid_findings_breakdown"]
        self.assertEqual(valid["removed_by_feedback"], 1)
        self.assertEqual(valid["net"], 0)
        self.assertEqual(body["valid_findings"], 0)
        # Exactly one finding carries label='false_positive'.
        self.assertEqual(body["false_positives"], 1)

    async def test_duplicate_on_valid_does_not_change_fp_count(self) -> None:
        """Spec scenario: duplicate annotation never affects FP count."""

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
        self.assertEqual(valid["duplicates_in_bucket"], 1)
        self.assertEqual(valid["net"], 0)
        self.assertEqual(body["false_positives"], 0)
        self.assertEqual(body["duplicate_findings"], 1)

    async def test_duplicate_on_fp_does_not_change_fp_count(self) -> None:
        """Even when validation_status is 'False Positive', a duplicate
        annotation does not contribute to the feedback-derived FP total."""

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
        self.assertEqual(body["false_positives"], 0)
        self.assertEqual(body["duplicate_findings"], 1)

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
        # An explicit ``label='unlabeled'`` row counts the same as no row.
        self.assertEqual(body["unlabeled_findings"], 2)
        self.assertEqual(body["valid_findings"], 1)
        self.assertEqual(body["false_positives"], 0)

    async def test_valid_rate_zero_when_every_valid_finding_is_duplicate(
        self,
    ) -> None:
        # Renamed + re-asserted under the new formula.
        # `portal-drop-candidates-and-rebase-positive-rate`:
        # valid_rate = valid_net / base_valid. Fixture has 1 Valid +
        # 1 FP. The reviewer marks both as duplicate. The Valid
        # finding's validation_status is unchanged (still in
        # _VALID_STATUSES) so base_valid = 1; valid_net subtracts
        # the duplicate, so valid_net = 0. Rate = 0/1 = 0.0
        # (previously null because denom = total - duplicates = 0).
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
        self.assertAlmostEqual(body["valid_rate"], 0.0, places=4)
        self.assertEqual(body["duplicate_findings"], 2)
        self.assertEqual(body["unlabeled_findings"], 0)
        self.assertEqual(body["false_positives"], 0)

    async def test_valid_rate_null_when_no_agent_validated_findings(
        self,
    ) -> None:
        # New scenario for the re-based formula: when the agent
        # rejected everything (only FP findings), base_valid = 0
        # and valid_rate is null. Build a single-FP fixture.
        async with self.session_factory() as session:
            user = (await session.execute(select(User))).scalar_one()
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-fp-only",
                mode="single",
                status="completed",
                valid_findings=0,
                false_positives=1,
                total_candidates=1,
            )
            session.add(run)
            await session.flush()
            fp = Finding(
                run_id=run.id,
                finding_id="F-FP-ONLY",
                finding_name="fp",
                finding_description="",
                confidence_level="High",
                analysis="",
                reason="",
                context="",
                business_context="",
                exploitation_status="not_exploitable",
                exploitation_steps="",
                validation_status="False Positive",
                validation_analysis="",
            )
            session.add(fp)
            await session.commit()
            run_id = str(run.id)

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        self.assertIsNone(body["valid_rate"])
        self.assertEqual(body["valid_findings"], 0)
        self.assertEqual(body["false_positives"], 0)

    async def test_valid_rate_above_one_when_reviewer_promotes_more_fp(
        self,
    ) -> None:
        # Reviewer marks the FP finding as true_positive without
        # downgrading any Valid: net = 1 + 1 - 0 - 0 = 2,
        # base_valid = 1, rate = 2/1 = 2.0. Surfaces the
        # >100% case the spec's design.md flagged.
        run_id, _, fp_row_id, user_id = await self._seed()
        async with self.session_factory() as session:
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
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        self.assertAlmostEqual(body["valid_rate"], 2.0, places=4)


if __name__ == "__main__":
    import unittest

    unittest.main()
