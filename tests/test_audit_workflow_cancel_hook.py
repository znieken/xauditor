"""``on_cancel`` hot-path hook fires before any cleanup on KeyboardInterrupt.

Regression for the "Ctrl+C → portal stuck on in_progress" bug.
The fix flushes the cancellation status to the portal IMMEDIATELY
when ``KeyboardInterrupt`` is observed (via the ``on_cancel``
callback), BEFORE ``pool.shutdown`` and BEFORE the post-cancel
``on_progress`` snapshot. This way, even if the user mashes Ctrl+C
several more times during the drain (which previously replaced the
in-flight ``UserCancelledError`` with a fresh ``KeyboardInterrupt``,
preventing services.py from running its bookkeeping), the portal
already shows ``status=cancelled`` rather than being stuck on
``in_progress``.
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
from xauditor.errors import UserCancelledError
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.scope import resolve_repository_scope
from xauditor.llm import LLMClient
from xauditor.models import ValidationStatus


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


class _InterruptingAnalyzer:
    """Raises ``KeyboardInterrupt`` on the first call. Used to
    simulate the user pressing Ctrl+C at the very start of the
    audit, before any path can complete."""

    def run(self, *, unit, path_functions, path_context=None, excluded_findings=()):
        del unit, path_functions, path_context, excluded_findings
        raise KeyboardInterrupt()


class _StubExploitation:
    def run(self, *, unit, analyzer, path_context=None):
        del unit, analyzer, path_context
        return ExploitationResult(status="ready", steps="x")


class _StubValidator:
    def run(self, *, unit, analyzer, exploitation=None, path_context=None):
        del unit, analyzer, exploitation, path_context
        return ValidationResult(status=ValidationStatus.VALID, analysis="ok")


class OnCancelHookTests(unittest.TestCase):
    def _build_workflow(self, tmp: str):
        repo_root = Path(tmp)
        (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
        config = load_config(
            repo_root=repo_root,
            env={
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                # 0.10.0+ default worker_count: 1 → InlineExecutor;
                # KeyboardInterrupt fires on the master thread.
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
        workflow = AuditWorkflow(
            config=config,
            llm_client=LLMClient.from_config(config.llm),
            analyzer_agent=_InterruptingAnalyzer(),
            exploitation_agent=_StubExploitation(),
            validator_agent=_StubValidator(),
        )
        return workflow, plan, source

    def test_on_cancel_fires_when_keyboard_interrupt_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = self._build_workflow(tmp)

            cancel_calls: list[tuple[object, str]] = []

            def _on_cancel(snapshot, message: str) -> None:
                cancel_calls.append((snapshot, message))

            with self.assertRaises(UserCancelledError):
                workflow.run(
                    source=source,
                    plan=plan,
                    on_cancel=_on_cancel,
                )

            # Hook must fire exactly once and carry the conventional
            # cancel message + a non-None partial snapshot.
            self.assertEqual(len(cancel_calls), 1)
            snapshot, message = cancel_calls[0]
            self.assertEqual(message, "cancelled by user")
            self.assertIsNotNone(snapshot)

    def test_on_cancel_fires_before_on_progress_final_snapshot(self) -> None:
        # The fix's whole point: portal status flips to "cancelled"
        # on the early hot-path BEFORE the post-cleanup
        # ``on_progress(partial_run)``. Sequence numbers prove the
        # ordering.
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = self._build_workflow(tmp)

            sequence: list[str] = []

            def _on_cancel(snapshot, message):
                del snapshot, message
                sequence.append("on_cancel")

            def _on_progress(snapshot):
                del snapshot
                sequence.append("on_progress")

            with self.assertRaises(UserCancelledError):
                workflow.run(
                    source=source,
                    plan=plan,
                    on_progress=_on_progress,
                    on_cancel=_on_cancel,
                )

            # ``on_cancel`` must appear before any ``on_progress``
            # call that fires from the cancellation handler. (Earlier
            # ``on_progress`` calls during normal path completion
            # would have happened before the KeyboardInterrupt — but
            # in this test the analyzer raises immediately on path 1
            # so no such normal call happens.)
            self.assertIn("on_cancel", sequence)
            self.assertEqual(
                sequence.index("on_cancel"),
                0,
                f"on_cancel must be the first callback fired during "
                f"cancellation; saw sequence={sequence}",
            )

    def test_on_cancel_optional_omitted_does_not_break_run(self) -> None:
        # Running without an ``on_cancel`` hook (e.g. unit tests
        # invoking workflow.run directly) must still raise
        # ``UserCancelledError`` cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = self._build_workflow(tmp)

            with self.assertRaises(UserCancelledError) as ctx:
                workflow.run(source=source, plan=plan)
            self.assertIn("Audit cancelled by user", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
