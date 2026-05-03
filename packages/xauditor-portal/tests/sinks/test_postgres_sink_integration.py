"""PostgresReportSink integration tests against a live PostgreSQL.

Covers tasks 1.9 (per-entity writes + idempotency) and 1.10 (end-to-end
snapshot: coverage / debates / subagents / no-findings / raw outputs all
populate after emit_snapshot).

Gated by ``XAUDITOR_TEST_DATABASE_URL`` — see ``tests/_live_db.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from tests._live_db import LiveDatabaseTestCase
from xauditor_portal.db.models.report import (
    AnalyzerSubagentRecord,
    AuditRun,
    CoverageFile,
    CoverageFunction,
    CoverageModule,
    ExploiterSubagentRecord,
    Finding,
    FindingSourceReference,
    NoFindingPath,
    PathRawOutput,
    ReferencedSymbol,
    ValidatorDebate,
    ValidatorSubagentRecord,
)
from xauditor_portal.sinks.postgres_sink import PostgresReportSink


# ----- in-memory snapshot fixtures ----------------------------------------


@dataclass(frozen=True)
class FakeSourceRef:
    file_path: str
    snippet: str
    language: str | None
    ordinal: int = 0


@dataclass(frozen=True)
class FakeFinding:
    finding_id: str
    path_fingerprint: str = "path-1"
    finding_name: str = "Buffer overflow"
    finding_description: str = "desc"
    confidence_level: str = "High"
    analyzer_status: str | None = None
    evidence_strength: str | None = None
    analysis: str = ""
    reason: str = ""
    context: str = ""
    business_context: str = ""
    context_notes: str | None = None
    suspect_function_id: str | None = "mod::fn"
    suspect_line: int | None = 10
    function_names: tuple[str, ...] = ("fn",)
    exploitation_status: str = "exploitable"
    exploitation_steps: str = "step 1"
    validation_status: str = "Valid"
    validation_analysis: str = ""
    source_references: tuple[FakeSourceRef, ...] = field(default_factory=tuple)
    referenced_symbols: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FakeCoverageRecord:
    category: str
    identifier: str
    state: str


@dataclass(frozen=True)
class FakeChain:
    path_fingerprint: str
    entry_function: str = "entry"
    function_chain: tuple[str, ...] = ("entry", "fn")


class FakeCoverageInventory:
    def __init__(self) -> None:
        self._records: dict[str, list[FakeCoverageRecord]] = {}
        self.audited_call_chains: list[FakeChain] = []

    def add(self, record: FakeCoverageRecord) -> None:
        self._records.setdefault(record.category, []).append(record)

    def items(self, category: str) -> list[FakeCoverageRecord]:
        return list(self._records.get(category, []))


@dataclass
class FakeAuditRun:
    build_fingerprint: str
    findings: tuple[FakeFinding, ...] = ()
    coverage: FakeCoverageInventory = field(default_factory=FakeCoverageInventory)
    shared_state: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeRunMeta:
    repo_root: str
    project_name: str
    build_fingerprint: str
    mode: str
    run_label: str
    started_at: datetime
    llm_providers_used: dict[str, Any] = field(default_factory=dict)


def _build_snapshot(
    *, mode: str = "team", report_fingerprint: str = "build-abc"
) -> tuple[FakeAuditRun, FakeRunMeta]:
    snapshot = FakeAuditRun(build_fingerprint=report_fingerprint)
    snapshot.findings = (
        FakeFinding(
            finding_id="F-001",
            analysis="analysis-body",
            source_references=(
                FakeSourceRef(
                    file_path="src/app.py",
                    snippet="print('a')",
                    language="python",
                    ordinal=0,
                ),
                FakeSourceRef(
                    file_path="src/app.py",
                    snippet="print('b')",
                    language="python",
                    ordinal=1,
                ),
                FakeSourceRef(
                    file_path="src/other.py",
                    snippet="y=1",
                    language="python",
                    ordinal=2,
                ),
            ),
            referenced_symbols=(
                {
                    "name": "token",
                    "kind": "variable",
                    "file_path": "src/app.py",
                    "start_line": 10,
                    "end_line": 10,
                    "used_by": [{"function_id": "fn", "line_number": 11}],
                },
            ),
        ),
        FakeFinding(
            finding_id="F-002",
            validation_status="False Positive",
            exploitation_status="not_exploitable",
        ),
    )
    for record in (
        FakeCoverageRecord("module", "mod.a", "audited"),
        FakeCoverageRecord("module", "mod.b", "not_audited"),
        FakeCoverageRecord("file", "src/app.py", "audited"),
        FakeCoverageRecord("function", "mod::fn", "audited"),
        FakeCoverageRecord("function", "mod::other", "not_audited"),
    ):
        snapshot.coverage.add(record)
    snapshot.coverage.audited_call_chains.append(FakeChain(path_fingerprint="path-1"))
    snapshot.shared_state["path-1"] = {
        "analyzer": {"status": "finding", "reason": "bad"},
        "validator": {"status": "ran"},
        "exploitation": {"status": "ran", "steps": "..."},
        "analyzer_subagents": [
            {"subagent_index": 0, "provider_name": "prov-a", "raw": "a"},
            {"subagent_index": 1, "provider_name": "prov-b", "raw": "b"},
        ],
        # Validator/exploiter subagents and validator_debates are keyed by the
        # same "<path_fingerprint>::f<finding_index>" string that
        # src/xauditor/audit/workflow.py:530 emits in real runs. The sink
        # translates these keys to the owning Finding.finding_id before
        # persisting so portal queries can join by finding_id.
        "validator_subagents": {
            "path-1::f0": [
                {"subagent_index": 0, "provider_name": "prov-a", "raw": "v0"},
            ],
            "path-1::f1": [
                {"subagent_index": 0, "provider_name": "prov-a", "raw": "v1"},
            ],
        },
        "exploiter_subagents": {
            "path-1::f0": [
                {"subagent_index": 0, "provider_name": "prov-b", "raw": "e0"},
            ],
            "path-1::f1": [
                {"subagent_index": 0, "provider_name": "prov-b", "raw": "e1"},
            ],
        },
        "validator_debates": {
            "path-1::f0": {
                "path_fingerprint": "path-1",
                "max_rounds": 3,
                "converged": True,
                "final_verdict": "Valid",
                "initial_verdicts": {0: "Valid", 1: "Inconclusive"},
                "turns": [
                    {
                        "round_index": 0,
                        "subagent_index": 0,
                        "provider_name": "prov-a",
                        "verdict": "Valid",
                        "rebuttal": "agree",
                        "raw_response": "{}",
                    },
                    {
                        "round_index": 1,
                        "subagent_index": 1,
                        "provider_name": "prov-b",
                        "verdict": "Valid",
                        "rebuttal": "ok",
                        "raw_response": "{}",
                    },
                ],
            },
            "path-1::f1": {
                "path_fingerprint": "path-1",
                "max_rounds": 2,
                "converged": False,
                "final_verdict": "False Positive",
                "initial_verdicts": {0: "False Positive", 1: "False Positive"},
                "turns": [
                    {
                        "round_index": 0,
                        "subagent_index": 0,
                        "provider_name": "prov-a",
                        "verdict": "False Positive",
                        "rebuttal": "no issue",
                        "raw_response": "{}",
                    },
                ],
            },
        },
    }
    snapshot.shared_state["path-2"] = {
        "analyzer": {"status": "clean", "reason": "nothing to flag"},
        "validator": {"status": "skipped"},
        "exploitation": {"status": "skipped"},
    }
    meta = FakeRunMeta(
        repo_root="/tmp/repo",
        project_name="repo",
        build_fingerprint=report_fingerprint,
        mode=mode,
        run_label=report_fingerprint,
        started_at=datetime.now(timezone.utc),
        llm_providers_used={"auditor": "prov-a", "validator": "prov-a"},
    )
    return snapshot, meta


def _new_sink_bound_to(sync_url: str) -> PostgresReportSink:
    """Return a PostgresReportSink whose engine points at the test DB.

    Bypasses ``ReportDBConfig``-driven URL resolution so the sink doesn't
    try to connect to the developer's production reportdb.
    """

    sink = PostgresReportSink.__new__(PostgresReportSink)
    from sqlalchemy import create_engine

    sink.config = None  # type: ignore[assignment]
    sink._engine = create_engine(sync_url, pool_pre_ping=True)
    sink._session_factory = sessionmaker(
        bind=sink._engine, expire_on_commit=False
    )
    sink._run_id = None
    return sink


# ----- tests --------------------------------------------------------------


class PostgresReportSinkIntegrationTests(LiveDatabaseTestCase):
    async def _count(self, model: Any) -> int:
        async with self.session_factory() as session:
            total = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        return int(total)

    async def test_emit_snapshot_writes_every_mirrored_table(self) -> None:
        sink = _new_sink_bound_to(self.sync_url)
        snapshot, meta = _build_snapshot()
        sink.open_run(meta)
        sink.emit_snapshot(snapshot)
        sink.close_run(snapshot)

        self.assertEqual(await self._count(AuditRun), 1)
        self.assertEqual(await self._count(Finding), 2)
        self.assertEqual(await self._count(FindingSourceReference), 3)
        self.assertEqual(await self._count(ReferencedSymbol), 1)
        self.assertEqual(await self._count(CoverageModule), 2)
        self.assertEqual(await self._count(CoverageFile), 1)
        self.assertEqual(await self._count(CoverageFunction), 2)
        self.assertEqual(await self._count(ValidatorDebate), 2)
        self.assertEqual(await self._count(NoFindingPath), 1)
        self.assertEqual(await self._count(AnalyzerSubagentRecord), 2)
        self.assertEqual(await self._count(ValidatorSubagentRecord), 2)
        self.assertEqual(await self._count(ExploiterSubagentRecord), 2)
        # path_raw_outputs: (analyzer + validator + exploitation) × 2 paths = 6.
        self.assertEqual(await self._count(PathRawOutput), 6)

        # Finalization stamps completion on the run row.
        async with self.session_factory() as session:
            run_row = (await session.execute(select(AuditRun))).scalar_one()
            self.assertEqual(run_row.status, "completed")
            self.assertEqual(run_row.progress_percent, 100)
            self.assertIsNotNone(run_row.completed_at)

        # Debate / subagent finding_ref values SHALL join against
        # findings.finding_id — not the internal "<path>::f<idx>" key.
        # This is the regression that blocks the portal's per-finding
        # debate section from rendering.
        async with self.session_factory() as session:
            finding_ids = {
                row.finding_id
                for row in (await session.execute(select(Finding))).scalars()
            }
            self.assertEqual(finding_ids, {"F-001", "F-002"})
            debate_refs = {
                row.finding_ref
                for row in (await session.execute(select(ValidatorDebate))).scalars()
            }
            self.assertEqual(debate_refs, {"F-001", "F-002"})
            for row in (
                await session.execute(select(ValidatorSubagentRecord))
            ).scalars():
                self.assertIn(row.finding_ref, finding_ids)
            for row in (
                await session.execute(select(ExploiterSubagentRecord))
            ).scalars():
                self.assertIn(row.finding_ref, finding_ids)
            # findings.path_fingerprint mirrors the owning validator_debates.path_fingerprint.
            debate_rows = (
                await session.execute(select(ValidatorDebate))
            ).scalars().all()
            finding_by_id = {
                row.finding_id: row
                for row in (await session.execute(select(Finding))).scalars()
            }
            for debate in debate_rows:
                finding_row = finding_by_id[debate.finding_ref]
                self.assertEqual(finding_row.path_fingerprint, debate.path_fingerprint)

    async def test_second_snapshot_is_idempotent(self) -> None:
        sink = _new_sink_bound_to(self.sync_url)
        snapshot, meta = _build_snapshot()
        sink.open_run(meta)
        sink.emit_snapshot(snapshot)
        sink.emit_snapshot(snapshot)  # exact re-emit
        # Row counts do not grow: the sink replaces child rows atomically
        # and uses the finding_id natural key for the parent Finding row.
        self.assertEqual(await self._count(Finding), 2)
        self.assertEqual(await self._count(FindingSourceReference), 3)
        self.assertEqual(await self._count(CoverageModule), 2)
        self.assertEqual(await self._count(ValidatorDebate), 2)
        self.assertEqual(await self._count(AnalyzerSubagentRecord), 2)

    async def test_report_dir_is_natural_key_not_fingerprint(self) -> None:
        """Two runs against the same build fingerprint must get their own rows.

        Regression test for the sink bug fixed by this change — cancelling
        a run and starting a new one against the same graph build
        previously collapsed onto a single AuditRun row.
        """

        sink_a = _new_sink_bound_to(self.sync_url)
        snap_a, meta_a = _build_snapshot(report_fingerprint="build-same")
        sink_a.open_run(meta_a)
        sink_a.emit_snapshot(snap_a)
        sink_a.close_run(snap_a)

        sink_b = _new_sink_bound_to(self.sync_url)
        snap_b, _ = _build_snapshot(report_fingerprint="build-same")
        meta_b = FakeRunMeta(
            repo_root="/tmp/repo",
            project_name="repo",
            build_fingerprint="build-same",
            mode="team",
            run_label="second-run",
            started_at=datetime.now(timezone.utc),
            llm_providers_used={},
        )
        sink_b.open_run(meta_b)
        sink_b.emit_snapshot(snap_b)
        sink_b.close_run(snap_b)

        self.assertEqual(await self._count(AuditRun), 2)

    async def test_orphaned_debate_key_logs_warning_and_falls_back(self) -> None:
        """A validator_debates key that points at no real finding must not crash.

        The sink SHALL log a warning and persist the row with the raw
        ``<path_fingerprint>::f<idx>`` key so the audit does not lose the
        transcript; the portal's finding-joined query will simply not see
        it (the data-layer regression is visible in the logs).
        """

        sink = _new_sink_bound_to(self.sync_url)
        snapshot, meta = _build_snapshot()
        snapshot.shared_state["path-1"]["validator_debates"]["deadbeef::f9"] = {
            "path_fingerprint": "deadbeef",
            "max_rounds": 1,
            "converged": False,
            "final_verdict": "Inconclusive",
            "initial_verdicts": {},
            "turns": [],
        }
        sink.open_run(meta)
        with self.assertLogs("xauditor_portal.sinks.postgres", level="WARNING") as cm:
            sink.emit_snapshot(snapshot)
            sink.close_run(snapshot)
        self.assertTrue(
            any("deadbeef::f9" in message for message in cm.output),
            f"expected a warning about the orphan debate key, got: {cm.output}",
        )
        # Two resolvable + one orphan = three debate rows persisted.
        self.assertEqual(await self._count(ValidatorDebate), 3)
        async with self.session_factory() as session:
            refs = {
                row.finding_ref
                for row in (await session.execute(select(ValidatorDebate))).scalars()
            }
            self.assertIn("deadbeef::f9", refs)
            self.assertEqual(refs, {"F-001", "F-002", "deadbeef::f9"})

    async def test_upsert_finding_writes_one_row_without_full_snapshot(self) -> None:
        """``upsert_finding`` lands a single Finding row when no prior snapshot exists.

        The Phase 1 streaming path can fire before any path-boundary
        ``emit_snapshot`` has run. The sink SHALL still create the
        Finding row and SHALL NOT touch any other mirrored table.
        """

        from dataclasses import replace as dataclass_replace

        sink = _new_sink_bound_to(self.sync_url)
        _, meta = _build_snapshot()
        sink.open_run(meta)
        finding = FakeFinding(finding_id="F-LIVE-1", analysis="hot")
        sink.upsert_finding("RUN-LIVE-1", finding)

        self.assertEqual(await self._count(Finding), 1)
        # Sibling tables are NOT populated by upsert_finding alone — they
        # are seeded by emit_snapshot at path boundaries.
        self.assertEqual(await self._count(CoverageModule), 0)
        self.assertEqual(await self._count(ValidatorDebate), 0)
        self.assertEqual(await self._count(AnalyzerSubagentRecord), 0)
        # An update to the same finding_id mutates the row in place.
        sink.upsert_finding("RUN-LIVE-1", dataclass_replace(finding, analysis="hotter"))
        self.assertEqual(await self._count(Finding), 1)
        async with self.session_factory() as session:
            row = (await session.execute(select(Finding))).scalar_one()
            self.assertEqual(row.analysis, "hotter")

    async def test_upsert_finding_does_not_touch_sibling_findings(self) -> None:
        """Per-row UPSERT MUST leave other findings on the same run untouched.

        Regression guard for the O(N²) write storm we are eliminating —
        the prior streaming path called ``emit_snapshot`` which rewrote
        every Finding's row + children for every coder settle. The new
        path is a single-row UPSERT.
        """

        from dataclasses import replace as dataclass_replace

        sink = _new_sink_bound_to(self.sync_url)
        snapshot, meta = _build_snapshot()
        sink.open_run(meta)
        sink.emit_snapshot(snapshot)

        # Capture the second finding's pre-upsert state.
        async with self.session_factory() as session:
            f002_pre = (
                await session.execute(
                    select(Finding).where(Finding.finding_id == "F-002")
                )
            ).scalar_one()
            f002_pre_analysis = f002_pre.analysis
            f002_pre_source_refs = (
                await session.execute(
                    select(func.count())
                    .select_from(FindingSourceReference)
                    .where(FindingSourceReference.finding_id == f002_pre.id)
                )
            ).scalar_one()

        # Upsert ONLY F-001 with a mutated analysis field.
        f001_mutated = dataclass_replace(snapshot.findings[0], analysis="streamed-update")
        sink.upsert_finding("RUN-LIVE-2", f001_mutated)

        # F-002 row is identical: same analysis, same source_reference count.
        async with self.session_factory() as session:
            f002_post = (
                await session.execute(
                    select(Finding).where(Finding.finding_id == "F-002")
                )
            ).scalar_one()
            self.assertEqual(f002_post.analysis, f002_pre_analysis)
            f002_post_source_refs = (
                await session.execute(
                    select(func.count())
                    .select_from(FindingSourceReference)
                    .where(FindingSourceReference.finding_id == f002_post.id)
                )
            ).scalar_one()
            self.assertEqual(f002_post_source_refs, f002_pre_source_refs)
            # F-001 reflects the streamed update.
            f001_post = (
                await session.execute(
                    select(Finding).where(Finding.finding_id == "F-001")
                )
            ).scalar_one()
            self.assertEqual(f001_post.analysis, "streamed-update")

    async def test_fail_run_marks_terminal_status(self) -> None:
        sink = _new_sink_bound_to(self.sync_url)
        snapshot, meta = _build_snapshot()
        sink.open_run(meta)
        sink.emit_snapshot(snapshot)
        sink.fail_run(status="cancelled", error="user cancelled")

        async with self.session_factory() as session:
            run_row = (await session.execute(select(AuditRun))).scalar_one()
            self.assertEqual(run_row.status, "cancelled")
            self.assertIsNotNone(run_row.completed_at)


if __name__ == "__main__":
    import unittest

    unittest.main()
