"""GET /api/runs list endpoint live-count integration tests.

Verifies that the batched ``_batch_run_metrics`` SQL pivot returns the
same per-run counts as the run-detail endpoint, so that PATCH-ing a
feedback annotation updates the row scalars on the runs-list view too.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` like the other live-DB tests.
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


class RunsListBatchedMetricsTests(LiveDatabaseTestCase):
    async def _seed_two_runs(self) -> tuple[str, str, str, str, str]:
        """Insert two runs sharing a project; each has one Valid and one FP.

        Returns ``(run_a_id, run_b_id, run_b_valid_finding_id,
        run_b_fp_finding_id, user_id)``. The list endpoint orders by
        ``started_at desc`` so we'll be checking both rows.
        """

        async with self.session_factory() as session:
            user = (await session.execute(select(User))).scalar_one()
            run_a = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-1",
                mode="single",
                status="completed",
                valid_findings=1,
                false_positives=1,
                total_candidates=2,
            )
            run_b = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-1",
                mode="single",
                status="completed",
                valid_findings=1,
                false_positives=1,
                total_candidates=2,
            )
            session.add_all([run_a, run_b])
            await session.flush()

            findings = []
            for run, prefix in ((run_a, "A"), (run_b, "B")):
                findings.append(
                    Finding(
                        run_id=run.id,
                        finding_id=f"F-{prefix}-VALID",
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
                )
                findings.append(
                    Finding(
                        run_id=run.id,
                        finding_id=f"F-{prefix}-FP",
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
                )
            session.add_all(findings)
            await session.commit()

            run_b_valid = next(
                f for f in findings if f.finding_id == "F-B-VALID"
            )
            run_b_fp = next(f for f in findings if f.finding_id == "F-B-FP")
            return (
                str(run_a.id),
                str(run_b.id),
                str(run_b_valid.id),
                str(run_b_fp.id),
                str(user.id),
            )

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

    def _row_for(self, body: dict, run_id: str) -> dict:
        for row in body["items"]:
            if row["id"] == run_id:
                return row
        raise AssertionError(f"run {run_id} not in list response: {body}")

    async def test_list_endpoint_returns_live_counts_per_run(self) -> None:
        run_a_id, run_b_id, b_valid_id, b_fp_id, user_id = (
            await self._seed_two_runs()
        )
        # PATCH-equivalent: directly write a duplicate annotation on
        # run B's Valid finding and a true_positive on run B's FP.
        # Run A stays untouched; its scalars should reflect "no
        # feedback".
        async with self.session_factory() as session:
            session.add_all(
                [
                    FindingAnnotation(
                        run_id=uuid.UUID(run_b_id),
                        finding_id=uuid.UUID(b_valid_id),
                        label="duplicate",
                        duplicate_of_finding_id=uuid.UUID(b_valid_id),
                        reviewer_user_id=uuid.UUID(user_id),
                    ),
                    FindingAnnotation(
                        run_id=uuid.UUID(run_b_id),
                        finding_id=uuid.UUID(b_fp_id),
                        label="true_positive",
                        reviewer_user_id=uuid.UUID(user_id),
                    ),
                ]
            )
            await session.commit()

        async with self._make_client() as client:
            await self._login(client)
            response = await client.get("/api/runs")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 2)

        # Run A: no annotations -> Valid bucket retains its agent base of
        # 1; ``false_positives`` is feedback-derived and lacks any
        # ``label='false_positive'`` row, so it stays at 0 even though
        # one finding's ``validation_status`` is "False Positive".
        row_a = self._row_for(body, run_a_id)
        self.assertEqual(row_a["valid_findings"], 1)
        self.assertEqual(row_a["false_positives"], 0)
        self.assertEqual(row_a["duplicate_findings"], 0)
        self.assertEqual(row_a["unlabeled_findings"], 2)
        # `portal-show-stages-form`: every run row carries the
        # `stages_form` field; runs the seed inserts without setting
        # it land on the column default `'prompt'`.
        self.assertEqual(row_a["stages_form"], "prompt")

        # Run B: Valid finding marked duplicate, FP finding marked TP.
        # Valid bucket: base 1 + 0 added - 0 removed - 1 dup = 0.
        # ``false_positives`` is feedback-derived and no annotation has
        # ``label='false_positive'`` here, so it's 0.
        # Duplicate finding count = 1.
        # Unlabeled = 0 (both findings have annotation rows with
        # non-unlabeled labels).
        row_b = self._row_for(body, run_b_id)
        self.assertEqual(row_b["valid_findings"], 0)
        self.assertEqual(row_b["false_positives"], 0)
        self.assertEqual(row_b["duplicate_findings"], 1)
        self.assertEqual(row_b["unlabeled_findings"], 0)
        self.assertEqual(row_b["stages_form"], "prompt")


if __name__ == "__main__":
    import unittest

    unittest.main()
