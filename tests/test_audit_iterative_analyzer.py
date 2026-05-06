"""Tests for fast-mode iterative analyzer + stage reorder + downgrade.

Scope: ``AuditWorkflow._process_unit_single`` with the new
``Analyzer → Validator → Exploiter`` order, the iterative
``excluded_findings`` loop, the
``max_findings_per_unit`` cap, the excluded-candidate guard, and
the exploiter's ``not_exploitable`` downgrade channel.

Drives the workflow with stub agents so the loop's termination
conditions can be exercised deterministically without calling out
to a real LLM.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agents import (
    AnalyzerResult,
    ExploitationResult,
    ValidationResult,
)
from xauditor.audit.workflow import AuditWorkflow, _matches_any_excluded
from xauditor.config import (
    AuditConfig,
    AuditModeConfig,
    AuditReplicationConfig,
    LLMConfig,
    LLMSettings,
    Neo4jConfig,
    PortalConfig,
    RepositoryConfig,
    ReportDBConfig,
    RuntimeConfig,
    LoggingConfig,
    GraphConfig,
    CoderConfig,
    XAuditorConfig,
)
from xauditor.llm import LLMClient
from xauditor.models import AuditUnit, FunctionRecord, PathRecord, ValidationStatus


def _config(*, max_findings_per_unit: int = 3, repo_root: Path | None = None) -> XAuditorConfig:
    if repo_root is None:
        repo_root = Path("/tmp")
    return XAuditorConfig(
        repo_root=repo_root,
        graphdb=Neo4jConfig(),
        repository=RepositoryConfig(),
        runtime=RuntimeConfig(root_dir=Path("/tmp/.xauditor")),
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
            max_findings_per_unit=max_findings_per_unit,
        ),
        reportdb=ReportDBConfig(),
        portal=PortalConfig(),
        coder=CoderConfig(),
        audit=AuditConfig(),
    )


def _unit() -> AuditUnit:
    path = PathRecord(
        entry_function="main",
        function_names=("main", "handle_request"),
        file_paths=("app.py",),
        path_fingerprint="fp::test",
        function_ids=("fn::main", "fn::handle"),
    )
    return AuditUnit(path=path, function_ids=("fn::main", "fn::handle"))


def _path_functions() -> list[FunctionRecord]:
    return [
        FunctionRecord(
            function_id="fn::main",
            name="main",
            qualified_name="main",
            file_path="app.py",
            module_name="app",
            start_line=1,
            end_line=5,
            source="def main(): handle_request('ls')",
        ),
        FunctionRecord(
            function_id="fn::handle",
            name="handle_request",
            qualified_name="handle_request",
            file_path="app.py",
            module_name="app",
            start_line=7,
            end_line=10,
            source="def handle_request(x): subprocess.run(x, shell=True)",
        ),
    ]


@dataclass
class _ScriptedAnalyzer:
    """Returns a pre-scripted list of analyzer results, one per call."""

    scripted: list[AnalyzerResult] = field(default_factory=list)
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def run(
        self,
        *,
        unit,
        path_functions,
        path_context=None,
        excluded_findings=(),
        subagent_id=None,
        provider_name=None,
    ):
        del unit, path_functions, path_context, subagent_id, provider_name
        self.calls.append(("run", tuple(excluded_findings)))
        idx = len(self.calls) - 1
        if idx < len(self.scripted):
            return self.scripted[idx]
        # Default: keep returning no_issue once the script runs out.
        return AnalyzerResult(status="no_issue")


@dataclass
class _ConstValidator:
    status: ValidationStatus = ValidationStatus.VALID
    analysis: str = "ok"
    calls: int = 0
    received_exploitation: list[Any] = field(default_factory=list)

    def run(self, *, unit, analyzer, exploitation=None, path_context=None):
        del unit, analyzer, path_context
        self.calls += 1
        # Track every value the workflow passes for `exploitation` —
        # Phase 1B's stage reorder requires this to ALWAYS be
        # `None` (the validator runs before the exploiter).
        self.received_exploitation.append(exploitation)
        return ValidationResult(status=self.status, analysis=self.analysis)


@dataclass
class _ConstExploitation:
    status: str = "ready"
    steps: str = "step"
    calls: int = 0

    def run(self, *, unit, analyzer, path_context=None, subagent_id=None, provider_name=None, extra_payload=None):
        del unit, analyzer, path_context, subagent_id, provider_name, extra_payload
        self.calls += 1
        return ExploitationResult(status=self.status, steps=self.steps)


class _ListLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debug_kvs: list[tuple[str, dict]] = []

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        pass

    def error_kv(self, message: str, **kwargs) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def debug_kv(self, message: str, **kwargs) -> None:
        self.debug_kvs.append((message, dict(kwargs)))


def _build_workflow(
    cfg: XAuditorConfig,
    analyzer: _ScriptedAnalyzer,
    validator: _ConstValidator,
    exploitation: _ConstExploitation,
    logger: _ListLogger,
) -> AuditWorkflow:
    return AuditWorkflow(
        config=cfg,
        llm_client=MagicMock(spec=LLMClient),
        analyzer_agent=analyzer,
        exploitation_agent=exploitation,
        validator_agent=validator,
        logger=logger,
    )


class IterativeAnalyzerLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        # Create the file so `_analyzer_payload` → `_render_chain_snippets`
        # can read source on disk.
        (self.repo_root / "app.py").write_text(
            # 10+ lines so the FunctionRecord end_line=10 stays in range
            # for `render_source_snippet`.
            "def main():\n"
            "    handle_request('ls')\n"
            "\n"
            "\n"
            "\n"
            "def handle_request(x):\n"
            "    import subprocess\n"
            "    subprocess.run(x, shell=True)\n"
            "    return None\n"
            "    # tail line\n",
            encoding="utf-8",
        )

    def test_loop_terminates_on_two_consecutive_no_issue(self) -> None:
        """One candidate, then two no_issue → exactly 1 finding accepted."""

        cfg = _config(max_findings_per_unit=5, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name="SQLi",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                    evidence_strength="high",
                ),
                AnalyzerResult(status="no_issue"),
                AnalyzerResult(status="no_issue"),
            ]
        )
        validator = _ConstValidator()
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, shared_state, checkpoint = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        # Exactly 3 analyzer calls (1 candidate + 2 no_issue), 1 validator + 1 exploitation
        self.assertEqual(len(analyzer.calls), 3)
        self.assertEqual(validator.calls, 1)
        self.assertEqual(exploitation.calls, 1)
        # Exactly 1 finding accepted.
        self.assertEqual(len(per_finding), 1)
        self.assertEqual(per_finding[0][0].finding_name, "SQLi")
        self.assertTrue(per_finding[0][3])  # is_candidate
        self.assertEqual(checkpoint, "Valid")
        # No cap-reached warning.
        self.assertFalse(any("cap reached" in w for w in logger.warnings))
        # Termination metadata.
        iters = shared_state["analyzer_iterations"]
        self.assertEqual(iters["terminated_by"], "convergence")
        self.assertEqual(iters["round_count"], 3)
        self.assertEqual(len(iters["accepted"]), 1)

    def test_loop_terminates_on_cap_with_warning(self) -> None:
        """Analyzer keeps returning new distinct candidates → cap caps."""

        cap = 2
        cfg = _config(max_findings_per_unit=cap, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name=f"F{i}",
                    suspect_function_id="fn::handle",
                    suspect_line=10 + i,
                    evidence_strength="high",
                )
                for i in range(5)
            ]
        )
        validator = _ConstValidator()
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, shared_state, checkpoint = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        # Exactly cap analyzer calls (loop exits when len(accepted) == cap).
        self.assertEqual(len(analyzer.calls), cap)
        self.assertEqual(validator.calls, cap)
        self.assertEqual(exploitation.calls, cap)
        self.assertEqual(len(per_finding), cap)
        # Cap-reached warning logged.
        self.assertTrue(
            any("max_findings_per_unit cap reached" in w for w in logger.warnings),
            f"Expected cap-reached warning; got {logger.warnings}",
        )
        # Termination metadata.
        iters = shared_state["analyzer_iterations"]
        self.assertEqual(iters["terminated_by"], "cap")
        self.assertEqual(len(iters["accepted"]), cap)

    def test_excluded_candidate_treated_as_no_issue(self) -> None:
        """Misbehaving model returns same candidate twice → guard catches it."""

        cfg = _config(max_findings_per_unit=5, repo_root=self.repo_root)
        # Same key three times → first accepted, next two are excluded matches.
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name="SQLi",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                    evidence_strength="high",
                ),
                AnalyzerResult(
                    status="candidate",
                    finding_name="SQLi",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                ),
                AnalyzerResult(
                    status="candidate",
                    finding_name="SQLi",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                ),
            ]
        )
        validator = _ConstValidator()
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, shared_state, _ = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        # Validator + exploitation only ran once (for the first acceptance).
        self.assertEqual(validator.calls, 1)
        self.assertEqual(exploitation.calls, 1)
        self.assertEqual(len(per_finding), 1)
        # Two excluded-candidate warnings logged.
        excluded_warnings = [
            w for w in logger.warnings if "returned excluded candidate" in w
        ]
        self.assertEqual(len(excluded_warnings), 2)
        # Loop exited via convergence (two consecutive "treated as no_issue").
        self.assertEqual(
            shared_state["analyzer_iterations"]["terminated_by"], "convergence"
        )

    def test_two_distinct_vulnerabilities_yield_two_findings(self) -> None:
        """End-to-end: SQLi + XSS on the same path → two findings."""

        cfg = _config(max_findings_per_unit=3, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name="SQLi",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                    evidence_strength="high",
                ),
                AnalyzerResult(
                    status="candidate",
                    finding_name="XSS",
                    suspect_function_id="fn::main",
                    suspect_line=3,
                    evidence_strength="medium",
                ),
                AnalyzerResult(status="no_issue"),
                AnalyzerResult(status="no_issue"),
            ]
        )
        validator = _ConstValidator()
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, shared_state, _ = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        self.assertEqual(len(per_finding), 2)
        self.assertEqual(per_finding[0][0].finding_name, "SQLi")
        self.assertEqual(per_finding[1][0].finding_name, "XSS")
        # Each candidate ran a full chain.
        self.assertEqual(validator.calls, 2)
        self.assertEqual(exploitation.calls, 2)
        # Excluded-findings list grew across calls.
        # Call 1: empty. Call 2: 1 entry. Call 3: 2 entries. Call 4: 2 entries.
        excluded_sizes = [len(call[1]) for call in analyzer.calls]
        self.assertEqual(excluded_sizes, [0, 1, 2, 2])

    def test_false_positive_skips_exploiter(self) -> None:
        """Validator returns FP → exploiter is NOT invoked."""

        cfg = _config(max_findings_per_unit=3, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name="X",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                ),
                AnalyzerResult(status="no_issue"),
                AnalyzerResult(status="no_issue"),
            ]
        )
        validator = _ConstValidator(status=ValidationStatus.FALSE_POSITIVE, analysis="nope")
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, _, _ = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        # Exploitation never ran.
        self.assertEqual(exploitation.calls, 0)
        # The FP finding is still in per_finding (with skipped exploitation).
        self.assertEqual(len(per_finding), 1)
        self.assertEqual(per_finding[0][2].status, ValidationStatus.FALSE_POSITIVE)
        self.assertEqual(per_finding[0][1].status, "skipped")

    def test_not_exploitable_downgrades_valid_to_partial(self) -> None:
        """Validator says Valid + exploiter says not_exploitable → Partial Valid."""

        cfg = _config(max_findings_per_unit=3, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name="X",
                    suspect_function_id="fn::handle",
                    suspect_line=8,
                ),
                AnalyzerResult(status="no_issue"),
                AnalyzerResult(status="no_issue"),
            ]
        )
        validator = _ConstValidator(status=ValidationStatus.VALID, analysis="seems valid")
        exploitation = _ConstExploitation(
            status="not_exploitable",
            steps="preconditions cannot be constructed",
        )
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, _, _ = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        self.assertEqual(len(per_finding), 1)
        downgraded_validator = per_finding[0][2]
        self.assertEqual(downgraded_validator.status, ValidationStatus.PARTIAL_VALID)
        self.assertIn("[exploiter downgrade]", downgraded_validator.analysis)
        self.assertIn("preconditions cannot be constructed", downgraded_validator.analysis)
        self.assertIn("seems valid", downgraded_validator.analysis)

    def test_no_candidate_at_all_returns_no_finding_placeholder(self) -> None:
        """All analyzer calls return no_issue → workflow returns the placeholder shape."""

        cfg = _config(max_findings_per_unit=3, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(status="no_issue"),
                AnalyzerResult(status="no_issue"),
            ]
        )
        validator = _ConstValidator()
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        per_finding, shared_state, checkpoint = workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        self.assertEqual(len(per_finding), 1)
        # Placeholder is_candidate=False.
        self.assertFalse(per_finding[0][3])
        self.assertEqual(per_finding[0][1], None)
        self.assertEqual(per_finding[0][2], None)
        self.assertEqual(checkpoint, "no_finding")
        # Validator and exploiter were never called.
        self.assertEqual(validator.calls, 0)
        self.assertEqual(exploitation.calls, 0)


    def test_validator_never_receives_exploitation_context(self) -> None:
        """Phase 1B stage reorder: validator runs BEFORE exploiter.

        Asserts the contract for `restructure-audit-modes-and-coverage`
        Phase 1 task 1.6.3 — the workflow MUST NOT pass exploitation
        context to the validator (in any mode), because the
        exploiter has not yet run.
        """

        cfg = _config(max_findings_per_unit=3, repo_root=self.repo_root)
        analyzer = _ScriptedAnalyzer(
            scripted=[
                AnalyzerResult(
                    status="candidate",
                    finding_name=f"F{i}",
                    suspect_function_id="fn::handle",
                    suspect_line=10 + i,
                )
                for i in range(3)
            ]
        )
        validator = _ConstValidator()
        exploitation = _ConstExploitation()
        logger = _ListLogger()
        workflow = _build_workflow(cfg, analyzer, validator, exploitation, logger)

        workflow._process_unit_single(
            unit=_unit(), path_functions=_path_functions(), path_context={}
        )

        # All validator invocations received `exploitation=None`.
        # Empty list also passes, but the test should run > 0
        # invocations to actually exercise the path.
        self.assertGreater(len(validator.received_exploitation), 0)
        for value in validator.received_exploitation:
            self.assertIsNone(
                value,
                f"validator received non-None exploitation: {value!r}",
            )


class MatchesAnyExcludedHelperTests(unittest.TestCase):
    def test_matches_on_exact_key(self) -> None:
        candidate = AnalyzerResult(
            status="candidate",
            finding_name="SQLi",
            suspect_function_id="fn::handle",
            suspect_line=10,
        )
        accepted = [
            (
                AnalyzerResult(
                    status="candidate",
                    finding_name="sqli",  # case-insensitive
                    suspect_function_id="fn::handle",
                    suspect_line=10,
                ),
                None,
                None,
                True,
            )
        ]
        self.assertTrue(_matches_any_excluded(candidate, accepted))

    def test_no_match_on_different_line(self) -> None:
        candidate = AnalyzerResult(
            status="candidate",
            finding_name="SQLi",
            suspect_function_id="fn::handle",
            suspect_line=10,
        )
        accepted = [
            (
                AnalyzerResult(
                    status="candidate",
                    finding_name="SQLi",
                    suspect_function_id="fn::handle",
                    suspect_line=42,
                ),
                None,
                None,
                True,
            )
        ]
        self.assertFalse(_matches_any_excluded(candidate, accepted))


if __name__ == "__main__":
    unittest.main()
