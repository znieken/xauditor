"""Cascade-cancellation when ``RunDeletedExternallyError`` propagates.

Regression for the "audit stuck after Delete" bug. When the portal admin
deletes an in-progress run, the next sink write on the main thread (typically
``on_progress(snapshot)`` after a path completes) raises
``RunDeletedExternallyError``. Without the cascade, the workflow's
``finally`` block calls ``_shutdown_pool_with_deadline`` which polls
indefinitely because no cancellation deadline is armed; the audit appears
stuck for the duration of in-flight LLM calls.

The fix: a new ``except RunDeletedExternallyError:`` handler in
``AuditWorkflow.run`` calls ``pool.cancel_all()``,
``coder_dispatcher.cancel_all()`` (if coder enabled),
``self.cancellation.set_cancelled()``, and ``self._cancelling.set()``
before re-raising. This arms the drain-deadline (cuts per-worker grace
from 10s to 0.5s) and gates the post-loop coder drain.

This test asserts the workflow's instance state after the cascade fires:
``cancellation.is_cancelled()`` is True and ``self._cancelling.is_set()``
is True. The pool/dispatcher cancel_all calls themselves are exercised
indirectly via the cancellation flag (which the pool's shutdown observes).
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit._cancellation import RunCancellation
from xauditor.audit.agents import AnalyzerResult, ExploitationResult, ValidationResult
from xauditor.audit.planner import plan_audit_paths
from tests._helpers import _TestOnlyGraphSource
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import load_config
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.scope import resolve_repository_scope
from xauditor.llm import LLMClient
from xauditor.models import ValidationStatus

from xauditor_portal.sinks import RunDeletedExternallyError


SAMPLE_APP = textwrap.dedent(
    """
    import subprocess

    def sink(user_input):
        return subprocess.run(user_input, shell=True)

    def handler_a(user_input):
        return sink(user_input)
    """
).strip() + "\n"


class _StubAnalyzer:
    """Lets the path complete (with or without a finding) so on_progress fires."""

    def run(
        self,
        *,
        unit,
        path_functions,
        path_context=None,
        subagent_id=None,
        provider_name=None,
    ):
        del path_context, subagent_id, provider_name
        return AnalyzerResult(
            status="candidate",
            finding_name="stub finding",
            description=f"stub finding for {unit.path.entry_function}",
            reason="cascade-test",
            suspect_function_id=(
                path_functions[0].function_id if path_functions else ""
            ),
            suspect_line=4,
            evidence_strength="high",
        )


class _StubExploitation:
    def run(self, *, unit, analyzer, path_context=None):
        del unit, analyzer, path_context
        return ExploitationResult(status="ready", steps="x")


class _StubValidator:
    def run(self, *, unit, analyzer, exploitation, path_context=None):
        del unit, analyzer, exploitation, path_context
        return ValidationResult(status=ValidationStatus.VALID, analysis="ok")


class DeleteCascadeTests(unittest.TestCase):
    def _build_workflow(self, tmp: str) -> tuple[AuditWorkflow, object, object]:
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
        from xauditor.integrations.neo4j_repository import (
            InMemoryNeo4jGraphRepository,
        )

        adapter = InMemoryNeo4jAdapter()
        repo = InMemoryNeo4jGraphRepository(adapter)
        fingerprint = LangChainGraphBuilder.from_config(
            config, repository=repo
        ).build(
            repo_root=repo_root, scope=resolve_repository_scope(repo_root, ())
        )
        source = _TestOnlyGraphSource.from_inmemory_repo(repo, fingerprint)
        plan = plan_audit_paths(source)
        cancellation = RunCancellation(timeout_seconds=30.0)
        workflow = AuditWorkflow(
            config=config,
            llm_client=LLMClient.from_config(config.llm),
            analyzer_agent=_StubAnalyzer(),
            exploitation_agent=_StubExploitation(),
            validator_agent=_StubValidator(),
            cancellation=cancellation,
        )
        return workflow, plan, source

    def test_run_deleted_externally_arms_cancellation_and_cancelling_flag(
        self,
    ) -> None:
        """When ``on_progress`` raises ``RunDeletedExternallyError``,
        the workflow's new ``except`` handler SHALL set both
        ``cancellation`` and ``_cancelling`` before re-raising."""

        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = self._build_workflow(tmp)

            run_id = "00000000-0000-0000-0000-000000000001"

            def _on_progress(snapshot):
                del snapshot
                raise RunDeletedExternallyError(run_id)

            with self.assertRaises(RunDeletedExternallyError) as cm:
                workflow.run(
                    source=source,
                    plan=plan,
                    on_progress=_on_progress,
                )

            self.assertEqual(cm.exception.run_id, run_id)
            self.assertTrue(
                workflow.cancellation.is_cancelled(),
                "RunDeletedExternallyError handler must call "
                "cancellation.set_cancelled() so _shutdown_pool_with_deadline "
                "escalates within audit.shutdown_timeout_seconds",
            )
            self.assertTrue(
                workflow._cancelling.is_set(),
                "RunDeletedExternallyError handler must set _cancelling "
                "so the post-loop coder drain is skipped",
            )

    def test_run_deleted_externally_propagates_not_swallowed(self) -> None:
        """The handler MUST re-raise. Swallowing would let the workflow
        complete normally against a deleted run, which is the bug we
        are preventing."""

        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = self._build_workflow(tmp)

            def _on_progress(snapshot):
                del snapshot
                raise RunDeletedExternallyError("any-id")

            # The exception type SHALL be RunDeletedExternallyError, NOT
            # UserCancelledError. Setting cancellation from the handler
            # does NOT trigger UserCancelledError raising on the
            # unwinding path.
            with self.assertRaises(RunDeletedExternallyError):
                workflow.run(
                    source=source,
                    plan=plan,
                    on_progress=_on_progress,
                )


if __name__ == "__main__":
    unittest.main()
