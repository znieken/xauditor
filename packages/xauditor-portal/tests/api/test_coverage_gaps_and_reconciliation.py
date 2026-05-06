"""Coverage Gaps + reconciliation API field tests.

Covers `portal-coverage-panel` Phase 1 backend half (tasks
1.1.1-1.1.4 + 2.1.1-2.1.3): the new `coverage_gaps` field on
`RunDetail` and the new `reconciliation` + `agentic_transcript`
fields on `FindingDetail` are surfaced on the typed API
responses.

Gated by `XAUDITOR_TEST_DATABASE_URL` like the other live-DB
tests.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.app import create_app
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.report import AuditRun, Finding


_FAKE_COVERAGE_GAPS = {
    "audited_classes": ["sql_injection", "command_injection"],
    "skipped_by_mode": ["state_machine_violation"],
    "out_of_scope": ["supply_chain", "business_logic_idor"],
    "mode": "fast",
    "advice_to_user": "Run with `audit.mode: deep` to also cover state.",
}


_FAKE_RECONCILIATION = {
    "per_unit_verdicts": [
        {
            "unit_kind": "path",
            "unit_id": "fp::path",
            "verdict": "Valid",
            "analysis": "path-unit verdict",
        },
        {
            "unit_kind": "sink",
            "unit_id": "sink::db.execute",
            "verdict": "Refuted",
            "analysis": "6/7 inbound paths sanitize",
        },
    ],
    "consolidated_verdict": "Partial Valid",
    "consolidation_reasoning": "Sink unit refutes — downgrade applied.",
}


_FAKE_AGENTIC_TRANSCRIPT = [
    {"tool": "read_file", "input": {"path": "src/app.py"}, "output": "..."},
    {"tool": "final_answer", "input": {"verdict": "Valid"}, "output": "ok"},
]


class RunDetailCoverageGapsTests(LiveDatabaseTestCase):
    async def _seed_run(
        self, *, coverage_gaps: dict | None
    ) -> str:
        async with self.session_factory() as session:
            user = (await session.execute(select(User))).scalar_one()
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-cg-1",
                mode="fast",
                status="completed",
                coverage_gaps=coverage_gaps,
            )
            session.add(run)
            await session.flush()
            await session.commit()
            return str(run.id)

    async def test_run_detail_includes_populated_coverage_gaps(self) -> None:
        run_id = await self._seed_run(coverage_gaps=_FAKE_COVERAGE_GAPS)
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://t"
        ) as client:
            await self.login_admin(client)
            response = await client.get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("coverage_gaps", body)
        self.assertEqual(body["coverage_gaps"], _FAKE_COVERAGE_GAPS)

    async def test_run_detail_returns_null_coverage_gaps_for_pre_phase5(self) -> None:
        run_id = await self._seed_run(coverage_gaps=None)
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://t"
        ) as client:
            await self.login_admin(client)
            response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        # Field is present but null — the frontend's null check is a
        # value comparison, not a key-presence check.
        self.assertIn("coverage_gaps", body)
        self.assertIsNone(body["coverage_gaps"])


class FindingDetailReconciliationTests(LiveDatabaseTestCase):
    async def _seed_finding(
        self,
        *,
        reconciliation: dict | None,
        agentic_transcript: list | None = None,
    ) -> tuple[str, str]:
        async with self.session_factory() as session:
            user = (await session.execute(select(User))).scalar_one()
            run = AuditRun(
                repo_root="/tmp/repo",
                project_name="repo",
                build_fingerprint="bf-rec-1",
                mode="deep",
                status="completed",
            )
            session.add(run)
            await session.flush()
            finding = Finding(
                run_id=run.id,
                finding_id="F-RECON-1",
                finding_name="SQLi in handler",
                finding_description="d",
                confidence_level="High",
                analysis="a",
                reason="r",
                context="c",
                business_context="b",
                exploitation_status="exploitable",
                exploitation_steps="s",
                validation_status="Valid",
                validation_analysis="va",
                reconciliation=reconciliation,
                agentic_transcript=agentic_transcript,
            )
            session.add(finding)
            await session.flush()
            await session.commit()
            return str(run.id), str(finding.id)

    async def test_finding_detail_includes_populated_reconciliation(self) -> None:
        _, finding_id = await self._seed_finding(
            reconciliation=_FAKE_RECONCILIATION,
            agentic_transcript=_FAKE_AGENTIC_TRANSCRIPT,
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://t"
        ) as client:
            await self.login_admin(client)
            response = await client.get(f"/api/findings/{finding_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("reconciliation", body)
        self.assertEqual(body["reconciliation"], _FAKE_RECONCILIATION)
        # Per-unit verdicts list survives the JSONB round-trip.
        self.assertEqual(len(body["reconciliation"]["per_unit_verdicts"]), 2)
        # Agentic transcript is surfaced too.
        self.assertIn("agentic_transcript", body)
        self.assertEqual(body["agentic_transcript"], _FAKE_AGENTIC_TRANSCRIPT)

    async def test_finding_detail_returns_null_for_path_only(self) -> None:
        _, finding_id = await self._seed_finding(
            reconciliation=None, agentic_transcript=None
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://t"
        ) as client:
            await self.login_admin(client)
            response = await client.get(f"/api/findings/{finding_id}")
        body = response.json()
        # Both fields present but null.
        self.assertIn("reconciliation", body)
        self.assertIsNone(body["reconciliation"])
        self.assertIn("agentic_transcript", body)
        self.assertIsNone(body["agentic_transcript"])
