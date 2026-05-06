"""Per-path isolation when an analyzer raises ``LLMError``.

A single LLM-stack failure on one path SHALL NOT abort the audit run; the
failing path SHALL be recorded with ``CoverageState.FAILED`` and a
``failed_llm_error`` checkpoint while sibling paths continue executing.
This test exercises the ``except LLMError`` clause added to
``AuditWorkflow.run`` alongside the existing ``ContextWindowExceededError``
handler.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agents import AnalyzerResult, ExploitationResult, ValidationResult
from xauditor.audit.planner import plan_audit_paths
from tests._helpers import _TestOnlyGraphSource
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import load_config
from xauditor.errors import LLMError
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.scope import resolve_repository_scope
from xauditor.llm import LLMClient
from xauditor.models import CoverageState, ValidationStatus


SAMPLE_APP = textwrap.dedent(
    """
    import subprocess

    def sink(user_input):
        return subprocess.run(user_input, shell=True)

    def handler_a(user_input):
        return sink(user_input)

    def handler_b(user_input):
        return sink(user_input)
    """
).strip() + "\n"


class _FailingAnalyzer:
    """Analyzer that raises ``LLMError`` for one fingerprint and succeeds for the rest."""

    def __init__(self, fingerprint_to_fail: str) -> None:
        self._fail = fingerprint_to_fail
        self.calls: list[str] = []

    def run(self, *, unit, path_functions, path_context=None, excluded_findings=()):
        del path_functions, path_context, excluded_findings
        fp = unit.path.path_fingerprint
        self.calls.append(fp)
        if fp == self._fail:
            raise LLMError(
                "synthetic LLM stack failure",
                operation="llm.analyzer",
                provider="test-provider",
                model_name="test-model",
            )
        return AnalyzerResult(
            status="candidate",
            finding_name="Command Injection via shell=True",
            description=f"{unit.path.entry_function} reaches a dangerous sink.",
            reason="Sink is reachable.",
            suspect_function_id=unit.function_ids[0] if unit.function_ids else "",
            suspect_line=4,
            evidence_strength="high",
        )


class _SuccessExploitation:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *, unit, analyzer, path_context=None):
        del analyzer, path_context
        self.calls.append(unit.path.path_fingerprint)
        return ExploitationResult(status="ready", steps="Exploit details")


class _SuccessValidator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *, unit, analyzer, exploitation=None, path_context=None):
        del analyzer, exploitation, path_context
        self.calls.append(unit.path.path_fingerprint)
        return ValidationResult(status=ValidationStatus.VALID, analysis="Validated")


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, message: str) -> None:
        self.events.append(("info", {"message": message}))

    def warning(self, message: str) -> None:
        self.events.append(("warning", {"message": message}))

    def debug(self, message: str) -> None:
        self.events.append(("debug", {"message": message}))

    def debug_kv(self, message: str, **details) -> None:
        self.events.append(("debug_kv", {"message": message, **details}))

    def error_kv(self, message: str, **details) -> None:
        self.events.append(("error_kv", {"message": message, **details}))


class WorkflowLLMErrorIsolationTests(unittest.TestCase):
    def _build(self, tmp: str, *, fail_first: bool):
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
        self.assertGreaterEqual(
            len(plan.audit_units),
            2,
            "Test fixture must produce at least 2 distinct paths.",
        )
        target_idx = 0 if fail_first else len(plan.audit_units) - 1
        target_fp = plan.audit_units[target_idx].path.path_fingerprint
        analyzer = _FailingAnalyzer(target_fp)
        exploitation = _SuccessExploitation()
        validator = _SuccessValidator()
        logger = _RecordingLogger()
        workflow = AuditWorkflow(
            config=config,
            llm_client=LLMClient.from_config(config.llm),
            analyzer_agent=analyzer,
            exploitation_agent=exploitation,
            validator_agent=validator,
            logger=logger,
        )
        return workflow, plan, source, target_fp, analyzer, exploitation, validator, logger

    def test_failing_path_marked_failed_other_paths_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (
                workflow,
                plan,
                source,
                failing_fp,
                analyzer,
                exploitation,
                validator,
                logger,
            ) = self._build(tmp, fail_first=True)
            audit_run = workflow.run(source=source, plan=plan)

        # Coverage: failing path is FAILED, the rest are AUDITED.
        path_records = {
            record.identifier: record.state
            for record in audit_run.coverage.items("path")
        }
        self.assertEqual(
            path_records[failing_fp],
            CoverageState.FAILED,
            f"path_records: {path_records}",
        )
        non_failed = {fp: state for fp, state in path_records.items() if fp != failing_fp}
        self.assertTrue(
            all(state == CoverageState.AUDITED for state in non_failed.values()),
            f"Expected non-failing paths to be AUDITED: {non_failed}",
        )

        # Checkpoint + shared_state for the failing path follow the spec shape.
        self.assertEqual(
            audit_run.checkpoints.get(failing_fp),
            "failed_llm_error",
        )
        failed_block = audit_run.shared_state.get(failing_fp, {}).get("failed")
        self.assertIsNotNone(failed_block)
        assert failed_block is not None  # for type-checker
        self.assertEqual(failed_block["reason"], "llm_error")
        self.assertEqual(failed_block["operation"], "llm.analyzer")
        self.assertEqual(failed_block["provider"], "test-provider")
        self.assertEqual(failed_block["model"], "test-model")
        self.assertIn("synthetic LLM stack failure", failed_block["cause"])

        # Sibling paths still ran exploitation + validator.
        sibling_count = len(plan.audit_units) - 1
        self.assertEqual(len(exploitation.calls), sibling_count)
        self.assertEqual(len(validator.calls), sibling_count)
        self.assertNotIn(failing_fp, exploitation.calls)
        self.assertNotIn(failing_fp, validator.calls)

        # Findings exist for the sibling paths.
        self.assertGreaterEqual(len(audit_run.findings), 1)
        for finding in audit_run.findings:
            self.assertNotEqual(finding.path_fingerprint, failing_fp)

        # Logger captured the failure event.
        error_events = [
            details for kind, details in logger.events
            if kind == "error_kv" and details.get("message") == "Audit path failed: LLM error"
        ]
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0]["operation"], "llm.analyzer")
        self.assertEqual(error_events[0]["path"], failing_fp)

        # Failed path SHALL NOT appear in audited_call_chains.
        chain_fps = {chain.path_fingerprint for chain in audit_run.coverage.audited_call_chains}
        self.assertNotIn(failing_fp, chain_fps)

    def test_failing_last_path_does_not_swallow_prior_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (
                workflow,
                plan,
                source,
                failing_fp,
                analyzer,
                exploitation,
                validator,
                logger,
            ) = self._build(tmp, fail_first=False)
            audit_run = workflow.run(source=source, plan=plan)
        path_records = {
            record.identifier: record.state
            for record in audit_run.coverage.items("path")
        }
        self.assertEqual(path_records[failing_fp], CoverageState.FAILED)
        self.assertGreaterEqual(len(audit_run.findings), 1)


if __name__ == "__main__":
    unittest.main()
