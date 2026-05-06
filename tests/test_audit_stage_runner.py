"""Tests for the StageRunner Protocol seam (Phase 4A).

Covers `restructure-audit-modes-and-coverage` Phase 4 tasks
4.1.1-4.1.3 + 4.2.1-4.2.3 + 4.5.1 + 4.6.1 + 4.6.3 + 4.6.4:

- StageRunner Protocol with two implementations
  (`PromptStageRunner` real, `AgenticStageRunner` stub).
- `Persona` dataclass + `DEFAULT_DEEP_PERSONAS` constant.
- `resolve_personas(personas, replication)` resolver:
  fast mode (rep=1) returns `(None,)`, deep mode (rep>1)
  cycles through the persona list with repeat-from-start.
- `build_stage_runner` selects implementation based on
  `audit.stages.form`.
- `estimate_audit_cost` + `emit_cost_estimate` produce
  order-of-magnitude estimates and stderr output.
- AgenticStageRunner stub raises `NotImplementedError` with
  the documented message.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unittest.mock import MagicMock

from xauditor.audit.agents import AnalyzerAgent, ExploitationAgent, ValidatorAgent
from xauditor.audit.stage_runner import (
    DEFAULT_DEEP_PERSONAS,
    AgenticStageRunner,
    Persona,
    PromptStageRunner,
    StageRunner,
    build_stage_runner,
    emit_cost_estimate,
    estimate_audit_cost,
    resolve_personas,
)
from xauditor.config import (
    AUDIT_STAGES_FORM_VALUES,
    AuditModeConfig,
    AuditReplicationConfig,
    AuditValidatorStageConfig,
    ValidatorDebateConfig,
)


class PersonaResolverTests(unittest.TestCase):
    def test_replication_1_returns_single_none(self) -> None:
        # Fast mode: no diversity dimension to exploit.
        self.assertEqual(resolve_personas((), 1), (None,))
        self.assertEqual(resolve_personas(DEFAULT_DEEP_PERSONAS, 1), (None,))

    def test_default_personas_used_when_none_supplied(self) -> None:
        result = resolve_personas((), 3)
        self.assertEqual(
            [p.name for p in result],
            ["data_flow", "auth_boundaries", "config_assumptions"],
        )

    def test_repeats_from_start_when_replication_exceeds_pool(self) -> None:
        result = resolve_personas((), 5)
        self.assertEqual(
            [p.name for p in result],
            [
                "data_flow",
                "auth_boundaries",
                "config_assumptions",
                "data_flow",
                "auth_boundaries",
            ],
        )

    def test_custom_personas_used_verbatim(self) -> None:
        custom = (
            Persona(name="entry_focus", focus_summary="x", extra_system_prefix="x"),
            Persona(name="sink_focus", focus_summary="y", extra_system_prefix="y"),
        )
        result = resolve_personas(custom, 4)
        self.assertEqual(
            [p.name for p in result],
            ["entry_focus", "sink_focus", "entry_focus", "sink_focus"],
        )

    def test_default_pool_has_three_personas(self) -> None:
        # Documented count — Phase 4A's deep-mode default replication
        # is 3, so the default persona list matches one-to-one.
        self.assertEqual(len(DEFAULT_DEEP_PERSONAS), 3)

    def test_invalid_replication_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_personas((), 0)


class PromptStageRunnerProtocolConformanceTests(unittest.TestCase):
    def test_prompt_runner_satisfies_protocol(self) -> None:
        # Build a runner with mock agents — Protocol is structural,
        # so we just need the methods present.
        runner = PromptStageRunner(
            analyzer_agent=MagicMock(spec=AnalyzerAgent),
            validator_agent=MagicMock(spec=ValidatorAgent),
            exploitation_agent=MagicMock(spec=ExploitationAgent),
        )
        self.assertTrue(hasattr(runner, "run_analyzer"))
        self.assertTrue(hasattr(runner, "run_validator"))
        self.assertTrue(hasattr(runner, "run_exploiter"))


class AgenticStageRunnerProtocolConformanceTests(unittest.TestCase):
    """`agentic-stage-runner-real` replaced the Phase 4A stub with
    a real implementation backed by an `AgentTransport`."""

    def test_real_runner_satisfies_protocol(self) -> None:
        from xauditor.audit.agent_transport import MockAgentTransport

        runner = AgenticStageRunner(transport=MockAgentTransport())
        self.assertTrue(hasattr(runner, "run_analyzer"))
        self.assertTrue(hasattr(runner, "run_validator"))
        self.assertTrue(hasattr(runner, "run_exploiter"))

    def test_run_analyzer_invokes_transport_once(self) -> None:
        from xauditor.audit.agent_transport import (
            AgentResult,
            MockAgentTransport,
        )
        from xauditor.audit.units import as_audit_unit
        from xauditor.models import AuditUnit, PathRecord

        transport = MockAgentTransport(
            scripted=[
                AgentResult(
                    final_answer={
                        "status": "candidate",
                        "finding_name": "SQLi",
                        "suspect_function_id": "fn::handle",
                        "suspect_line": 10,
                        "evidence_strength": "high",
                    }
                )
            ]
        )
        runner = AgenticStageRunner(transport=transport)
        unit = as_audit_unit(
            AuditUnit(
                path=PathRecord(
                    entry_function="main",
                    function_names=("main",),
                    file_paths=("app.py",),
                    path_fingerprint="fp::1",
                    function_ids=("fn::1",),
                ),
                function_ids=("fn::1",),
            )
        )
        result = runner.run_analyzer(
            unit=unit, path_functions=[], path_context={}
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.finding_name, "SQLi")
        # Transcript was recorded for the workflow's _build_finding.
        self.assertEqual(len(runner.agentic_transcripts), 1)

    def test_persona_appended_to_system_prompt(self) -> None:
        from xauditor.audit.agent_transport import (
            AgentResult,
            MockAgentTransport,
        )
        from xauditor.audit.stage_runner import Persona
        from xauditor.audit.units import as_audit_unit
        from xauditor.models import AuditUnit, PathRecord

        transport = MockAgentTransport(
            scripted=[AgentResult(final_answer={"status": "no_issue"})]
        )
        runner = AgenticStageRunner(transport=transport)
        unit = as_audit_unit(
            AuditUnit(
                path=PathRecord(
                    entry_function="main",
                    function_names=("main",),
                    file_paths=("app.py",),
                    path_fingerprint="fp::2",
                    function_ids=("fn::1",),
                ),
                function_ids=("fn::1",),
            )
        )
        persona = Persona(
            name="auth_boundaries",
            focus_summary="x",
            extra_system_prefix="Focus on trust-boundary transitions.",
            tool_emphasis=("read_file", "grep"),
        )
        runner.run_analyzer(
            unit=unit, path_functions=[], path_context={}, persona=persona
        )
        system = transport.calls[0]["system_prompt"]
        self.assertIn("Focus on trust-boundary transitions.", system)
        # tool_grants no longer flows on the transport call —
        # the coder-service container is the security sandbox;
        # per-tool grants configured via claude's own settings.
        self.assertNotIn("tool_grants", transport.calls[0])

    def test_excluded_findings_land_in_payload(self) -> None:
        from xauditor.audit.agent_transport import (
            AgentResult,
            MockAgentTransport,
        )
        from xauditor.audit.units import as_audit_unit
        from xauditor.models import AuditUnit, PathRecord

        transport = MockAgentTransport(
            scripted=[AgentResult(final_answer={"status": "no_issue"})]
        )
        runner = AgenticStageRunner(transport=transport)
        unit = as_audit_unit(
            AuditUnit(
                path=PathRecord(
                    entry_function="main",
                    function_names=("main",),
                    file_paths=("app.py",),
                    path_fingerprint="fp::3",
                    function_ids=("fn::1",),
                ),
                function_ids=("fn::1",),
            )
        )
        excluded = (
            {
                "finding_name": "SQLi",
                "suspect_function_id": "fn::1",
                "suspect_line": 5,
            },
        )
        runner.run_analyzer(
            unit=unit,
            path_functions=[],
            path_context={},
            excluded_findings=excluded,
        )
        payload = transport.calls[0]["user_payload"]
        self.assertIn("excluded_findings", payload)
        self.assertEqual(len(payload["excluded_findings"]), 1)


class BuildStageRunnerTests(unittest.TestCase):
    def _agents(self):
        return {
            "analyzer_agent": MagicMock(spec=AnalyzerAgent),
            "validator_agent": MagicMock(spec=ValidatorAgent),
            "exploitation_agent": MagicMock(spec=ExploitationAgent),
        }

    def test_default_returns_prompt_runner(self) -> None:
        cfg = AuditModeConfig()  # stages_form defaults to "prompt"
        runner, transport = build_stage_runner(audit_mode=cfg, **self._agents())
        self.assertIsInstance(runner, PromptStageRunner)
        self.assertIsNone(transport)

    def test_agentic_form_returns_real_runner(self) -> None:
        from xauditor.audit.agent_transport import MockAgentTransport

        cfg = AuditModeConfig(stages_form="agentic")
        # Inject a mock transport to avoid CoderServiceAgentTransport's
        # /health probe dependency in CI.
        mock_transport = MockAgentTransport()
        runner, transport = build_stage_runner(
            audit_mode=cfg,
            transport=mock_transport,
            **self._agents(),
        )
        self.assertIsInstance(runner, AgenticStageRunner)
        self.assertIs(transport, mock_transport)

    def test_stages_form_values_export(self) -> None:
        self.assertEqual(set(AUDIT_STAGES_FORM_VALUES), {"prompt", "agentic"})


class CostEstimateTests(unittest.TestCase):
    def test_fast_estimate_scales_with_unit_count_and_cap(self) -> None:
        cfg = AuditModeConfig(mode="fast", max_findings_per_unit=3)
        e1 = estimate_audit_cost(audit_mode=cfg, unit_count=10)
        e2 = estimate_audit_cost(audit_mode=cfg, unit_count=100)
        # Linear in unit count.
        self.assertEqual(e2.estimated_calls, e1.estimated_calls * 10)
        self.assertEqual(
            e2.estimated_input_tokens_low,
            e1.estimated_input_tokens_low * 10,
        )

    def test_fast_estimate_scales_with_max_findings_per_unit(self) -> None:
        small = AuditModeConfig(mode="fast", max_findings_per_unit=1)
        large = AuditModeConfig(mode="fast", max_findings_per_unit=10)
        e_small = estimate_audit_cost(audit_mode=small, unit_count=1)
        e_large = estimate_audit_cost(audit_mode=large, unit_count=1)
        # 10× the cap → 10× the calls.
        self.assertEqual(e_large.estimated_calls, e_small.estimated_calls * 10)

    def test_deep_estimate_is_higher_than_fast(self) -> None:
        fast = AuditModeConfig(mode="fast")
        deep = AuditModeConfig(
            mode="deep",
            replication=AuditReplicationConfig(analyzer=3, validator=3, exploiter=1),
            validator=AuditValidatorStageConfig(
                debate=ValidatorDebateConfig(enabled=True, max_rounds=2),
            ),
        )
        fast_est = estimate_audit_cost(audit_mode=fast, unit_count=50)
        deep_est = estimate_audit_cost(audit_mode=deep, unit_count=50)
        # Deep mode is materially more expensive; at minimum more
        # calls and substantially more tokens.
        self.assertGreater(deep_est.estimated_calls, fast_est.estimated_calls)
        self.assertGreater(
            deep_est.estimated_input_tokens_low,
            fast_est.estimated_input_tokens_low * 5,
        )

    def test_render_includes_mode_and_call_count(self) -> None:
        cfg = AuditModeConfig(mode="fast")
        text = estimate_audit_cost(audit_mode=cfg, unit_count=42).render()
        self.assertIn("mode=fast", text)
        self.assertIn("42 units", text)
        self.assertIn("LLM calls", text)
        self.assertIn("tokens", text)


class EmitCostEstimateTests(unittest.TestCase):
    def test_emits_to_stderr(self) -> None:
        cfg = AuditModeConfig(mode="fast")
        est = estimate_audit_cost(audit_mode=cfg, unit_count=5)
        buf = io.StringIO()
        with redirect_stderr(buf):
            emit_cost_estimate(est)
        self.assertIn("[estimate]", buf.getvalue())
        self.assertIn("mode=fast", buf.getvalue())

    def test_emits_to_logger_when_present(self) -> None:
        cfg = AuditModeConfig(mode="deep")
        est = estimate_audit_cost(audit_mode=cfg, unit_count=5)
        logger = MagicMock()
        emit_cost_estimate(est, logger=logger)
        # Both stderr and the run logger get the same line for
        # later auditing.
        logger.info.assert_called_once()
        self.assertIn("mode=deep", logger.info.call_args.args[0])


class ConfigPersonaParsingTests(unittest.TestCase):
    """Yaml-supplied personas reach AuditModeConfig.personas."""

    def test_personas_parsed_from_yaml(self) -> None:
        import tempfile
        import textwrap
        import warnings
        from xauditor.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "xauditor.yml").write_text(
                textwrap.dedent("""
                    llm:
                      default_provider: p1
                      providers:
                        p1: {base_url: mock://p1, api_key: k, model_name: m}
                    audit:
                      personas:
                        - name: entry_focus
                          focus_summary: Authn boundaries
                          extra_system_prefix: Look at decorators first
                          tool_emphasis: [grep, read_file]
                        - name: sink_focus
                          focus_summary: Multi-path sanitization
                """).strip() + "\n",
                encoding="utf-8",
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                cfg = load_config(repo_root=repo, env={"HOME": tmp})
        self.assertEqual(len(cfg.audit_mode.personas), 2)
        self.assertEqual(cfg.audit_mode.personas[0].name, "entry_focus")
        self.assertEqual(
            cfg.audit_mode.personas[0].tool_emphasis, ("grep", "read_file")
        )
        self.assertEqual(cfg.audit_mode.personas[1].name, "sink_focus")
        self.assertEqual(cfg.audit_mode.personas[1].tool_emphasis, ())

    def test_invalid_stages_form_rejected(self) -> None:
        import tempfile
        import textwrap
        from xauditor.errors import ConfigError
        from xauditor.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "xauditor.yml").write_text(
                textwrap.dedent("""
                    llm:
                      default_provider: p1
                      providers:
                        p1: {base_url: mock://p1, api_key: k, model_name: m}
                    audit:
                      stages:
                        form: hybrid
                """).strip() + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo, env={"HOME": tmp})
        self.assertIn("audit.stages.form", str(ctx.exception))

    def test_personas_require_name(self) -> None:
        import tempfile
        import textwrap
        from xauditor.errors import ConfigError
        from xauditor.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "xauditor.yml").write_text(
                textwrap.dedent("""
                    llm:
                      default_provider: p1
                      providers:
                        p1: {base_url: mock://p1, api_key: k, model_name: m}
                    audit:
                      personas:
                        - focus_summary: nameless
                """).strip() + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo, env={"HOME": tmp})
        self.assertIn("audit.personas[0].name", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
