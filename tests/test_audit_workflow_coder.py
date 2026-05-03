"""Workflow-level integration tests for the asynchronous coder stage.

Uses an in-process fake coder dispatcher so the tests do not spawn real
subprocesses. The fake records the order of submit / poll / drain / cancel
calls so we can assert per-path latency, snapshot emission, and
cancellation semantics without timing-dependent sleeps.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agents import AnalyzerResult, ExploitationResult, ValidationResult
from xauditor.audit.coder import CoderResult
from xauditor.audit.planner import plan_audit_paths
from tests._helpers import _TestOnlyGraphSource
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import load_config
from xauditor.errors import UserCancelledError
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.scope import resolve_repository_scope
from xauditor.llm import LLMClient
from xauditor.models import (
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_PENDING,
    CODER_STATUS_SKIPPED,
    CODER_STATUS_VERIFIED,
    ValidationStatus,
)


SAMPLE_APP = """\
import subprocess


def handle_request(user_input):
    subprocess.run(user_input, shell=True)


def main():
    handle_request("ls")
"""


class _StubAnalyzer:
    def run(self, *, unit, path_functions, path_context=None, subagent_id=None, provider_name=None):
        return AnalyzerResult(
            status="candidate",
            finding_name="Command Injection",
            description=f"{unit.path.entry_function} reaches a dangerous sink.",
            reason="The sink is reachable from the current path.",
            suspect_function_id=path_functions[0].function_id if path_functions else "",
            suspect_line=4,
            evidence_strength="high",
        )


class _StubExploitation:
    def run(self, *, unit, analyzer, path_context=None, subagent_id=None, provider_name=None, extra_payload=None):
        return ExploitationResult(status="ready", steps="Exploit details")


class _StubValidator:
    def run(self, *, unit, analyzer, exploitation, path_context=None):
        return ValidationResult(status=ValidationStatus.VALID, analysis="Validated")


class _FakeDispatcher:
    """In-process stand-in for ``CoderDispatcher``.

    ``simulate_pending=True`` keeps submitted tasks in flight until the
    workflow's end-of-loop drain or cancel_all path runs them. With
    ``simulate_pending=False`` (the default), every submit immediately
    settles, exercising the per-iteration drain.
    """

    def __init__(
        self,
        *,
        results: dict[str, CoderResult] | None = None,
        simulate_pending: bool = False,
    ) -> None:
        self.results = results or {}
        self.submitted: list[str] = []
        self._completed: list[tuple[str, CoderResult]] = []
        self._simulate_pending = simulate_pending
        self.cancel_all_called = 0

    def _result_for(self, finding_id: str) -> CoderResult:
        return self.results.get(
            finding_id,
            CoderResult(status=CODER_STATUS_VERIFIED, analysis="ok", reason="ok"),
        )

    def submit(
        self,
        finding_id: str,
        payload: dict,
        *,
        on_settled=None,
    ) -> None:
        self.submitted.append(finding_id)
        if not self._simulate_pending:
            result = self._result_for(finding_id)
            # Mirror production: in real ThreadPoolExecutor the
            # ``add_done_callback`` fires from a worker thread AFTER
            # ``submit`` returns and after the workflow has appended
            # the finding to ``findings``. Defer the callback until
            # the next ``poll_completed`` / ``drain`` so the test
            # exercises the same ordering.
            self._completed.append((finding_id, result, on_settled))

    def poll_completed(self) -> list[tuple[str, CoderResult]]:
        # Snapshot the deferred completions, fire any registered
        # callbacks (mirroring worker-thread `add_done_callback`),
        # then return only the leftovers (those that didn't have a
        # callback wired) for the caller to merge via the legacy path.
        out: list[tuple[str, CoderResult]] = []
        for entry in self._completed:
            finding_id, result, callback = entry
            if callback is not None:
                callback(finding_id, result)
            else:
                out.append((finding_id, result))
        self._completed = []
        return out

    def pending_finding_ids(self) -> tuple[str, ...]:
        if self._simulate_pending:
            return tuple(self.submitted)
        return ()

    def has_pending(self) -> bool:
        return self._simulate_pending and bool(self.submitted)

    def cancel_all(self) -> tuple[str, ...]:
        self.cancel_all_called += 1
        cancelled = tuple(self.submitted)
        self.submitted = []
        self._simulate_pending = False
        return cancelled

    def drain(self, *, timeout=None, on_settled=None):
        if not self._simulate_pending:
            # Drain leftover deferred completions: invoke per-task
            # callbacks (registered at submit time) and then return any
            # entries whose caller used the legacy poll path.
            out: list[tuple[str, CoderResult]] = []
            for entry in self._completed:
                finding_id, result, callback = entry
                if callback is not None:
                    callback(finding_id, result)
                else:
                    out.append((finding_id, result))
                    if on_settled is not None:
                        on_settled(finding_id, result)
            self._completed = []
            return out
        results = []
        for fid in list(self.submitted):
            result = self._result_for(fid)
            results.append((fid, result))
            if on_settled is not None:
                on_settled(fid, result)
        self.submitted = []
        self._simulate_pending = False
        return results

    def shutdown(self, *, wait: bool = False) -> None:
        return None


def _build(
    repo_root: Path,
    *,
    coder_enabled: bool,
    dispatcher: _FakeDispatcher | None = None,
):
    (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
    env = {
        "XAUDITOR_LLM_BASE_URL": "mock://offline",
        "XAUDITOR_LLM_API_KEY": "secret",
        "XAUDITOR_LLM_MODEL_NAME": "mock-model",
    }
    if coder_enabled:
        env["XAUDITOR_CODER_ENABLED"] = "true"
    config = load_config(repo_root=repo_root, env=env, require_llm=True)
    from xauditor.integrations.neo4j import InMemoryNeo4jAdapter
    from xauditor.integrations.neo4j_repository import InMemoryNeo4jGraphRepository
    adapter = InMemoryNeo4jAdapter()
    repo = InMemoryNeo4jGraphRepository(adapter)
    fingerprint = LangChainGraphBuilder.from_config(config, repository=repo).build(
        repo_root=repo_root,
        scope=resolve_repository_scope(repo_root, ()),
    )
    source = _TestOnlyGraphSource.from_inmemory_repo(repo, fingerprint)
    plan = plan_audit_paths(source)
    workflow = AuditWorkflow(
        config=config,
        llm_client=LLMClient.from_config(config.llm),
        analyzer_agent=_StubAnalyzer(),
        exploitation_agent=_StubExploitation(),
        validator_agent=_StubValidator(),
        coder_dispatcher=dispatcher,  # type: ignore[arg-type]
    )
    return workflow, plan, source


class CoderDisabledTests(unittest.TestCase):
    def test_findings_stay_skipped_when_coder_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = _build(Path(tmp), coder_enabled=False)
            self.assertGreater(len(plan.audit_units), 0)
            run = workflow.run(plan=plan, source=source)
        self.assertGreaterEqual(len(run.findings), 1)
        for finding in run.findings:
            self.assertEqual(finding.coder_status, CODER_STATUS_SKIPPED)
            self.assertEqual(finding.coder_call_chain_evidence, ())


class CoderFastPathTests(unittest.TestCase):
    """When the dispatcher settles synchronously, verdicts apply during the loop."""

    def test_synchronous_dispatch_settles_findings(self) -> None:
        dispatcher = _FakeDispatcher(
            results={
                "F-0001": CoderResult(
                    status=CODER_STATUS_NOT_VERIFIED, analysis="bad", reason="sanitizer"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = _build(
                Path(tmp), coder_enabled=True, dispatcher=dispatcher
            )
            run = workflow.run(plan=plan, source=source)
        self.assertGreaterEqual(len(run.findings), 1)
        # Every submitted finding ended up settled, none Pending.
        self.assertNotIn(CODER_STATUS_PENDING, {f.coder_status for f in run.findings})
        self.assertGreaterEqual(len(dispatcher.submitted), 0)


class CoderSlowDispatcherTests(unittest.TestCase):
    """Pending tasks settle during the run-close drain."""

    def test_run_close_drains_pending_tasks(self) -> None:
        dispatcher = _FakeDispatcher(simulate_pending=True)
        snapshots: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = _build(
                Path(tmp), coder_enabled=True, dispatcher=dispatcher
            )

            def _on_progress(snapshot):
                pending = sum(
                    1 for f in snapshot.findings if f.coder_status == CODER_STATUS_PENDING
                )
                verified = sum(
                    1 for f in snapshot.findings if f.coder_status == CODER_STATUS_VERIFIED
                )
                snapshots.append((pending, verified))

            run = workflow.run(plan=plan, source=source, on_progress=_on_progress)
        # During the per-path loop, findings were Pending; after the
        # end-of-run drain, every finding has a terminal verdict.
        for finding in run.findings:
            self.assertEqual(finding.coder_status, CODER_STATUS_VERIFIED)
        # The drain emitted at least one snapshot whose pending count is 0.
        self.assertTrue(
            any(pending == 0 and verified > 0 for pending, verified in snapshots),
            f"expected a snapshot with all coder verdicts settled, got {snapshots}",
        )


class CoderCancellationTests(unittest.TestCase):
    """Cancellation marks pending findings as Skipped with the user reason."""

    def test_keyboard_interrupt_marks_pending_skipped(self) -> None:
        dispatcher = _FakeDispatcher(simulate_pending=True)
        # Raise KeyboardInterrupt from within on_progress on the FIRST
        # heartbeat only. The cancellation handler also calls on_progress to
        # emit the partial-run snapshot; we let that one through cleanly.
        triggers = {"count": 0}

        def _on_progress(_snapshot):
            triggers["count"] += 1
            if triggers["count"] == 1:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            workflow, plan, source = _build(
                Path(tmp), coder_enabled=True, dispatcher=dispatcher
            )
            with self.assertRaises(UserCancelledError) as ctx:
                workflow.run(plan=plan, source=source, on_progress=_on_progress)
        partial = ctx.exception.partial_audit_run
        self.assertIsNotNone(partial)
        self.assertGreaterEqual(dispatcher.cancel_all_called, 1)
        # Every finding that was dispatched ended up Skipped (cancelled by user).
        skipped = [
            f for f in partial.findings if f.coder_status == CODER_STATUS_SKIPPED
        ]
        self.assertGreaterEqual(len(skipped), 1)
        for finding in skipped:
            self.assertEqual(finding.coder_reason, "cancelled by user")

    def test_late_worker_callback_after_cancel_does_not_overwrite_skipped(self) -> None:
        """Regression: a worker thread whose claude subprocess was just
        SIGTERM'd by ``cancel_all`` will fire ``on_settled`` with an
        Inconclusive result (transport error: exit -SIGTERM). That
        callback MUST NOT overwrite the Skipped state set by the
        cancel handler — otherwise the portal shows a confusing mix of
        Skipped and Inconclusive findings for the same cancelled run.
        ``AuditWorkflow._cancelling`` is a ``threading.Event`` that
        the stream callback checks; once raised, all subsequent
        callbacks snap their result to Skipped regardless of what the
        worker produced."""

        from xauditor.audit.coder import CoderResult
        from xauditor.models import CODER_STATUS_INCONCLUSIVE

        with tempfile.TemporaryDirectory() as tmp:
            workflow, _plan, _source = _build(
                Path(tmp), coder_enabled=True,
                dispatcher=_FakeDispatcher(simulate_pending=False),
            )
            # Build a stream callback the same way the workflow does.
            findings: list = []
            pending: set[str] = set()
            workflow._cancelling.set()  # Simulate "we're inside the cancel handler"

            def _snapshot_factory():
                return None

            on_progress_calls: list = []
            upsert_calls: list = []

            def _on_progress(snap):
                on_progress_calls.append(snap)

            def _on_finding_upsert(finding):
                upsert_calls.append(finding)

            callback = workflow._make_stream_callback(
                findings=findings,
                pending_coder=pending,
                on_progress=_on_progress,
                snapshot_factory=_snapshot_factory,
                on_finding_upsert=_on_finding_upsert,
            )

            # Inject a Finding so the callback can find an index.
            from xauditor.models import (
                CODER_STATUS_PENDING,
                ConfidenceLevel,
                Finding,
                ValidationStatus,
            )
            stub = Finding(
                finding_id="F-1",
                finding_name="x", finding_description="x",
                confidence_level=ConfidenceLevel.HIGH,
                source_references=(),
                analysis="", reason="",
                context="", business_context="",
                exploitation_status="", exploitation_steps="",
                validation_status=ValidationStatus.VALID,
                validation_analysis="",
                path_fingerprint="p",
                coder_status=CODER_STATUS_PENDING,
            )
            findings.append(stub)

            # Worker fires with a transport-error Inconclusive (mimics what
            # parse_coder_response yields after SIGTERM: exit code -15).
            worker_result = CoderResult(
                status=CODER_STATUS_INCONCLUSIVE,
                analysis="",
                reason="transport error: exit -15",
                evidence=(),
                cli_exit_code=-15,
                cli_stderr=None,
                duration_ms=20,
            )
            callback("F-1", worker_result)

            # The flag is set → the callback should have snapped the
            # finding to Skipped (cancelled by user), NOT Inconclusive.
            from xauditor.models import CODER_STATUS_SKIPPED
            self.assertEqual(findings[0].coder_status, CODER_STATUS_SKIPPED)
            self.assertEqual(findings[0].coder_reason, "cancelled by user")
            # Specifically: the worker's "transport error" reason must NOT leak through.
            self.assertNotIn("transport error", findings[0].coder_reason)
            # The streaming callback uses the per-row upsert path, NOT the
            # legacy emit_snapshot path: ``on_finding_upsert`` fires once
            # with the snapped finding and ``on_progress`` is never called.
            self.assertEqual(len(upsert_calls), 1)
            self.assertEqual(upsert_calls[0].finding_id, "F-1")
            self.assertEqual(upsert_calls[0].coder_status, CODER_STATUS_SKIPPED)
            self.assertEqual(on_progress_calls, [])


if __name__ == "__main__":
    unittest.main()
