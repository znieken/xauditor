"""Integration tests for the coder-aware run-detail and run-list metrics.

Covers ``portal-coder-fail-default-and-filtered-valid-counts``:

- The shared ``_run_metrics`` / ``_batch_run_metrics`` pivot applies a
  server-side default coder filter (``Verified, Inconclusive, Pending,
  Skipped``) when the caller does not pass ``coder_status``.
- The run-list endpoints (``GET /api/runs`` and the per-build runs
  endpoint at ``GET /api/projects/{p}/builds/{b}/runs``) carry the default
  and consequently hide findings whose ``coder_status`` is ``Fail`` or
  ``Not Verified`` from the per-run scalar counts.
- ``GET /api/runs/{run_id}`` accepts a repeatable ``coder_status`` query
  param. With no param, the default applies. With explicit values, those
  values restrict the counts. With the present-but-empty marker
  ``?coder_status=``, no coder restriction is applied (matches the
  findings endpoint's "user unchecked everything" semantics).
- The cached ``audit_runs.valid_findings`` column is NOT read at request
  time — the API result comes from the live pivot even when the cached
  scalar disagrees.

Gated by ``XAUDITOR_TEST_DATABASE_URL`` via ``LiveDatabaseTestCase``.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.api.projects import project_key
from xauditor_portal.app import create_app
from xauditor_portal.db.models.report import (
    AuditRun,
    CoderFinding,
    Finding,
)


class RunMetricsCoderFilterTests(LiveDatabaseTestCase):
    REPO_ROOT = "/tmp/coder-metrics-repo"
    PROJECT_NAME = "coder-metrics-repo"
    BUILD_FINGERPRINT = "bf-coder-metrics"

    async def _seed(self) -> str:
        """Insert a run with six findings: one per coder_status outcome.

        - F-1 → CoderFinding(Verified)
        - F-2 → CoderFinding(Inconclusive)
        - F-3 → CoderFinding(Pending)
        - F-4 → no CoderFinding row (surfaces as Skipped)
        - F-5 → CoderFinding(Fail)            ← excluded by default
        - F-6 → CoderFinding(Not Verified)    ← excluded by default

        Every finding has ``validation_status = 'Valid'`` so the
        valid-side base count equals the number of findings inside the
        active coder filter.
        """

        async with self.session_factory() as session:
            run = AuditRun(
                repo_root=self.REPO_ROOT,
                project_name=self.PROJECT_NAME,
                build_fingerprint=self.BUILD_FINGERPRINT,
                mode="single",
                status="completed",
                # Cached scalar deliberately disagrees with the live pivot.
                valid_findings=999,
                false_positives=999,
                total_candidates=6,
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
                for i in range(1, 7)
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
                        status="Inconclusive",
                        analysis="",
                        reason="",
                    ),
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-3",
                        status="Pending",
                        analysis="",
                        reason="",
                    ),
                    # F-4 → no CoderFinding row.
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-5",
                        status="Fail",
                        analysis="",
                        reason="",
                    ),
                    CoderFinding(
                        run_id=run.id,
                        finding_ref="F-6",
                        status="Not Verified",
                        analysis="",
                        reason="",
                    ),
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

    async def test_run_detail_default_filter_excludes_fail_and_not_verified(
        self,
    ) -> None:
        """First-open detail page applies the server default.

        The seed has six Valid-bucket findings; the default filter
        ``[Verified, Inconclusive, Pending, Skipped]`` covers F-1..F-4 =
        4 findings. The cached ``audit_runs.valid_findings = 999`` is
        intentionally wrong to verify the API path uses the live pivot.
        """

        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["valid_findings"], 4)
        self.assertEqual(body["valid_findings_breakdown"]["base"], 4)
        self.assertEqual(body["valid_findings_breakdown"]["net"], 4)
        # Cached scalar disagrees with the live count — proves we read the
        # pivot, not the column.
        self.assertNotEqual(body["valid_findings"], 999)

    async def test_run_detail_explicit_filter_includes_fail(self) -> None:
        """Passing ``coder_status=[Verified, Fail]`` widens the scope."""

        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}",
                params=[
                    ("coder_status", "Verified"),
                    ("coder_status", "Fail"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # Filter set covers F-1 (Verified) and F-5 (Fail) → 2 findings.
        self.assertEqual(body["valid_findings"], 2)
        self.assertEqual(body["valid_findings_breakdown"]["base"], 2)

    async def test_run_detail_empty_marker_disables_coder_restriction(
        self,
    ) -> None:
        """``?coder_status=`` means "user unchecked everything" → all rows."""

        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/runs/{run_id}",
                params=[("coder_status", "")],
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # No coder restriction → every Valid-bucket finding counts. Seed
        # has six.
        self.assertEqual(body["valid_findings"], 6)
        self.assertEqual(body["valid_findings_breakdown"]["base"], 6)

    async def test_list_runs_applies_default_filter(self) -> None:
        """``GET /api/runs`` returns per-run scalars under the default."""

        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                "/api/runs",
                params={"project": self.PROJECT_NAME},
            )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        matching = [item for item in items if item["id"] == run_id]
        self.assertEqual(len(matching), 1)
        # Same expected count as the run-detail default scenario.
        self.assertEqual(matching[0]["valid_findings"], 4)

    async def test_build_runs_endpoint_applies_default_filter(self) -> None:
        """``GET /api/projects/{p}/builds/{b}/runs`` mirrors the default."""

        run_id = await self._seed()
        pk = project_key(self.REPO_ROOT, self.PROJECT_NAME)
        async with self._make_client() as client:
            await self._login(client)
            response = await client.get(
                f"/api/projects/{pk}/builds/{self.BUILD_FINGERPRINT}/runs",
            )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        matching = [item for item in items if item["id"] == run_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["valid_findings"], 4)

    async def test_run_list_and_default_run_detail_agree(self) -> None:
        """The list-row scalar matches the detail page's first-open tile."""

        run_id = await self._seed()
        async with self._make_client() as client:
            await self._login(client)
            list_resp = await client.get(
                "/api/runs",
                params={"project": self.PROJECT_NAME},
            )
            detail_resp = await client.get(f"/api/runs/{run_id}")
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        self.assertEqual(detail_resp.status_code, 200, detail_resp.text)
        list_row = next(
            item for item in list_resp.json()["items"] if item["id"] == run_id
        )
        detail_body = detail_resp.json()
        self.assertEqual(list_row["valid_findings"], detail_body["valid_findings"])
