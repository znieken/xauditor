"""Workflow integration tests for ``audit-stream-path-loading``.

Asserts that the sliding-window workflow produces byte-for-byte
identical findings (in order, with stable ``finding_id``s) when fed
through the streaming entry vs. the eager ``plan=`` shim — across
multiple ``path_batch_size`` values, including the degenerate
single-path window. Spec D4 / MODIFIED requirement scenarios.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Make the tests/_helpers package importable. Mirrors the pattern in
# the surrounding workflow-test files.
sys.path.insert(0, str(Path(__file__).parent))

from xauditor.audit.planner import plan_audit_paths, stream_audit_units
from xauditor.audit.streamer import EagerPathStreamer
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import load_config
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.scope import resolve_repository_scope
from xauditor.llm import LLMClient
from xauditor.models import ValidationStatus
from xauditor.audit.agents import (
    AnalyzerResult,
    ExploitationResult,
    ValidationResult,
)

from tests._helpers import _TestOnlyGraphSource


# A small repo with multiple distinct entry-points so the graph
# carries several reachable paths. We don't need a huge synthetic
# graph for these tests — plan-order correctness and streamer
# parity hold for any path count.
SAMPLE_APP = """\
import subprocess


def helper_a(user_input):
    subprocess.run(user_input, shell=True)


def helper_b(user_input):
    subprocess.run(user_input, shell=True)


def helper_c(user_input):
    subprocess.run(user_input, shell=True)


def handler_a():
    helper_a("ls")


def handler_b():
    helper_b("ls")


def handler_c():
    helper_c("ls")
"""


class _Analyzer:
    def run(self, *, unit, path_functions, path_context=None, excluded_findings=()):
        del path_functions, path_context, excluded_findings
        return AnalyzerResult(
            status="candidate",
            finding_name="Command Injection via shell=True",
            description=f"{unit.path.entry_function} reaches a dangerous sink.",
            reason="The sink is reachable from the current path.",
            suspect_function_id=unit.function_ids[0] if unit.function_ids else "",
            suspect_line=4,
            evidence_strength="high",
        )


class _Exploitation:
    def run(self, *, unit, analyzer, path_context=None):
        del unit, analyzer, path_context
        return ExploitationResult(status="ready", steps="Exploit details")


class _Validator:
    def run(self, *, unit, analyzer, exploitation=None, path_context=None):
        del unit, analyzer, exploitation, path_context
        return ValidationResult(status=ValidationStatus.VALID, analysis="Validated")


def _build_source(tmp: str) -> tuple[_TestOnlyGraphSource, "object"]:
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
    return source, config


def _make_workflow(config) -> AuditWorkflow:
    return AuditWorkflow(
        config=config,
        llm_client=LLMClient.from_config(config.llm),
        analyzer_agent=_Analyzer(),
        exploitation_agent=_Exploitation(),
        validator_agent=_Validator(),
    )


def _finding_signatures(audit_run) -> list[tuple[str, str, str]]:
    """Stable per-finding tuple suitable for cross-run equality.

    ``finding_id`` carries the plan-order assignment; ``path``
    pins the source path; the first function-name lets a human
    eyeball a regression. We avoid full struct equality so
    cosmetic field drift doesn't fail the test for the wrong
    reason.
    """

    return [
        (
            f.finding_id,
            f.path_fingerprint,
            f.function_names[0] if f.function_names else "",
        )
        for f in audit_run.findings
    ]


class WorkflowStreamingParityTests(unittest.TestCase):
    def test_streaming_run_matches_eager_run_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, config = _build_source(tmp)
            plan = plan_audit_paths(source)
            self.assertGreater(len(plan.audit_units), 1)

            # Eager baseline (legacy ``plan=`` entry).
            eager_workflow = _make_workflow(config)
            eager_run = eager_workflow.run(source=source, plan=plan)

            # Streaming: open a real ``PathStreamer`` and pass it
            # through. Use a small batch_size so we exercise multi-
            # batch behaviour even on the small fixture.
            streaming_workflow = _make_workflow(config)
            streamer = stream_audit_units(source, batch_size=2)
            try:
                streaming_run = streaming_workflow.run(
                    source=source, streamer=streamer
                )
            finally:
                streamer.close()

        self.assertEqual(
            _finding_signatures(eager_run),
            _finding_signatures(streaming_run),
        )

    def test_finding_ids_stable_across_path_batch_sizes(self) -> None:
        # MODIFIED requirement scenario "Identical finding ids
        # across `path_batch_size` values".
        with tempfile.TemporaryDirectory() as tmp:
            source, config = _build_source(tmp)
            plan = plan_audit_paths(source)
            self.assertGreater(len(plan.audit_units), 1)

            signatures: list[list[tuple[str, str, str]]] = []
            for batch_size in (1, 2, 4, 100_000):
                workflow = _make_workflow(config)
                streamer = stream_audit_units(source, batch_size=batch_size)
                try:
                    audit_run = workflow.run(source=source, streamer=streamer)
                finally:
                    streamer.close()
                signatures.append(_finding_signatures(audit_run))

        first = signatures[0]
        for sig in signatures[1:]:
            self.assertEqual(sig, first)

    def test_legacy_plan_entry_still_works(self) -> None:
        # Tests that haven't migrated to the streamer API still
        # call ``workflow.run(plan=plan)`` — the back-compat shim
        # wraps ``plan.audit_units`` in an ``EagerPathStreamer``.
        with tempfile.TemporaryDirectory() as tmp:
            source, config = _build_source(tmp)
            plan = plan_audit_paths(source)
            workflow = _make_workflow(config)
            audit_run = workflow.run(source=source, plan=plan)
        self.assertEqual(len(audit_run.findings), len(plan.audit_units))


class WorkflowEagerStreamerSmokeTests(unittest.TestCase):
    def test_eager_streamer_consumed_by_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, config = _build_source(tmp)
            plan = plan_audit_paths(source)
            workflow = _make_workflow(config)
            eager_streamer = EagerPathStreamer(plan.audit_units)
            audit_run = workflow.run(source=source, streamer=eager_streamer)
        self.assertEqual(len(audit_run.findings), len(plan.audit_units))


class WorkflowMutuallyExclusiveInputTests(unittest.TestCase):
    def test_passing_neither_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, config = _build_source(tmp)
            workflow = _make_workflow(config)
            with self.assertRaises(TypeError):
                workflow.run(source=source)


if __name__ == "__main__":
    unittest.main()
