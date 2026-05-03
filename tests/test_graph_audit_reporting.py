from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.planner import plan_audit_paths
from tests._helpers import _TestOnlyGraphSource
from xauditor.audit.workflow import AuditWorkflow
from xauditor.audit.agents import (
    AnalyzerAgent,
    AnalyzerResult,
    ExploitationAgent,
    ExploitationResult,
    ValidationResult,
    ValidatorAgent,
)
from xauditor.config import Neo4jConfig, load_config
from xauditor.errors import GraphdbError
from xauditor.graph.builder import (
    GraphSynthesizerAgent,
    InventoryAgent,
    LangChainGraphBuilder,
    ReferenceTracerAgent,
)
from xauditor.graph.canonical import (
    CanonicalGraphTransformer,
    DiscoveredCall,
    DiscoveredClass,
    DiscoveredClassMember,
    DiscoveredFile,
    DiscoveredFunction,
    DiscoveredGraph,
)
from xauditor.graph.fingerprint import compute_build_fingerprint
from xauditor.graph.scope import module_name_for_path, resolve_repository_scope
from xauditor.integrations.lsp import LSPAvailability
from xauditor.integrations.neo4j import InMemoryNeo4jAdapter, Neo4jAdapter
from xauditor.llm import LLMClient
from xauditor.models import (
    AuditPlan,
    AuditUnit,
    ClassMemberRecord,
    ClassRecord,
    ConfidenceLevel,
    CoverageInventory,
    CoverageState,
    EdgeRecord,
    FileRecord,
    Finding,
    FunctionRecord,
    GraphProvenance,
    ModuleRecord,
    PathRecord,
    RepositoryScope,
    SourceReference,
    ValidationStatus,
)
# 0.9.0 ``consolidate-on-neo4j-source``: GraphBuildResult deleted.
# Tests in this file that exercised legacy GraphBuildResult /
# persist_graph / load_build paths are kept around as historical
# context but skipped — the new streaming canonical_finalize moves
# their coverage to the integration-test layer
# (``tests/integration/test_neo4j_e2e.py``, deferred to PR 4).
import pytest as _pytest

GraphBuildResult = None  # type: ignore[assignment]
_LEGACY_SKIP_REASON = (
    "0.9.0 consolidate-on-neo4j-source: GraphBuildResult / persist_graph / "
    "load_build removed; coverage moved to streaming canonical_finalize + "
    "Neo4j integration tests."
)
from xauditor.prompts import (
    ANALYZER_PROMPT,
    EXPLOITATION_PROMPT,
    GRAPH_INVENTORY_PROMPT,
    GRAPH_REFERENCE_TRACER_PROMPT,
    GRAPH_SYNTHESIZER_PROMPT,
    VALIDATOR_PROMPT,
)
from xauditor.reporting.markdown import (
    render_coverage_report,
    render_false_positives_report,
    render_findings_report,
)
from xauditor.services import ApplicationServices, InMemoryStateStore


SAMPLE_APP = textwrap.dedent(
    """
    import subprocess

    def run_ls(user_input):
        return subprocess.run(f"ls {user_input}", shell=True, capture_output=True)

    def helper(user_input):
        return run_ls(user_input)

    def handler_a(user_input):
        return helper(user_input)

    def handler_b(user_input):
        return helper(user_input)
    """
).strip() + "\n"


CLASS_APP = textwrap.dedent(
    """
    import subprocess

    class CommandRunner:
        DEFAULT_CMD = "ls"
        retries = 3

        def run(self, user_input):
            return subprocess.run(f"{self.DEFAULT_CMD} {user_input}", shell=True, capture_output=True)

        def handler(self, user_input):
            return self.run(user_input)
    """
).strip() + "\n"


class GraphAuditReportingTests(unittest.TestCase):
    class _RecordingLogger:
        def __init__(self) -> None:
            self.debug_messages: list[tuple[str, dict[str, object]]] = []

        def debug(self, _message: str) -> None:
            return None

        def debug_kv(self, message: str, **details: object) -> None:
            self.debug_messages.append((message, details))

        def info(self, _message: str) -> None:
            return None

        def warning(self, _message: str) -> None:
            return None

        def error(self, _message: str) -> None:
            return None

        def error_kv(self, _message: str, **_details: object) -> None:
            return None

    class _StubInventoryAgent:
        def __init__(self, files: tuple[DiscoveredFile, ...]) -> None:
            self.files = files
            self.calls = 0

        def run(self, *, repo_root: Path, scope, tools):
            del repo_root, scope, tools
            self.calls += 1
            return self.files, {"inventory_calls": self.calls}

    class _FailOnceReferenceTracerAgent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *, repo_root: Path, files: tuple[DiscoveredFile, ...], tools):
            del repo_root, files, tools
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("reference tracer interrupted")
            return (), {"reference_calls": self.calls}

    class _StubGraphSynthesizerAgent:
        def run(
            self,
            *,
            repo_root: Path,
            files: tuple[DiscoveredFile, ...],
            calls,
            lsp_messages: tuple[str, ...],
            lsp_availability,
        ) -> DiscoveredGraph:
            del repo_root, calls, lsp_availability
            return DiscoveredGraph(
                files=files,
                classes=(),
                class_members=(),
                functions=(
                    DiscoveredFunction(
                        function_id="app.py:handler_a:9",
                        name="handler_a",
                        qualified_name="handler_a",
                        file_path="app.py",
                        module_name="app",
                        start_line=9,
                        end_line=10,
                    ),
                ),
                calls=(),
                provenance=(
                    GraphProvenance(file_path="app.py", line_number=9, evidence="defined handler_a"),
                ),
                lsp_messages=lsp_messages,
            )

    class _NonCandidateAnalyzerAgent:
        def run(self, *, unit, path_functions, path_context=None):
            del unit, path_functions
            return AnalyzerResult(
                status="no_issue",
                reason="No dangerous sink was confirmed on this path.",
            )

    class _UnresolvedCandidateAnalyzerAgent:
        def run(self, *, unit, path_functions, path_context=None):
            del path_functions
            return AnalyzerResult(
                status="candidate",
                finding_name="Command Injection via shell=True",
                description=f"{unit.path.entry_function} reaches a dangerous sink.",
                reason="The sink is reachable from the current entry-point-specific path.",
                suspect_function_id="missing:function",
                suspect_line=42,
                evidence_strength="high",
            )

    class _StubExploitationAgent:
        def run(self, *, unit, analyzer, path_context=None):
            del unit, analyzer
            return ExploitationResult(status="ready", steps="Exploit details")

    class _StubValidatorAgent:
        def run(self, *, unit, analyzer, exploitation, path_context=None):
            del unit, analyzer, exploitation
            return ValidationResult(status=ValidationStatus.VALID, analysis="Validated")

    def test_canonical_transformer_normalizes_discovery_into_paths_and_build_coverage(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            scope = resolve_repository_scope(repo_root, ())
            discovered = DiscoveredGraph(
                files=(
                    DiscoveredFile(path="app.py", module_name="app", language="python"),
                ),
                classes=(
                    DiscoveredClass(
                        class_id="app.py:ShellRunner:1",
                        name="ShellRunner",
                        file_path="app.py",
                        module_name="app",
                        start_line=1,
                        end_line=4,
                        summary="ShellRunner encapsulates shell execution behavior.",
                        business_context="ShellRunner coordinates command execution.",
                    ),
                ),
                class_members=(
                    DiscoveredClassMember(
                        member_id="app.py:ShellRunner:command:2",
                        name="command",
                        class_id="app.py:ShellRunner:1",
                        file_path="app.py",
                        module_name="app",
                        line_number=2,
                    ),
                ),
                functions=(
                    DiscoveredFunction(
                        function_id="app.py:run_ls:3",
                        name="run_ls",
                        qualified_name="run_ls",
                        file_path="app.py",
                        module_name="app",
                        start_line=3,
                        end_line=4,
                    ),
                    DiscoveredFunction(
                        function_id="app.py:helper:6",
                        name="helper",
                        qualified_name="helper",
                        file_path="app.py",
                        module_name="app",
                        start_line=6,
                        end_line=7,
                    ),
                    DiscoveredFunction(
                        function_id="app.py:handler_a:9",
                        name="handler_a",
                        qualified_name="handler_a",
                        file_path="app.py",
                        module_name="app",
                        start_line=9,
                        end_line=10,
                    ),
                    DiscoveredFunction(
                        function_id="app.py:handler_b:12",
                        name="handler_b",
                        qualified_name="handler_b",
                        file_path="app.py",
                        module_name="app",
                        start_line=12,
                        end_line=13,
                    ),
                ),
                calls=(
                    DiscoveredCall(
                        caller_function_id="app.py:helper:6",
                        callee_name="run_ls",
                        file_path="app.py",
                        line_number=7,
                        evidence="run_ls(user_input)",
                    ),
                    DiscoveredCall(
                        caller_function_id="app.py:handler_a:9",
                        callee_name="helper",
                        file_path="app.py",
                        line_number=10,
                        evidence="helper(user_input)",
                    ),
                    DiscoveredCall(
                        caller_function_id="app.py:handler_b:12",
                        callee_name="helper",
                        file_path="app.py",
                        line_number=13,
                        evidence="helper(user_input)",
                    ),
                ),
                provenance=(
                    GraphProvenance(file_path="app.py", line_number=3, evidence="defined run_ls"),
                ),
            )
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )

            transformed = CanonicalGraphTransformer(config=config).transform(
                repo_root=repo_root,
                scope=scope,
                build_fingerprint="build-1",
                discovered=discovered,
            )

            self.assertEqual(len(transformed.modules), 1)
            self.assertEqual(len(transformed.classes), 1)
            self.assertEqual(len(transformed.class_members), 1)
            self.assertEqual(len(transformed.edges), 3)
            self.assertEqual(len(transformed.paths), 2)
            self.assertEqual(transformed.paths[0].entry_function, "handler_a")
            self.assertEqual(transformed.coverage.counts("path")[CoverageState.NOT_AUDITED], 2)
            self.assertEqual(transformed.coverage.counts("class")[CoverageState.NOT_AUDITED], 1)
            self.assertEqual(transformed.coverage.counts("function")[CoverageState.NOT_AUDITED], 4)
            self.assertEqual(transformed.class_enrichments[0].class_id, "app.py:ShellRunner:1")
            self.assertEqual(transformed.path_enrichments[0].path_fingerprint, transformed.paths[0].path_fingerprint)

    def test_fingerprint_changes_with_scope_and_workflow_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text("print('a')\n", encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            scope_all = resolve_repository_scope(repo_root, ())
            scope_excluded = resolve_repository_scope(repo_root, ("app.py",))

            first = compute_build_fingerprint(scope_all, config, workflow_version="graph-v1")
            second = compute_build_fingerprint(scope_excluded, config, workflow_version="graph-v1")
            third = compute_build_fingerprint(scope_all, config, workflow_version="graph-v2")

            self.assertNotEqual(first, second)
            self.assertNotEqual(first, third)

    def test_fingerprint_changes_with_enable_llm_enrichment_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text("print('a')\n", encoding="utf-8")
            base_env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            enriched = load_config(repo_root=repo_root, env=base_env, require_llm=True)
            disabled = load_config(
                repo_root=repo_root,
                env={**base_env, "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "false"},
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())

            fp_enriched = compute_build_fingerprint(scope, enriched, workflow_version="graph-v2")
            fp_disabled = compute_build_fingerprint(scope, disabled, workflow_version="graph-v2")

            self.assertTrue(enriched.graph.build.enable_llm_enrichment)
            self.assertFalse(disabled.graph.build.enable_llm_enrichment)
            self.assertNotEqual(fp_enriched, fp_disabled)

    def test_graph_builder_resumes_from_cached_inventory_stage(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
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
            scope = resolve_repository_scope(repo_root, ())
            files = (DiscoveredFile(path="app.py", module_name="app", language="python"),)
            inventory_agent = self._StubInventoryAgent(files)
            reference_agent = self._FailOnceReferenceTracerAgent()
            builder = LangChainGraphBuilder(
                config=config,
                llm_client=LLMClient.from_config(config.llm),
                inventory_agent=inventory_agent,
                reference_tracer_agent=reference_agent,
                graph_synthesizer_agent=self._StubGraphSynthesizerAgent(),
            )
            state_store = InMemoryStateStore()

            with self.assertRaises(RuntimeError):
                builder.build(
                    repo_root=repo_root,
                    scope=scope,
                    build_fingerprint="build-1",
                    state_store=state_store,
                )

            self.assertEqual(inventory_agent.calls, 1)

            graph = builder.build(
                repo_root=repo_root,
                scope=scope,
                build_fingerprint="build-1",
                state_store=state_store,
            )

            self.assertEqual(inventory_agent.calls, 1)
            self.assertEqual(reference_agent.calls, 2)
            self.assertEqual(graph.build_fingerprint, "build-1")

    def test_graph_builder_runs_multi_agent_tool_pipeline_and_lsp_augmentation(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        class SpyTools:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def ls(self, path: str = ".", ignore=None):
                self.calls.append("ls")
                return {"entries": ["app.py"], "tool": "ls"}

            def glob(self, pattern: str, path: str = "."):
                self.calls.append("glob")
                return {"matches": ["app.py"], "tool": "glob", "truncated": False}

            def read(self, file_path: str, offset: int = 0, limit: int = 200):
                self.calls.append("read")
                return {"tool": "read", "lines": ["1: import subprocess"], "truncated": False}

            def rg(self, pattern: str, path: str = ".", include: str | None = None):
                self.calls.append("rg")
                return {"tool": "rg", "matches": [{"path": "app.py", "line_number": 3, "line": "def run_ls(user_input):"}]}

            def grep(self, pattern: str, path: str = ".", include: str | None = None):
                self.calls.append("grep")
                return {"tool": "grep", "matches": [{"path": "app.py", "line_number": 4, "line": "shell=True"}]}

            def bash(self, command: str, *, workdir: str = ".", timeout: int = 5, description: str = ""):
                self.calls.append("bash")
                return {"tool": "bash", "stdout": "app.py", "stderr": "", "exit_code": 0}

            def codesearch(self, query: str, max_tokens: int = 400):
                self.calls.append("codesearch")
                return {"tool": "codesearch", "results": [{"title": "subprocess.run", "snippet": "shell executes"}]}

        class SpyLSP:
            def __init__(self) -> None:
                self.hover_calls: list[str] = []
                self.diagnostic_calls: list[str] = []

            def availability_for_paths(self, paths):
                return {
                    "python": LSPAvailability(
                        language="python",
                        binary="pylsp",
                        available=True,
                        install_hint="pip install python-lsp-server",
                    )
                }

            def hover(self, path: Path, symbol: str) -> str | None:
                self.hover_calls.append(symbol)
                return f"hover:{symbol}"

            def diagnostics(self, path: Path) -> list[str]:
                self.diagnostic_calls.append(path.name)
                return []

        with tempfile.TemporaryDirectory() as tmp:
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
            scope = resolve_repository_scope(repo_root, ())
            spy_tools = SpyTools()
            spy_lsp = SpyLSP()

            builder = LangChainGraphBuilder.from_config(
                config,
                lsp_registry=spy_lsp,
                tools_factory=lambda _repo_root: spy_tools,
            )
            graph = builder.build(repo_root=repo_root, scope=scope)

            self.assertEqual(set(spy_tools.calls), {"ls", "glob", "read", "rg", "grep", "bash", "codesearch"})
            self.assertIn("run_ls", spy_lsp.hover_calls)
            self.assertEqual(spy_lsp.diagnostic_calls, ["app.py"])
            self.assertEqual(len(graph.paths), 2)

    def test_graph_builder_extracts_classes_methods_class_members_and_enrichments(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())

            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=scope)

            self.assertEqual([item.name for item in graph.classes], ["CommandRunner"])
            self.assertEqual({item.name for item in graph.class_members}, {"DEFAULT_CMD", "retries"})
            self.assertEqual(
                {item.qualified_name for item in graph.functions},
                {"CommandRunner.handler", "CommandRunner.run"},
            )
            self.assertEqual(graph.paths[0].entry_function, "CommandRunner.handler")
            self.assertEqual(graph.paths[0].function_names, ("CommandRunner.handler", "CommandRunner.run"))
            self.assertEqual(graph.coverage.counts("class")[CoverageState.NOT_AUDITED], 1)
            self.assertEqual(graph.coverage.counts("class_member")[CoverageState.NOT_AUDITED], 2)
            self.assertEqual(graph.class_enrichments[0].summary.startswith("CommandRunner"), True)
            self.assertTrue(graph.path_enrichments[0].business_context)

    def test_graph_builder_groups_directory_files_into_one_module(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            graph_dir = repo_root / "src" / "xauditor" / "graph"
            graph_dir.mkdir(parents=True)
            (graph_dir / "builder.py").write_text("def build_graph():\n    return helper()\n", encoding="utf-8")
            (graph_dir / "canonical.py").write_text("def helper():\n    return 'ok'\n", encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )

            graph = LangChainGraphBuilder.from_config(config).build(
                repo_root=repo_root,
                scope=resolve_repository_scope(repo_root, ()),
            )

            self.assertEqual(
                {module.name for module in graph.modules},
                {"src.xauditor.graph"},
            )
            self.assertEqual(
                {file.module_name for file in graph.files},
                {"src.xauditor.graph"},
            )
            self.assertNotIn("src.xauditor.graph.builder", {module.name for module in graph.modules})

    def test_module_name_for_path_uses_parent_directory_for_nested_files_and_stem_for_root_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            nested_path = repo_root / "src" / "xauditor" / "graph" / "builder.py"
            root_path = repo_root / "app.py"

            self.assertEqual(module_name_for_path(repo_root, nested_path), "src.xauditor.graph")
            self.assertEqual(module_name_for_path(repo_root, root_path), "app")

    def test_graph_build_agents_use_repository_analysis_prompt_catalog(self) -> None:
        class StubTools:
            def ls(self, path: str = ".", ignore=None):
                del path, ignore
                return {"entries": ["app.py"], "tool": "ls"}

            def glob(self, pattern: str, path: str = "."):
                del pattern, path
                return {"matches": ["app.py"], "tool": "glob", "truncated": False}

            def read(self, file_path: str, offset: int = 0, limit: int = 200):
                del file_path, offset, limit
                return {"tool": "read", "lines": ["1: import subprocess"], "truncated": False}

            def rg(self, pattern: str, path: str = ".", include: str | None = None):
                del pattern, path, include
                return {"tool": "rg", "matches": []}

            def grep(self, pattern: str, path: str = ".", include: str | None = None):
                del pattern, path, include
                return {"tool": "grep", "matches": []}

            def bash(self, command: str, *, workdir: str = ".", timeout: int = 5, description: str = ""):
                del command, workdir, timeout, description
                return {"tool": "bash", "stdout": "app.py", "stderr": "", "exit_code": 0}

            def codesearch(self, query: str, max_tokens: int = 400):
                del query, max_tokens
                return {"tool": "codesearch", "results": []}

        seen_system_prompts: list[str] = []

        def fake_invoke_agent(**kwargs):
            seen_system_prompts.append(kwargs["system_prompt"])
            return kwargs["executor"](kwargs["payload"]), {
                "runtime": "langchain-executor",
                "agent_name": kwargs["agent_name"],
                "prompt_version": kwargs.get("prompt_version", "v1"),
            }

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            scope = resolve_repository_scope(repo_root, ())
            llm_client = LLMClient.from_config(
                load_config(
                    repo_root=repo_root,
                    env={
                        "XAUDITOR_LLM_BASE_URL": "mock://offline",
                        "XAUDITOR_LLM_API_KEY": "secret",
                        "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                    },
                    require_llm=True,
                ).llm
            )

            with patch("xauditor.graph.builder.invoke_agent", side_effect=fake_invoke_agent):
                tools = StubTools()
                files, _inventory_state = InventoryAgent().run(repo_root=repo_root, scope=scope, tools=tools)
                calls, _reference_state = ReferenceTracerAgent().run(repo_root=repo_root, files=files, tools=tools)
                discovered = GraphSynthesizerAgent(llm_client=llm_client, lsp_registry=object()).run(
                    repo_root=repo_root,
                    files=files,
                    calls=calls,
                    lsp_messages=(),
                    lsp_availability={},
                )

        self.assertTrue(discovered.functions)
        self.assertEqual(
            seen_system_prompts,
            [
                GRAPH_INVENTORY_PROMPT,
                GRAPH_REFERENCE_TRACER_PROMPT,
                GRAPH_SYNTHESIZER_PROMPT,
            ],
        )

    def test_audit_agents_use_psirt_prompt_catalog(self) -> None:
        unit = AuditUnit(
            path=PathRecord(
                entry_function="handler",
                function_names=("handler", "run_ls"),
                file_paths=("app.py", "app.py"),
                path_fingerprint="path-1",
                function_ids=("app.py:handler:1", "app.py:run_ls:3"),
            ),
            function_ids=("app.py:handler:1", "app.py:run_ls:3"),
        )
        path_functions = [
            FunctionRecord(
                function_id="app.py:handler:1",
                name="handler",
                qualified_name="handler",
                file_path="app.py",
                module_name="app",
                start_line=1,
                end_line=2,
                source="def handler(user_input):\n    return run_ls(user_input)",
            ),
            FunctionRecord(
                function_id="app.py:run_ls:3",
                name="run_ls",
                qualified_name="run_ls",
                file_path="app.py",
                module_name="app",
                start_line=3,
                end_line=4,
                source="def run_ls(user_input):\n    return subprocess.run(user_input, shell=True)",
            ),
        ]
        seen_system_prompts: list[str] = []

        def fake_invoke_agent(**kwargs):
            seen_system_prompts.append(kwargs["system_prompt"])
            if kwargs["agent_name"] == "analyzer":
                return AnalyzerResult(
                    status="candidate",
                    finding_name="Command Injection via shell=True",
                    description="Path reaches a dangerous sink.",
                    reason="Source-backed path evidence reaches subprocess.run with shell=True.",
                    suspect_function_id="app.py:run_ls:3",
                    suspect_line=4,
                    evidence_strength="high",
                ), {
                    "runtime": "shared-model",
                    "agent_name": kwargs["agent_name"],
                    "prompt_version": kwargs.get("prompt_version", "v1"),
                }
            if kwargs["agent_name"] == "exploitation":
                return ExploitationResult(status="ready", steps="Exploit details"), {
                    "runtime": "shared-model",
                    "agent_name": kwargs["agent_name"],
                    "prompt_version": kwargs.get("prompt_version", "v1"),
                }
            return ValidationResult(status=ValidationStatus.VALID, analysis="Validated"), {
                "runtime": "shared-model",
                "agent_name": kwargs["agent_name"],
                "prompt_version": kwargs.get("prompt_version", "v1"),
            }

        with patch("xauditor.audit.agents.invoke_agent", side_effect=fake_invoke_agent):
            analyzer = AnalyzerAgent().run(unit=unit, path_functions=path_functions)
            exploitation = ExploitationAgent().run(unit=unit, analyzer=analyzer)
            validator = ValidatorAgent().run(unit=unit, analyzer=analyzer, exploitation=exploitation)

        self.assertEqual(validator.status, ValidationStatus.VALID)
        self.assertEqual(
            seen_system_prompts,
            [
                ANALYZER_PROMPT,
                EXPLOITATION_PROMPT,
                VALIDATOR_PROMPT,
            ],
        )

    def test_audit_agents_log_prompt_versions_for_real_model_calls(self) -> None:
        class FakeChatModel:
            provider_name = "audit-specialist"
            model_name = "audit-model"
            is_mock = False

            def invoke_json(self, system: str, user: dict[str, object], *, user_text: str | None = None) -> dict[str, object]:
                del user, user_text
                if system == ANALYZER_PROMPT:
                    return {
                        "status": "candidate",
                        "finding_name": "Command Injection via shell=True",
                        "description": "Path reaches a dangerous sink.",
                        "reason": "Source-backed path evidence reaches subprocess.run with shell=True.",
                        "suspect_function_id": "app.py:run_ls:3",
                        "suspect_line": 4,
                        "evidence_strength": "high",
                    }
                if system == EXPLOITATION_PROMPT:
                    return {
                        "status": "ready",
                        "steps": "Exploit details",
                    }
                return {
                    "status": ValidationStatus.VALID.value,
                    "analysis": "Validated",
                }

            def invoke_text(self, system: str, user: dict[str, object], *, user_text: str | None = None) -> str:
                raise NotImplementedError

        unit = AuditUnit(
            path=PathRecord(
                entry_function="handler",
                function_names=("handler", "run_ls"),
                file_paths=("app.py", "app.py"),
                path_fingerprint="path-1",
                function_ids=("app.py:handler:1", "app.py:run_ls:3"),
            ),
            function_ids=("app.py:handler:1", "app.py:run_ls:3"),
        )
        path_functions = [
            FunctionRecord(
                function_id="app.py:run_ls:3",
                name="run_ls",
                qualified_name="run_ls",
                file_path="app.py",
                module_name="app",
                start_line=3,
                end_line=4,
                source="def run_ls(user_input):\n    return subprocess.run(user_input, shell=True)",
            ),
        ]
        logger = self._RecordingLogger()
        chat_model = FakeChatModel()

        analyzer = AnalyzerAgent(chat_model=chat_model, logger=logger).run(unit=unit, path_functions=path_functions)
        exploitation = ExploitationAgent(chat_model=chat_model, logger=logger).run(unit=unit, analyzer=analyzer)
        validator = ValidatorAgent(chat_model=chat_model, logger=logger).run(
            unit=unit,
            analyzer=analyzer,
            exploitation=exploitation,
        )

        self.assertEqual(validator.status, ValidationStatus.VALID)
        prompt_logs = {
            message: details
            for message, details in logger.debug_messages
            if message in {"Analyzer agent call", "Exploitation agent call", "Validator agent call"}
        }
        self.assertEqual(prompt_logs["Analyzer agent call"]["prompt_version"], "v3")
        self.assertEqual(prompt_logs["Exploitation agent call"]["prompt_version"], "v3")
        self.assertEqual(prompt_logs["Validator agent call"]["prompt_version"], "v3")
        self.assertEqual(prompt_logs["Analyzer agent call"]["provider"], "audit-specialist")
        self.assertEqual(prompt_logs["Validator agent call"]["runtime"], "shared-model")

    def test_graph_build_skips_persisted_enrichment_when_disabled_and_audit_falls_back(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "false",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )

            graph, _status = services.build_graph(excludes=(), force=False)

            self.assertEqual(graph.class_enrichments, ())
            self.assertEqual(graph.function_enrichments, ())
            self.assertEqual(graph.path_enrichments, ())
            self.assertTrue(all(not item.business_context for item in graph.classes))
            self.assertTrue(all(not item.business_context and not item.trust_boundary for item in graph.functions))
            self.assertTrue(all(not item.business_context and not item.trust_boundary for item in graph.paths))

            loaded = services.graph_repository.load_build(graph.build_fingerprint)
            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(loaded))
            self.assertTrue(all(not item.path.business_context and not item.path.trust_boundary for item in plan.audit_units))

            audit_run = AuditWorkflow.from_config(services.config).run(source=_TestOnlyGraphSource.from_graph_build_result(loaded), plan=plan)

            self.assertTrue(audit_run.shared_state)
            self.assertTrue(all(finding.business_context for finding in audit_run.findings))

    def test_neo4j_persistence_includes_paths_and_build_coverage_inventory(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        class RecordingDriver:
            def __init__(self) -> None:
                self.batches: list[tuple[tuple[str, ...], int | None]] = []

            def verify_connectivity(self) -> None:
                return None

            def run_batch(self, queries, *, timeout=None) -> None:
                self.batches.append((tuple(queries), timeout))

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=scope)
            driver = RecordingDriver()
            adapter = Neo4jAdapter(config=config.graphdb, driver=driver)

            adapter.bootstrap_schema()
            adapter.persist_graph(graph)

            persisted = "\n".join(query for batch, _timeout in driver.batches for query in batch)
            self.assertIn("Class", persisted)
            self.assertIn("ClassMember", persisted)
            self.assertIn("DECLARES_MEMBER", persisted)
            self.assertIn("DECLARES_METHOD", persisted)
            self.assertIn("Path", persisted)
            self.assertNotIn("CoverageRecord", persisted)
            self.assertNotIn("HAS_COVERAGE", persisted)
            self.assertIn("path_fingerprint", persisted)
            self.assertLess(len(driver.batches), 5)

    def test_neo4j_persist_graph_scopes_entities_by_build_and_orders_path_membership(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        class RecordingDriver:
            def __init__(self) -> None:
                self.batches: list[tuple[tuple[str, ...], int | None]] = []

            def verify_connectivity(self) -> None:
                return None

            def run_batch(self, queries, *, timeout=None) -> None:
                self.batches.append((tuple(queries), timeout))

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=scope)
            driver = RecordingDriver()
            adapter = Neo4jAdapter(config=config.graphdb, driver=driver)

            adapter.bootstrap_schema()
            adapter.persist_graph(graph)

            persisted = "\n".join(query for batch, _timeout in driver.batches for query in batch)
            self.assertIn("GraphBuild", persisted)
            self.assertIn("HAS_BUILD", persisted)
            self.assertIn("build_fingerprint", persisted)
            self.assertIn("position: 0", persisted)
            self.assertIn("position: 1", persisted)

    def test_neo4j_bootstrap_schema_migrates_legacy_unique_constraints_before_persisting_build_scoped_graph(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        class LegacyConstraintDriver:
            def __init__(self) -> None:
                self.batches: list[tuple[str, ...]] = []
                self.constraint_modes = {
                    "xauditor_function": "legacy",
                    "xauditor_file": "legacy",
                    "xauditor_module": "legacy",
                    "xauditor_class": "legacy",
                    "xauditor_class_member": "legacy",
                    "xauditor_path": "legacy",
                }

            def verify_connectivity(self) -> None:
                return None

            def run_batch(self, queries, *, timeout=None) -> None:
                del timeout
                self.batches.append(tuple(queries))
                for query in queries:
                    if query.startswith("DROP CONSTRAINT "):
                        name = query.removeprefix("DROP CONSTRAINT ").split(" ", 1)[0]
                        self.constraint_modes.pop(name, None)
                        continue
                    if query.startswith("CREATE CONSTRAINT "):
                        name = query.removeprefix("CREATE CONSTRAINT ").split(" ", 1)[0]
                        self.constraint_modes.setdefault(name, "current")
                        continue
                    if "MERGE (m:Module {module_key:" in query and self.constraint_modes.get("xauditor_module") == "legacy":
                        raise GraphdbError(
                            "Failed to execute batched cypher-shell statements inside the managed Neo4j container. "
                            "Node(2) already exists with label `Module` and property `name` = 'src.xauditor.__init__'"
                        )
                    if "MERGE (f:File {file_key:" in query and self.constraint_modes.get("xauditor_file") == "legacy":
                        raise GraphdbError("legacy file uniqueness still active")
                    if "MERGE (c:Class {class_key:" in query and self.constraint_modes.get("xauditor_class") == "legacy":
                        raise GraphdbError("legacy class uniqueness still active")
                    if (
                        "MERGE (m:ClassMember {class_member_key:" in query
                        and self.constraint_modes.get("xauditor_class_member") == "legacy"
                    ):
                        raise GraphdbError("legacy class member uniqueness still active")
                    if (
                        "MERGE (fn:Function {function_key:" in query
                        and self.constraint_modes.get("xauditor_function") == "legacy"
                    ):
                        raise GraphdbError("legacy function uniqueness still active")
                    if "MERGE (p:Path {path_key:" in query and self.constraint_modes.get("xauditor_path") == "legacy":
                        raise GraphdbError("legacy path uniqueness still active")

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            graph = GraphBuildResult(
                repo_root=repo_root,
                build_fingerprint="build-legacy-compat",
                scope=RepositoryScope(
                    repo_root=repo_root,
                    excludes=(),
                    included_files=(
                        repo_root / "src" / "xauditor" / "__init__.py",
                        repo_root / "src" / "xauditor" / "app.py",
                    ),
                    excluded_files=(),
                ),
                modules=(
                    ModuleRecord(
                        name="src.xauditor.__init__",
                        file_paths=("src/xauditor/__init__.py",),
                    ),
                    ModuleRecord(
                        name="src.xauditor.app",
                        file_paths=("src/xauditor/app.py",),
                    ),
                ),
                files=(
                    FileRecord(
                        path="src/xauditor/__init__.py",
                        module_name="src.xauditor.__init__",
                        language="python",
                        content="",
                    ),
                    FileRecord(
                        path="src/xauditor/app.py",
                        module_name="src.xauditor.app",
                        language="python",
                        content=CLASS_APP,
                    ),
                ),
                classes=(
                    ClassRecord(
                        class_id="src/xauditor/app.py:CommandRunner:3",
                        name="CommandRunner",
                        file_path="src/xauditor/app.py",
                        module_name="src.xauditor.app",
                        start_line=3,
                        end_line=10,
                    ),
                ),
                class_members=(
                    ClassMemberRecord(
                        member_id="src/xauditor/app.py:CommandRunner:DEFAULT_CMD:4",
                        name="DEFAULT_CMD",
                        class_id="src/xauditor/app.py:CommandRunner:3",
                        file_path="src/xauditor/app.py",
                        module_name="src.xauditor.app",
                        line_number=4,
                        source='DEFAULT_CMD = "ls"',
                    ),
                ),
                functions=(
                    FunctionRecord(
                        function_id="src/xauditor/app.py:CommandRunner.run:7",
                        name="run",
                        qualified_name="CommandRunner.run",
                        file_path="src/xauditor/app.py",
                        module_name="src.xauditor.app",
                        start_line=7,
                        end_line=8,
                        source="def run(self, user_input): pass",
                        class_id="src/xauditor/app.py:CommandRunner:3",
                    ),
                    FunctionRecord(
                        function_id="src/xauditor/app.py:CommandRunner.handler:10",
                        name="handler",
                        qualified_name="CommandRunner.handler",
                        file_path="src/xauditor/app.py",
                        module_name="src.xauditor.app",
                        start_line=10,
                        end_line=11,
                        source="def handler(self, user_input): return self.run(user_input)",
                        class_id="src/xauditor/app.py:CommandRunner:3",
                    ),
                ),
                paths=(
                    PathRecord(
                        entry_function="CommandRunner.handler",
                        function_names=("handler", "run"),
                        file_paths=("src/xauditor/app.py",),
                        path_fingerprint="src/xauditor/app.py:handler->run",
                        function_ids=(
                            "src/xauditor/app.py:CommandRunner.handler:10",
                            "src/xauditor/app.py:CommandRunner.run:7",
                        ),
                    ),
                ),
                edges=(),
                provenance=(),
                coverage=CoverageInventory(),
            )
            driver = LegacyConstraintDriver()
            adapter = Neo4jAdapter(config=Neo4jConfig(), driver=driver)

            with patch("xauditor.resilience.time.sleep", return_value=None):
                adapter.bootstrap_schema()
                adapter.persist_graph(graph)

            bootstrapped = "\n".join(query for batch in driver.batches for query in batch)
            self.assertIn("DROP CONSTRAINT xauditor_module IF EXISTS;", bootstrapped)
            self.assertIn("DROP CONSTRAINT xauditor_file IF EXISTS;", bootstrapped)
            self.assertIn("DROP CONSTRAINT xauditor_function IF EXISTS;", bootstrapped)
            self.assertIn("DROP CONSTRAINT xauditor_class IF EXISTS;", bootstrapped)
            self.assertIn("DROP CONSTRAINT xauditor_class_member IF EXISTS;", bootstrapped)
            self.assertIn("DROP CONSTRAINT xauditor_path IF EXISTS;", bootstrapped)

    def test_neo4j_bootstrap_schema_awaits_indexes_before_returning(self) -> None:
        class RecordingDriver:
            def __init__(self) -> None:
                self.batches: list[tuple[str, ...]] = []

            def verify_connectivity(self) -> None:
                return None

            def run_batch(self, queries, *, timeout=None) -> None:
                del timeout
                self.batches.append(tuple(queries))

        driver = RecordingDriver()
        adapter = Neo4jAdapter(config=Neo4jConfig(), driver=driver)
        adapter.bootstrap_schema()

        flat = [query for batch in driver.batches for query in batch]
        self.assertIn(
            "CALL db.awaitIndexes();",
            flat,
            "bootstrap_schema must await index population so persist_graph never "
            "races the Neo4j 'index is still populating' error",
        )
        await_idx = flat.index("CALL db.awaitIndexes();")
        create_idx = [
            i for i, q in enumerate(flat) if q.startswith("CREATE CONSTRAINT ")
        ]
        self.assertTrue(create_idx, "at least one CREATE CONSTRAINT must exist")
        self.assertGreater(
            await_idx,
            max(create_idx),
            "awaitIndexes must run after every CREATE CONSTRAINT",
        )

    def test_in_memory_neo4j_graph_repository_round_trips_build_and_isolates_versions(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        from xauditor.integrations.neo4j_repository import InMemoryNeo4jGraphRepository

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            adapter = InMemoryNeo4jAdapter()
            repository = InMemoryNeo4jGraphRepository(adapter)

            graph_a = LangChainGraphBuilder.from_config(config).build(
                repo_root=repo_root,
                scope=resolve_repository_scope(repo_root, ()),
                build_fingerprint="build-a",
            )
            graph_b = LangChainGraphBuilder.from_config(config).build(
                repo_root=repo_root,
                scope=resolve_repository_scope(repo_root, ()),
                build_fingerprint="build-b",
            )

            adapter.persist_graph(graph_a)
            adapter.persist_graph(graph_b)

            self.assertEqual(repository.get_latest_build(repo_root), "build-b")
            loaded = repository.load_build("build-a")
            self.assertEqual(loaded.build_fingerprint, "build-a")
            self.assertEqual(loaded.paths[0].function_ids, graph_a.paths[0].function_ids)

    def test_neo4j_persist_audit_run_links_build_and_path(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        class RecordingDriver:
            def __init__(self) -> None:
                self.batches: list[tuple[tuple[str, ...], int | None]] = []

            def verify_connectivity(self) -> None:
                return None

            def run_batch(self, queries, *, timeout=None) -> None:
                self.batches.append((tuple(queries), timeout))

        with tempfile.TemporaryDirectory() as tmp:
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
            graph = LangChainGraphBuilder.from_config(config).build(
                repo_root=repo_root,
                scope=resolve_repository_scope(repo_root, ()),
            )
            audit_run = AuditWorkflow.from_config(config).run(source=_TestOnlyGraphSource.from_graph_build_result(graph), plan=plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(graph)))
            driver = RecordingDriver()
            adapter = Neo4jAdapter(config=config.graphdb, driver=driver)

            adapter.persist_graph(graph)
            adapter.persist_audit_run(audit_run)

            persisted = "\n".join(query for batch, _timeout in driver.batches for query in batch)
            self.assertIn("FOR_BUILD", persisted)
            self.assertIn("ON_PATH", persisted)

    def test_graph_builder_path_planner_and_audit_workflow_respect_entry_points(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
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
            scope = resolve_repository_scope(repo_root, ())
            builder = LangChainGraphBuilder.from_config(config)
            graph = builder.build(repo_root=repo_root, scope=scope)

            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(graph))

            fingerprints = {item.path.path_fingerprint for item in plan.audit_units}
            entry_points = {item.path.entry_function for item in plan.audit_units}

            self.assertEqual(len(plan.audit_units), 2)
            self.assertEqual(len(fingerprints), 2)
            self.assertEqual(entry_points, {"handler_a", "handler_b"})

            workflow = AuditWorkflow.from_config(config)
            audit_run = workflow.run(source=_TestOnlyGraphSource.from_graph_build_result(graph), plan=plan)

            self.assertEqual(len(audit_run.findings), 2)
            self.assertTrue(
                all(finding.validation_status == ValidationStatus.VALID for finding in audit_run.findings)
            )
            self.assertTrue(
                all(finding.confidence_level == ConfidenceLevel.HIGH for finding in audit_run.findings)
            )
            self.assertTrue(audit_run.shared_state)
            sample_state = next(iter(audit_run.shared_state.values()))
            self.assertIn("analyzer", sample_state)
            self.assertIn("exploitation", sample_state)
            self.assertIn("validator", sample_state)

    def test_audit_workflow_logs_when_analyzer_result_is_not_a_candidate(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
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
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=resolve_repository_scope(repo_root, ()))
            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(graph))
            workflow = AuditWorkflow(
                config=config,
                llm_client=LLMClient.from_config(config.llm),
                analyzer_agent=self._NonCandidateAnalyzerAgent(),
                exploitation_agent=self._StubExploitationAgent(),
                validator_agent=self._StubValidatorAgent(),
                logger=self._RecordingLogger(),
            )

            audit_run = workflow.run(source=_TestOnlyGraphSource.from_graph_build_result(graph), plan=AuditPlan(audit_units=(plan.audit_units[0],)))

            self.assertEqual(audit_run.findings, ())
            self.assertIn(
                "Downstream agents skipped",
                [message for message, _details in workflow.logger.debug_messages],
            )
            skip_entry = next(
                details
                for message, details in workflow.logger.debug_messages
                if message == "Downstream agents skipped"
            )
            self.assertIn("analyzer reported no finding", str(skip_entry.get("reason")))

    def test_audit_workflow_logs_when_candidate_suspect_function_cannot_be_resolved(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
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
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=resolve_repository_scope(repo_root, ()))
            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(graph))
            logger = self._RecordingLogger()
            workflow = AuditWorkflow(
                config=config,
                llm_client=LLMClient.from_config(config.llm),
                analyzer_agent=self._UnresolvedCandidateAnalyzerAgent(),
                exploitation_agent=self._StubExploitationAgent(),
                validator_agent=self._StubValidatorAgent(),
                logger=logger,
            )

            audit_run = workflow.run(source=_TestOnlyGraphSource.from_graph_build_result(graph), plan=AuditPlan(audit_units=(plan.audit_units[0],)))

            self.assertEqual(len(audit_run.findings), 1)
            messages = [message for message, _details in logger.debug_messages]
            self.assertIn(
                "Suspect function unresolved; emitting finding without suspect annotation",
                messages,
            )

    def _make_finding(
        self,
        *,
        validation_status: ValidationStatus = ValidationStatus.VALID,
        referenced_symbols: tuple[dict[str, object], ...] = (),
    ) -> Finding:
        return Finding(
            finding_id="F-0001",
            finding_name="Example",
            finding_description="desc",
            confidence_level=ConfidenceLevel.HIGH,
            source_references=(
                SourceReference(
                    file_path="app.py",
                    start_line=1,
                    end_line=2,
                    focus_lines=(),
                    language="python",
                    snippet="pass",
                ),
            ),
            analysis="a",
            reason="r",
            context="Call stack: entry",
            business_context="biz",
            exploitation_status="exploitable",
            exploitation_steps="run it",
            validation_status=validation_status,
            validation_analysis="looks valid",
            path_fingerprint="fp-123",
            referenced_symbols=referenced_symbols,
        )

    def test_findings_report_renders_referenced_symbols_section(self) -> None:
        referenced_symbols = (
            {
                "symbol_id": "sym-1",
                "name": "API_TOKEN",
                "kind": "constant",
                "module_name": "app.config",
                "file_path": "app/config.py",
                "start_line": 10,
                "end_line": 10,
                "type_annotation": "Final[str]",
                "value_repr": "<placeholder:string:length=4096>",
                "is_placeholder": True,
                "used_by": (
                    {"function_id": "fn-1", "line_number": 42, "evidence": "token = API_TOKEN"},
                ),
            },
        )
        finding = self._make_finding(referenced_symbols=referenced_symbols)
        report = render_findings_report([finding])

        self.assertIn("### Referenced Symbols", report)
        self.assertIn("#### `API_TOKEN`", report)
        self.assertIn("kind=constant", report)
        self.assertIn("module=app.config", report)
        self.assertIn("app/config.py:10-10", report)
        self.assertIn("- Type: `Final[str]`", report)
        self.assertIn("- Value (placeholder):", report)
        self.assertIn("```python", report)
        self.assertIn("<placeholder:string:length=4096>", report)
        self.assertIn("- Used by:", report)
        self.assertIn("`fn-1` @ line 42", report)
        self.assertIn("token = API_TOKEN", report)

    def test_findings_report_omits_referenced_symbols_heading_when_empty(self) -> None:
        finding = self._make_finding(referenced_symbols=())
        report = render_findings_report([finding])
        self.assertNotIn("### Referenced Symbols", report)

    def test_false_positives_report_renders_referenced_symbols(self) -> None:
        referenced_symbols = (
            {
                "name": "CONFIG",
                "kind": "global",
                "module_name": "app",
                "file_path": "app/__init__.py",
                "start_line": 1,
                "end_line": 1,
                "value_repr": "{}",
                "used_by": (
                    {"function_id": "fn-x", "line_number": 5, "evidence": "CONFIG.update(x)"},
                ),
            },
        )
        finding = self._make_finding(
            validation_status=ValidationStatus.FALSE_POSITIVE,
            referenced_symbols=referenced_symbols,
        )
        report = render_false_positives_report([finding])
        self.assertIn("### Referenced Symbols", report)
        self.assertIn("#### `CONFIG`", report)
        self.assertIn("CONFIG.update(x)", report)

    def test_markdown_reports_include_required_sections_and_coverage_percentages(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            app_path = repo_root / "app.py"
            app_path.write_text(SAMPLE_APP, encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=scope)
            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(graph))
            audit_run = AuditWorkflow.from_config(config).run(source=_TestOnlyGraphSource.from_graph_build_result(graph), plan=plan)

            findings_report = render_findings_report(audit_run.findings)
            coverage_report = render_coverage_report(audit_run.coverage)

            self.assertIn("**Finding Id**", findings_report)
            self.assertIn("**Confidence Level**", findings_report)
            self.assertIn("**Source code references**", findings_report)
            self.assertIn(str(app_path), findings_report)
            self.assertIn("```python", findings_report)
            self.assertIn("<====", findings_report)
            self.assertIn("Coverage Report", coverage_report)
            self.assertIn("Modules: 100.00%", coverage_report)
            self.assertIn("Functions: 100.00%", coverage_report)

    def test_path_planner_suppresses_duplicate_paths_within_one_entry_point(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
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
            scope = resolve_repository_scope(repo_root, ())
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=scope)
            duplicated_graph = type(graph)(
                repo_root=graph.repo_root,
                build_fingerprint=graph.build_fingerprint,
                scope=graph.scope,
                modules=graph.modules,
                files=graph.files,
                classes=graph.classes,
                class_members=graph.class_members,
                functions=graph.functions,
                paths=graph.paths,
                edges=graph.edges
                + (
                    EdgeRecord(
                        source_function=graph.edges[0].source_function,
                        target_function=graph.edges[0].target_function,
                        edge_type=graph.edges[0].edge_type,
                        provenance=GraphProvenance(
                            file_path=graph.edges[0].provenance.file_path,
                            line_number=graph.edges[0].provenance.line_number,
                            evidence=graph.edges[0].provenance.evidence,
                        ),
                        confidence=graph.edges[0].confidence,
                    ),
                ),
                provenance=graph.provenance,
                coverage=graph.coverage,
                class_enrichments=graph.class_enrichments,
                function_enrichments=graph.function_enrichments,
                path_enrichments=graph.path_enrichments,
                lsp_messages=graph.lsp_messages,
                discovery_shared_state=graph.discovery_shared_state,
            )

            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(duplicated_graph))

            self.assertEqual(len(plan.audit_units), 2)

    def test_audit_workflow_assigns_low_confidence_for_false_positive_validation(self) -> None:
        _pytest.skip(_LEGACY_SKIP_REASON)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(
                textwrap.dedent(
                    """
                    def run_safe(user_input):
                        return f"echo {user_input}"

                    def handler(user_input):
                        return run_safe(user_input)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                },
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())
            graph = LangChainGraphBuilder.from_config(config).build(repo_root=repo_root, scope=scope)
            plan = plan_audit_paths(_TestOnlyGraphSource.from_graph_build_result(graph))
            workflow = AuditWorkflow.from_config(config)

            audit_run = workflow.run(source=_TestOnlyGraphSource.from_graph_build_result(graph), plan=plan)

            self.assertEqual(audit_run.findings, ())
            self.assertTrue(audit_run.shared_state)
            shared = next(iter(audit_run.shared_state.values()))
            self.assertEqual(shared["analyzer"]["status"], "no_issue")
            self.assertEqual(shared["validator"]["status"], "skipped")
            self.assertEqual(shared["exploitation"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
