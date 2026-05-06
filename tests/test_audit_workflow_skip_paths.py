"""Tests for AuditWorkflow's resume skip-set behaviour (add-audit-resume §2).

Each path whose fingerprint is in ``skip_paths`` AND whose
``prior_shared_state[fp]`` entry is non-empty SHALL be carried forward
without any agent invocation. Paths in the skip set without prior state
SHALL fall through to a normal (fresh-execution) run.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xauditor.audit.planner import plan_audit_paths
from tests._helpers import _TestOnlyGraphSource
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import load_config
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.scope import resolve_repository_scope
from xauditor.llm import LLMClient
from xauditor.models import (
    AuditPlan,
    ValidationStatus,
)
from xauditor.audit.agents import (
    AnalyzerResult,
    ExploitationResult,
    ValidationResult,
)


SAMPLE_APP = """\
import subprocess

def handle_request(user_input):
    subprocess.run(user_input, shell=True)

def main():
    handle_request("ls")
"""


class _CountingAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, unit, path_functions, path_context=None, excluded_findings=()):
        del path_functions, path_context, excluded_findings
        self.calls += 1
        return AnalyzerResult(
            status="candidate",
            finding_name="Command Injection via shell=True",
            description=f"{unit.path.entry_function} reaches a dangerous sink.",
            reason="The sink is reachable from the current path.",
            suspect_function_id=unit.function_ids[0] if unit.function_ids else "",
            suspect_line=4,
            evidence_strength="high",
        )


class _CountingExploitation:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, unit, analyzer, path_context=None):
        del unit, analyzer, path_context
        self.calls += 1
        return ExploitationResult(status="ready", steps="Exploit details")


class _CountingValidator:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, unit, analyzer, exploitation=None, path_context=None):
        del unit, analyzer, exploitation, path_context
        self.calls += 1
        return ValidationResult(status=ValidationStatus.VALID, analysis="Validated")


class _RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.debug_messages: list[tuple[str, dict]] = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def debug_kv(self, message: str, **details) -> None:
        self.debug_messages.append((message, details))

    def error_kv(self, message: str, **details) -> None:
        self.debug_messages.append((message, details))


def _build_workflow_and_plan(tmp: str) -> tuple[AuditWorkflow, AuditPlan, _CountingAnalyzer, _CountingExploitation, _CountingValidator, _RecordingLogger, _TestOnlyGraphSource]:
    repo_root = Path(tmp)
    (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
    config = load_config(
        repo_root=repo_root,
        env={
            "XAUDITOR_LLM_BASE_URL": "mock://offline",
            "XAUDITOR_LLM_API_KEY": "secret",
            "XAUDITOR_LLM_MODEL_NAME": "mock-model",
        },
        require_llm=True,
    )
    from xauditor.integrations.neo4j import InMemoryNeo4jAdapter
    from xauditor.integrations.neo4j_repository import InMemoryNeo4jGraphRepository
    adapter = InMemoryNeo4jAdapter()
    repo = InMemoryNeo4jGraphRepository(adapter)
    fingerprint = LangChainGraphBuilder.from_config(config, repository=repo).build(
        repo_root=repo_root, scope=resolve_repository_scope(repo_root, ())
    )
    source = _TestOnlyGraphSource.from_inmemory_repo(repo, fingerprint)
    plan = plan_audit_paths(source)
    analyzer = _CountingAnalyzer()
    exploitation = _CountingExploitation()
    validator = _CountingValidator()
    logger = _RecordingLogger()
    workflow = AuditWorkflow(
        config=config,
        llm_client=LLMClient.from_config(config.llm),
        analyzer_agent=analyzer,
        exploitation_agent=exploitation,
        validator_agent=validator,
        logger=logger,
    )
    return workflow, plan, analyzer, exploitation, validator, logger, source


class SkipPathsTests(unittest.TestCase):
    def test_fresh_run_invokes_every_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, analyzer, exploitation, validator, *_ = _build_workflow_and_plan(tmp)
            self.assertGreater(len(plan.audit_units), 0)
            workflow.run(source=_build_workflow_and_plan(tmp)[6], plan=plan)
        # At least one path completed → every agent invoked at least once.
        self.assertGreaterEqual(analyzer.calls, 1)
        self.assertGreaterEqual(exploitation.calls, 1)
        self.assertGreaterEqual(validator.calls, 1)

    def test_path_in_skip_set_with_prior_state_invokes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, analyzer, exploitation, validator, _, source = _build_workflow_and_plan(tmp)
            first_fp = plan.audit_units[0].path.path_fingerprint
            # Synthesize prior state that looks like a completed path.
            prior = {
                first_fp: {
                    "analyzer": {"status": "candidate"},
                    "exploitation": {"status": "ready", "steps": "prior-exploit"},
                    "validator": {"status": "Valid", "analysis": "prior-valid"},
                    "model_settings": {},
                }
            }
            audit_run = workflow.run(
                source=source,
                plan=AuditPlan(audit_units=(plan.audit_units[0],)),
                skip_paths=frozenset({first_fp}),
                prior_shared_state=prior,
            )
        self.assertEqual(analyzer.calls, 0)
        self.assertEqual(exploitation.calls, 0)
        self.assertEqual(validator.calls, 0)
        # Prior state is carried forward into the snapshot.
        self.assertIn(first_fp, audit_run.shared_state)
        self.assertEqual(
            audit_run.shared_state[first_fp]["validator"]["analysis"],
            "prior-valid",
        )

    def test_path_in_skip_set_without_prior_state_still_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, analyzer, exploitation, validator, logger, source = _build_workflow_and_plan(tmp)
            first_fp = plan.audit_units[0].path.path_fingerprint
            workflow.run(
                source=source,
                plan=AuditPlan(audit_units=(plan.audit_units[0],)),
                skip_paths=frozenset({first_fp}),
                prior_shared_state={},  # intentionally empty
            )
        # Fell through to fresh execution.
        self.assertGreaterEqual(analyzer.calls, 1)
        # Warning was logged.
        self.assertTrue(
            any("no prior state" in w for w in logger.warnings),
            f"Expected a warning about the empty prior state; got {logger.warnings}",
        )


if __name__ == "__main__":
    unittest.main()
