"""Tests for `AgentTransport` Protocol + transports.

Covers `wire-agentic-into-workflow` Phase 0 contract:

- `MockAgentTransport` records calls and returns scripted
  `AgentResult`s.
- `CoderServiceAgentTransport`'s startup probe fails fast
  with a clear error when the endpoint is unreachable.
- `AgenticReconciler` shortcuts single-unit groups (no agent
  call) and consolidates multi-unit groups via the configured
  transport.
- `build_reconciler` selects between `PassthroughReconciler`
  and `AgenticReconciler` based on `audit.stages.form`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agent_transport import (
    AgentResult,
    CoderServiceAgentTransport,
    MockAgentTransport,
)
from xauditor.audit.reconciler import (
    AgenticReconciler,
    PassthroughReconciler,
    build_reconciler,
)
from xauditor.config import AuditAgenticConfig, AuditModeConfig
from xauditor.models import (
    ConfidenceLevel,
    Finding,
    SourceReference,
    ValidationStatus,
)


def _finding(
    *,
    finding_id: str = "F-1",
    finding_name: str = "SQLi in handler",
    suspect_function_id: str = "fn::handle",
    suspect_line: int = 42,
    validation_status: ValidationStatus = ValidationStatus.VALID,
    validation_analysis: str = "valid analysis",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        finding_name=finding_name,
        finding_description="d",
        confidence_level=ConfidenceLevel.HIGH,
        source_references=(
            SourceReference(
                file_path="src/app.py",
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
        validation_status=validation_status,
        validation_analysis=validation_analysis,
        path_fingerprint="fp::path",
        function_names=("handler",),
        suspect_function_id=suspect_function_id,
        suspect_line=suspect_line,
    )


class MockAgentTransportTests(unittest.TestCase):
    def test_records_calls_and_returns_scripted_results(self) -> None:
        scripted = [
            AgentResult(final_answer={"a": 1}),
            AgentResult(final_answer={"b": 2}),
        ]
        transport = MockAgentTransport(scripted=scripted)
        r1 = transport.invoke(
            system_prompt="sys1",
            user_payload={"u": 1},
            response_model=dict,
            timeout_seconds=60,
        )
        r2 = transport.invoke(
            system_prompt="sys2",
            user_payload={"u": 2},
            response_model=dict,
            timeout_seconds=60,
            project="alpha",
        )
        self.assertEqual(r1.final_answer, {"a": 1})
        self.assertEqual(r2.final_answer, {"b": 2})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0]["system_prompt"], "sys1")
        self.assertEqual(transport.calls[1]["project"], "alpha")

    def test_exhausted_script_returns_fallback(self) -> None:
        transport = MockAgentTransport(scripted=[])
        result = transport.invoke(
            system_prompt="sys",
            user_payload={},
            response_model=dict,
            timeout_seconds=60,
        )
        self.assertTrue(result.fell_back)
        self.assertIn("scripted list exhausted", result.fallback_reason)


class CoderServiceAgentTransportProbeTests(unittest.TestCase):
    def test_unreachable_endpoint_raises_runtime_error(self) -> None:
        # Pick a port that's almost certainly not bound. The probe
        # MUST fail fast with a clear message naming the endpoint and
        # the config field.
        transport = CoderServiceAgentTransport(
            endpoint="http://127.0.0.1:1",
        )
        with self.assertRaises(RuntimeError) as ctx:
            transport.invoke(
                system_prompt="sys",
                user_payload={},
                response_model=dict,
                timeout_seconds=60,
            )
        msg = str(ctx.exception)
        self.assertIn("http://127.0.0.1:1", msg)
        self.assertIn(
            "audit.agentic.transport.coder_service.endpoint", msg
        )


class AgenticReconcilerTests(unittest.TestCase):
    def test_single_unit_group_shortcuts_no_agent_call(self) -> None:
        transport = MockAgentTransport(scripted=[])
        reconciler = AgenticReconciler(transport=transport)
        result = reconciler.reconcile([_finding()])
        # Zero transport invocations — passthrough shortcut.
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].consolidated_verdict, "Valid")
        # No reasoning prose in the shortcut.
        self.assertEqual(result[0].consolidation_reasoning, "")

    def test_multi_unit_group_invokes_transport(self) -> None:
        transport = MockAgentTransport(
            scripted=[
                AgentResult(
                    final_answer={
                        "verdict": "Partial Valid",
                        "reasoning": (
                            "Path + Sink units agree on SQLi but Entry "
                            "unit refutes — admin-only with hard auth."
                        ),
                    }
                )
            ]
        )
        reconciler = AgenticReconciler(transport=transport)
        # Two findings sharing the same fingerprint represent the
        # same underlying issue from two unit kinds.
        f1 = _finding(finding_id="F-PATH")
        f2 = _finding(finding_id="F-SINK", validation_status=ValidationStatus.PARTIAL_VALID)
        result = reconciler.reconcile([f1, f2])
        # One agent call for the multi-unit group.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(result), 1)
        # Real consolidation lands on the result.
        self.assertEqual(result[0].consolidated_verdict, "Partial Valid")
        self.assertIn("admin-only", result[0].consolidation_reasoning)
        # Per-unit verdicts preserved.
        self.assertEqual(len(result[0].per_unit_verdicts), 2)

    def test_reconciler_payload_includes_per_unit_verdicts(self) -> None:
        transport = MockAgentTransport(
            scripted=[AgentResult(final_answer={"verdict": "Valid", "reasoning": "ok"})]
        )
        reconciler = AgenticReconciler(transport=transport)
        f1 = _finding(finding_id="F-1")
        f2 = _finding(finding_id="F-2", validation_status=ValidationStatus.INCONCLUSIVE)
        reconciler.reconcile([f1, f2])
        payload = transport.calls[0]["user_payload"]
        self.assertIn("per_unit_verdicts", payload)
        self.assertEqual(len(payload["per_unit_verdicts"]), 2)
        self.assertEqual(payload["per_unit_verdicts"][0]["verdict"], "Valid")
        self.assertEqual(payload["per_unit_verdicts"][1]["verdict"], "Inconclusive")


class BuildReconcilerTests(unittest.TestCase):
    def test_prompt_form_returns_passthrough(self) -> None:
        cfg = AuditModeConfig()  # stages_form defaults to "prompt"
        reconciler = build_reconciler(audit_mode=cfg)
        self.assertIsInstance(reconciler, PassthroughReconciler)

    def test_agentic_form_returns_agentic_reconciler(self) -> None:
        cfg = AuditModeConfig(stages_form="agentic")
        transport = MockAgentTransport()
        reconciler = build_reconciler(audit_mode=cfg, transport=transport)
        self.assertIsInstance(reconciler, AgenticReconciler)

    def test_agentic_form_requires_transport(self) -> None:
        cfg = AuditModeConfig(stages_form="agentic")
        with self.assertRaises(ValueError) as ctx:
            build_reconciler(audit_mode=cfg)
        self.assertIn("transport", str(ctx.exception))


class AuditAgenticConfigDefaultsTests(unittest.TestCase):
    def test_defaults_match_design_doc(self) -> None:
        cfg = AuditAgenticConfig()
        self.assertEqual(cfg.timeout_seconds, 300)
        self.assertEqual(cfg.transport.kind, "coder_service")
        self.assertEqual(
            cfg.transport.coder_service.endpoint, "http://127.0.0.1:8090"
        )
        self.assertEqual(cfg.transport.coder_service.bearer_token_env, "")
        self.assertEqual(
            cfg.transport.coder_service.request_timeout_safety_seconds, 60
        )
        self.assertEqual(cfg.transport.coder_service.project, "")


if __name__ == "__main__":
    unittest.main()
