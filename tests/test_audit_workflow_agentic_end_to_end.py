"""End-to-end integration test for `audit.stages.form: agentic`.

Constructs an `AuditWorkflow` with an `AgenticStageRunner` backed by
a `MockAgentTransport`, runs a fixture audit through
`_process_unit_single`, and asserts:

- Three transport invocations per unit (one each for analyzer /
  validator / exploiter).
- Each emitted finding's `agentic_transcript` carries one entry per
  stage call with the expected `stage` keys.
- `Finding.reconciliation` is populated with the structurally
  complete `PassthroughReconciler` payload (one `per_unit_verdict`
  entry mirroring the validation status).
- A run with no findings produces a no_finding placeholder with
  empty `agentic_transcript` (drained-but-empty since there ARE
  transcripts for the no-issue analyzer call).

The `MockAgentTransport` short-circuits coder-service entirely, so
this test runs in CI without `claude` on PATH and without a live
xauditor-coder-service install. `wire-agentic-into-workflow` Phase 3
end-to-end smoke against a real coder-service deployment is the V1
runtime validation task in the change's `tasks.md`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agent_transport import (
    AgentResult,
    MockAgentTransport,
)
from xauditor.audit.agents import (
    AnalyzerResult,
    ExploitationResult,
    ValidationResult,
)
from xauditor.audit.reconciler import PassthroughReconciler
from xauditor.audit.stage_runner import AgenticStageRunner
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import (
    AuditAgenticConfig,
    AuditAnalyzerStageConfig,
    AuditConfig,
    AuditModeConfig,
    AuditReplicationConfig,
    CoderConfig,
    GraphConfig,
    LLMConfig,
    LLMSettings,
    LoggingConfig,
    Neo4jConfig,
    PortalConfig,
    ReportDBConfig,
    RepositoryConfig,
    RuntimeConfig,
    TeamingConfig,
    XAuditorConfig,
)
from xauditor.llm import LLMClient
from xauditor.models import (
    AuditUnit,
    PathRecord,
    ValidationStatus,
)


def _make_unit(*, fingerprint: str = "fp::handler") -> AuditUnit:
    return AuditUnit(
        path=PathRecord(
            entry_function="handle_request",
            function_names=("handle_request",),
            file_paths=("src/app.py",),
            path_fingerprint=fingerprint,
            function_ids=("fn::handle",),
            business_context="Web request handler.",
        ),
        function_ids=("fn::handle",),
    )


def _minimal_config() -> XAuditorConfig:
    return XAuditorConfig(
        repo_root=Path("/tmp"),
        graphdb=Neo4jConfig(),
        repository=RepositoryConfig(),
        runtime=RuntimeConfig(root_dir=Path("/tmp/.xauditor-agentic-e2e")),
        logging=LoggingConfig(level="info"),
        llm=LLMSettings(
            default_provider="p1",
            providers={
                "p1": LLMConfig(
                    base_url="mock://offline",
                    api_key="k",
                    model_name="m",
                )
            },
        ),
        graph=GraphConfig(),
        audit_mode=AuditModeConfig(
            mode="fast",
            replication=AuditReplicationConfig(),
            max_findings_per_unit=3,
            stages_form="agentic",
            agentic=AuditAgenticConfig(timeout_seconds=60),
        ),
        teaming=TeamingConfig(),
        coder=CoderConfig(),
        reportdb=ReportDBConfig(),
        portal=PortalConfig(),
        audit=AuditConfig(),
    )


def _make_workflow(
    *, transport: MockAgentTransport
) -> AuditWorkflow:
    """Build a workflow with an AgenticStageRunner backed by the
    supplied mock transport. Bypasses `from_config` so we don't
    trigger the CoderServiceAgentTransport `/health` probe.
    """

    runner = AgenticStageRunner(transport=transport, timeout_seconds=60)
    reconciler = PassthroughReconciler()
    workflow = AuditWorkflow(
        config=_minimal_config(),
        llm_client=LLMClient.__new__(LLMClient),  # never invoked under agentic mode
        stage_runner=runner,
        reconciler=reconciler,
        agent_transport=transport,
    )
    return workflow


class SingleModeAgenticEndToEndTests(unittest.TestCase):
    def test_three_transport_calls_per_unit_one_per_stage(self) -> None:
        scripted = [
            # Round 1 analyzer: candidate finding.
            AgentResult(
                final_answer={
                    "status": "candidate",
                    "finding_name": "SQLi via raw cursor.execute",
                    "description": "Reachable injection sink.",
                    "reason": "User input flows to raw SQL.",
                    "suspect_function_id": "fn::handle",
                    "suspect_line": 42,
                    "evidence_strength": "high",
                },
                transcript=[
                    {"tool": "Read", "input": {"path": "app.py"}, "output": "..."},
                    {"tool": "final_answer", "input": {}, "output": None},
                ],
                tool_call_count=2,
            ),
            # Validator: confirms.
            AgentResult(
                final_answer={"status": "Valid", "analysis": "Confirmed reachable."},
                transcript=[
                    {"tool": "Read", "input": {"path": "app.py"}, "output": "..."},
                    {"tool": "final_answer", "input": {}, "output": None},
                ],
                tool_call_count=2,
            ),
            # Exploiter: provides exploit.
            AgentResult(
                final_answer={
                    "status": "exploitable",
                    "steps": "Send `?q=' OR 1=1--`",
                },
                transcript=[
                    {"tool": "final_answer", "input": {}, "output": None},
                ],
                tool_call_count=1,
            ),
            # Round 2 analyzer: no_issue → loop terminates after 2nd no_issue.
            AgentResult(final_answer={"status": "no_issue"}, transcript=[]),
            AgentResult(final_answer={"status": "no_issue"}, transcript=[]),
        ]
        transport = MockAgentTransport(scripted=scripted)
        workflow = _make_workflow(transport=transport)
        unit = _make_unit()

        per_finding, shared_state, _ = workflow._process_unit_single(
            unit=unit,
            path_functions=[],
            path_context={},
        )

        # Stage calls observed: 1st analyzer + validator + exploiter
        # + 2 more analyzers (no_issue terminating the loop).
        # Total 5 transport.invoke(...) calls, 3 of which are stage
        # calls for the FIRST candidate.
        self.assertEqual(len(transport.calls), 5)
        # First three calls are analyzer / validator / exploiter (chain).
        self.assertEqual(transport.calls[0]["response_model"].__name__, "AnalyzerOutput")
        self.assertEqual(transport.calls[1]["response_model"].__name__, "ValidationOutput")
        self.assertEqual(transport.calls[2]["response_model"].__name__, "ExploitationOutput")

        # One accepted candidate emitted.
        accepted = [entry for entry in per_finding if entry[3]]
        self.assertEqual(len(accepted), 1)

        # Per-unit transcript drained onto shared_state.
        transcript = shared_state.get("agentic_transcript", ())
        # Five transcript entries (one per stage call across all rounds).
        self.assertEqual(len(transcript), 5)
        stages_seen = {entry["stage"] for entry in transcript}
        self.assertEqual(stages_seen, {"analyzer", "validator", "exploiter"})

    def test_no_candidate_round_emits_no_finding_with_drained_transcripts(self) -> None:
        # Two consecutive no_issue → loop terminates.
        scripted = [
            AgentResult(
                final_answer={"status": "no_issue"},
                transcript=[{"tool": "Read", "input": {}, "output": ""}],
            ),
            AgentResult(
                final_answer={"status": "no_issue"},
                transcript=[],
            ),
        ]
        transport = MockAgentTransport(scripted=scripted)
        workflow = _make_workflow(transport=transport)
        unit = _make_unit(fingerprint="fp::clean")

        per_finding, shared_state, checkpoint = workflow._process_unit_single(
            unit=unit,
            path_functions=[],
            path_context={},
        )

        # Two analyzer-only invocations; no validator / exploiter.
        self.assertEqual(len(transport.calls), 2)
        # Single placeholder no_finding emitted.
        self.assertEqual(checkpoint, "no_finding")
        self.assertEqual(per_finding[0][3], False)
        # Drained transcripts land on shared_state regardless of
        # whether candidates were accepted.
        transcript = shared_state.get("agentic_transcript", ())
        self.assertEqual(len(transcript), 2)
        for entry in transcript:
            self.assertEqual(entry["stage"], "analyzer")

    def test_passthrough_reconciler_populates_finding_reconciliation(self) -> None:
        # Build a single Valid finding through `_apply_reconciliation`
        # to check that the workflow's run-end reconciler step stamps
        # the structurally complete payload.
        from xauditor.models import (
            ConfidenceLevel,
            Finding,
            SourceReference,
        )

        finding = Finding(
            finding_id="F-0001",
            finding_name="SQLi",
            finding_description="d",
            confidence_level=ConfidenceLevel.HIGH,
            source_references=(
                SourceReference(
                    file_path="app.py",
                    start_line=0,
                    end_line=0,
                    focus_lines=(),
                    language="python",
                    snippet="...",
                ),
            ),
            analysis="a",
            reason="r",
            context="c",
            business_context="b",
            exploitation_status="exploitable",
            exploitation_steps="s",
            validation_status=ValidationStatus.VALID,
            validation_analysis="confirmed",
            path_fingerprint="fp::1",
        )
        workflow = _make_workflow(transport=MockAgentTransport())
        findings = [finding]
        upserted: list[Finding] = []
        workflow._apply_reconciliation(findings, on_finding_upsert=upserted.append)

        # Finding now carries the structurally complete passthrough
        # payload (one per_unit_verdict entry mirroring the
        # validation status).
        updated = findings[0]
        self.assertIsNotNone(updated.reconciliation)
        payload = updated.reconciliation
        self.assertEqual(payload["consolidated_verdict"], "Valid")
        self.assertEqual(payload["consolidation_reasoning"], "")
        self.assertEqual(len(payload["per_unit_verdicts"]), 1)
        self.assertEqual(payload["per_unit_verdicts"][0]["verdict"], "Valid")

        # on_finding_upsert was re-emitted exactly once for the
        # finding that gained the reconciliation payload.
        self.assertEqual(len(upserted), 1)
        self.assertEqual(upserted[0].finding_id, "F-0001")


class DeepModeTeamAgenticDispatchTests(unittest.TestCase):
    """`wire-agentic-into-workflow` Phase 2 — AnalyzerTeam +
    ExploiterTeam dispatch through the agentic stage runner per
    replica when `agentic_stage_runner` is set on the team. Per-
    replica diversity comes from persona injection (different
    `extra_system_prefix` appended to the system prompt).
    """

    def test_analyzer_team_per_replica_dispatches_through_stage_runner(
        self,
    ) -> None:
        from xauditor.audit.agents import AnalyzerTeam
        from xauditor.audit.stage_runner import resolve_personas
        from xauditor.config import (
            AnalyzerTeamConfig,
            ExploiterTeamConfig,
            LLMSettings,
            TeamingConfig,
            ValidatorTeamConfig,
        )

        # Three replicas, each with a distinct persona.
        scripted = [
            AgentResult(final_answer={"status": "no_issue"}),
            AgentResult(final_answer={"status": "no_issue"}),
            AgentResult(final_answer={"status": "no_issue"}),
        ]
        transport = MockAgentTransport(scripted=scripted)
        runner = AgenticStageRunner(transport=transport, timeout_seconds=60)

        teaming = TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(
                provider_list=("p1", "p2", "p3"),
                subagent_count=3,
            ),
            validator=ValidatorTeamConfig(provider_list=("p1",), subagent_count=1),
            exploiter=ExploiterTeamConfig(provider_list=("p1",), subagent_count=1),
        )
        # `LLMSettings` only consulted when stage_runner is None
        # (i.e. for the per-provider model build path); we pass a
        # minimal-shape value to satisfy the dataclass.
        llm = LLMSettings(default_provider="p1", providers={})

        # Resolve three personas.
        personas = resolve_personas(personas=(), replication=3)
        team = AnalyzerTeam(
            stage_cfg=AuditAnalyzerStageConfig(
                provider_list=teaming.analyzer.provider_list,
            ),
            replication=teaming.analyzer.subagent_count,
            llm=llm,
            agentic_stage_runner=runner,
            personas=personas,
        )

        unit = _make_unit()
        consolidated, records = team.run(
            unit=unit, path_functions=[], path_context={}
        )

        # Three transport invocations — one per replica.
        self.assertEqual(len(transport.calls), 3)

        # Per-replica system prompts diverge (each persona's
        # extra_system_prefix appended).
        prompts = [call["system_prompt"] for call in transport.calls]
        self.assertEqual(len(set(prompts)), 3)
        # Each replica's prompt contains the matching persona prefix.
        for call, persona in zip(transport.calls, personas):
            if persona is not None:
                self.assertIn(persona.extra_system_prefix, call["system_prompt"])

        # Records carry the agentic provider sentinel.
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(record.provider_name, "coder-service")

        # No candidate findings → consolidated is empty (matches the
        # team's existing contract).
        self.assertEqual(consolidated, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
