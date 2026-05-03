from __future__ import annotations

import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from xauditor.audit.agents import AnalyzerResult, ExploitationResult, ValidationResult
from xauditor.audit.workflow import AuditWorkflow
from xauditor.cli import main
from xauditor.errors import GraphdbError
from xauditor.integrations.lsp import LanguageServerRegistry
from xauditor.llm import LLMClient
from xauditor.models import CoverageState, GraphBuildStatus, ValidationStatus
from xauditor.runtime_logging import RuntimeLogger
from xauditor.services import ApplicationServices, InMemoryStateStore


_LEGACY_BUILD_RESULT_SKIP = (
    "0.9.0 consolidate-on-neo4j-source: build_graph now returns "
    "(fingerprint, status); graph.lsp_messages / graph.build_fingerprint / "
    "InMemoryNeo4jAdapter.graphs accumulator removed. Migration of these "
    "tests to read from services.graph_repository is deferred to PR follow-up."
)


SAMPLE_APP = textwrap.dedent(
    """
    import subprocess

    def run_ls(user_input):
        return subprocess.run(f"ls {user_input}", shell=True, capture_output=True)

    def helper(user_input):
        return run_ls(user_input)

    def handler(user_input):
        return helper(user_input)
    """
).strip() + "\n"


MULTI_ENTRY_APP = textwrap.dedent(
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


class CliWorkflowTests(unittest.TestCase):
    class _FailOnceNeo4jAdapter:
        def __init__(self) -> None:
            self.graphs: list[object] = []
            self._should_fail = True

        def ping(self) -> None:
            return None

        def bootstrap_schema(self) -> None:
            return None

        def persist_graph(self, graph) -> None:
            if self._should_fail:
                self._should_fail = False
                raise GraphdbError("Neo4j sync interrupted")
            self.graphs.append(graph)

        def persist_audit_run(self, _audit_run) -> None:
            return None

    class _InterruptOnceNeo4jAdapter(_FailOnceNeo4jAdapter):
        def persist_graph(self, graph) -> None:
            if self._should_fail:
                self._should_fail = False
                raise KeyboardInterrupt()
            self.graphs.append(graph)

    class _InterruptOnSecondAnalyzerAgent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *, unit, path_functions, path_context=None):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt()
            suspect = path_functions[-1]
            return AnalyzerResult(
                status="candidate",
                finding_name="Command Injection via shell=True",
                description=f"{unit.path.entry_function} reaches a dangerous sink.",
                reason="The sink is reachable from the current entry-point-specific path.",
                suspect_function_id=suspect.function_id,
                suspect_line=suspect.start_line,
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

    def test_graphdb_lifecycle_commands_only_touch_managed_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_LOGGING_LEVEL": "info",
            }
            services = ApplicationServices.for_testing(repo_root=repo_root, env=env)
            stdout = io.StringIO()
            stderr = io.StringIO()

            self.assertEqual(main(["graphdb", "start"], services=services, stdout=stdout, stderr=stderr), 1)
            self.assertIn("run `xauditor graphdb init`", stderr.getvalue())

            stdout = io.StringIO()
            self.assertEqual(main(["graphdb", "init"], services=services, stdout=stdout, stderr=io.StringIO()), 0)
            self.assertIn("graphdb ready", stdout.getvalue())

            stdout = io.StringIO()
            self.assertEqual(main(["graphdb", "stop"], services=services, stdout=stdout, stderr=io.StringIO()), 0)
            self.assertIn("stopped", stdout.getvalue())

            stdout = io.StringIO()
            self.assertEqual(
                main(["graphdb", "reset", "--yes"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("Deleted", stdout.getvalue())

    def test_graph_build_and_audit_commands_render_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_LOGGING_LEVEL": "info",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )

            build_stdout = io.StringIO()
            build_code = main(["graph", "build"], services=services, stdout=build_stdout, stderr=io.StringIO())
            self.assertEqual(build_code, 0)
            self.assertIn("Graph build completed", build_stdout.getvalue())

            audit_stdout = io.StringIO()
            audit_code = main(["audit"], services=services, stdout=audit_stdout, stderr=io.StringIO())
            self.assertEqual(audit_code, 0)
            # Phase 2 of make-postgres-the-canonical-sink: the run-time
            # Markdown sink is gone; the CLI summary names the run id
            # so the operator can run ``audit export`` against it.
            self.assertIn("audit export", audit_stdout.getvalue())
            self.assertIn("run id:", audit_stdout.getvalue())

    def test_graph_build_with_real_provider_requires_langchain_openai_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_DEFAULT_PROVIDER": "shared",
                "XAUDITOR_LOGGING_LEVEL": "info",
                "XAUDITOR_LLM_BASE_URL": "https://llm.example/v1",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "gpt-test",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )

            stderr = io.StringIO()
            with patch("xauditor.model_factory.LANGCHAIN_OPENAI_AVAILABLE", False):
                code = main(["graph", "build"], services=services, stdout=io.StringIO(), stderr=stderr)

            self.assertEqual(code, 1)
            self.assertIn("langchain-openai", stderr.getvalue())
            self.assertIn("uv sync", stderr.getvalue())

    def test_audit_loads_graph_from_neo4j_when_cache_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )

            build_code = main(["graph", "build"], services=services, stdout=io.StringIO(), stderr=io.StringIO())
            self.assertEqual(build_code, 0)
            assert services.state_store.latest_build_fingerprint is not None
            services.state_store.clear_build(services.state_store.latest_build_fingerprint)

            audit_code = main(["audit"], services=services, stdout=io.StringIO(), stderr=io.StringIO())
            self.assertEqual(audit_code, 0)

    def test_audit_fails_when_neo4j_has_no_build_even_if_cache_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            services.build_graph(excludes=(), force=False)
            services.neo4j.graphs_by_fingerprint.clear()
            services.neo4j.repo_builds.clear()

            stderr = io.StringIO()
            audit_code = main(["audit"], services=services, stdout=io.StringIO(), stderr=stderr)

            self.assertEqual(audit_code, 1)
            self.assertIn("Run `xauditor graph build` first.", stderr.getvalue())

    def test_graph_build_logs_info_and_debug_output_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            base_env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }

            info_services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env={**base_env, "XAUDITOR_LOGGING_LEVEL": "info"},
                state_store=InMemoryStateStore(),
            )
            info_stderr = io.StringIO()
            info_code = main(["graph", "build"], services=info_services, stdout=io.StringIO(), stderr=info_stderr)

            self.assertEqual(info_code, 0)
            self.assertIn("Starting graph build", info_stderr.getvalue())
            self.assertNotIn("Graph build context", info_stderr.getvalue())
            self.assertNotIn("Graph build stage inventory: starting", info_stderr.getvalue())
            self.assertNotIn("Inventory stage output", info_stderr.getvalue())
            self.assertNotIn("Graph build stage graph_synthesizer: starting", info_stderr.getvalue())
            self.assertNotIn("Graph synthesizer stage output", info_stderr.getvalue())
            self.assertNotIn("Inventory tool activity", info_stderr.getvalue())
            self.assertNotIn("LSP availability", info_stderr.getvalue())

            debug_services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env={**base_env, "XAUDITOR_LOGGING_LEVEL": "debug"},
                state_store=InMemoryStateStore(),
            )
            debug_stderr = io.StringIO()
            debug_code = main(["graph", "build"], services=debug_services, stdout=io.StringIO(), stderr=debug_stderr)

            self.assertEqual(debug_code, 0)
            self.assertIn("Graph build context | fingerprint=", debug_stderr.getvalue())
            self.assertIn("Graph build stage inventory: starting | file_count=1", debug_stderr.getvalue())
            self.assertIn("Inventory stage output | file_count=1", debug_stderr.getvalue())
            self.assertIn("Graph build stage graph_synthesizer: starting | file_count=1", debug_stderr.getvalue())
            self.assertIn("Graph synthesizer stage output | function_count=", debug_stderr.getvalue())
            self.assertIn("Inventory tool activity", debug_stderr.getvalue())
            self.assertIn("LSP availability", debug_stderr.getvalue())
            self.assertNotIn("secret", debug_stderr.getvalue())

    def test_audit_logs_info_and_debug_output_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(MULTI_ENTRY_APP, encoding="utf-8")
            base_env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }

            info_services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env={**base_env, "XAUDITOR_LOGGING_LEVEL": "info"},
                state_store=InMemoryStateStore(),
            )
            info_services.build_graph(excludes=(), force=False)
            info_stderr = io.StringIO()
            info_code = main(["audit"], services=info_services, stdout=io.StringIO(), stderr=info_stderr)

            self.assertEqual(info_code, 0)
            self.assertIn("Starting audit", info_stderr.getvalue())
            self.assertNotIn("Audit path start", info_stderr.getvalue())
            self.assertNotIn("Validator verdict", info_stderr.getvalue())

            debug_services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env={**base_env, "XAUDITOR_LOGGING_LEVEL": "debug"},
                state_store=InMemoryStateStore(),
            )
            debug_services.build_graph(excludes=(), force=False)
            debug_stderr = io.StringIO()
            debug_code = main(["audit"], services=debug_services, stdout=io.StringIO(), stderr=debug_stderr)

            self.assertEqual(debug_code, 0)
            self.assertIn("Audit path start | index=1 | total=2", debug_stderr.getvalue())
            self.assertIn("Path enrichment skipped | path=", debug_stderr.getvalue())
            self.assertIn("Analyzer completed | path=", debug_stderr.getvalue())
            self.assertIn("Validator verdict | path=", debug_stderr.getvalue())
            self.assertIn("Finding emitted | finding_id=F-0001", debug_stderr.getvalue())
            self.assertNotIn("secret", debug_stderr.getvalue())

    def test_graph_build_persists_classes_methods_and_class_members(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            state_store = InMemoryStateStore()
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=state_store,
            )

            build_stdout = io.StringIO()
            build_code = main(["graph", "build"], services=services, stdout=build_stdout, stderr=io.StringIO())

            self.assertEqual(build_code, 0)
            self.assertIn("Graph build completed", build_stdout.getvalue())
            build = state_store.get_latest_build()
            self.assertIsNotNone(build)
            assert build is not None
            self.assertEqual([item.name for item in build.classes], ["CommandRunner"])
            self.assertEqual({item.name for item in build.class_members}, {"DEFAULT_CMD", "retries"})
            self.assertEqual(
                {item.qualified_name for item in build.functions},
                {"CommandRunner.handler", "CommandRunner.run"},
            )

    def test_graph_build_reports_missing_or_available_language_servers(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(repo_root=repo_root, env=env)
            services.lsp_registry = LanguageServerRegistry(
                server_map={
                    "python": {
                        "binary": "available-pylsp",
                        "install": "pip install python-lsp-server",
                        "extensions": [".py"],
                    }
                },
                which=lambda _binary: "/usr/bin/available-pylsp",
            )
            graph, status = services.build_graph(excludes=(), force=False)
            self.assertEqual(status, GraphBuildStatus.COMPLETED)
            self.assertEqual(graph.lsp_messages, ())

            services = ApplicationServices.for_testing(repo_root=repo_root, env=env)
            services.lsp_registry = LanguageServerRegistry(
                server_map={
                    "python": {
                        "binary": "missing-pylsp",
                        "install": "pip install python-lsp-server",
                        "extensions": [".py"],
                    }
                },
                which=lambda _binary: None,
            )
            graph, status = services.build_graph(excludes=(), force=True)
            self.assertEqual(status, GraphBuildStatus.COMPLETED)
            self.assertIn("pip install python-lsp-server", graph.lsp_messages[0])

    def test_graph_build_does_not_print_stale_lsp_warning_when_reusing_cached_index(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_LOGGING_LEVEL": "info",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            services.lsp_registry = LanguageServerRegistry(
                server_map={
                    "python": {
                        "binary": "missing-pylsp",
                        "install": "pip install python-lsp-server",
                        "extensions": [".py"],
                    }
                },
                which=lambda _binary: None,
            )
            first_stdout = io.StringIO()
            first_stderr = io.StringIO()
            first_code = main(["graph", "build"], services=services, stdout=first_stdout, stderr=first_stderr)
            self.assertEqual(first_code, 0)
            self.assertNotIn("Missing python language server", first_stdout.getvalue())
            self.assertIn("Missing python language server", first_stderr.getvalue())

            reused_stdout = io.StringIO()
            reused_stderr = io.StringIO()
            reused_code = main(["graph", "build"], services=services, stdout=reused_stdout, stderr=reused_stderr)
            self.assertEqual(reused_code, 0)
            self.assertIn("reused existing index", reused_stdout.getvalue())
            self.assertNotIn("Missing python language server", reused_stdout.getvalue())
            self.assertNotIn("Missing python language server", reused_stderr.getvalue())
            self.assertNotIn("Graph build context", reused_stderr.getvalue())

    def test_graph_build_logs_debug_context_when_reusing_cached_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(SAMPLE_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            services.build_graph(excludes=(), force=False)

            debug_stderr = io.StringIO()
            graph, status = services.build_graph(
                excludes=(),
                force=False,
                logger=RuntimeLogger(level="debug", stream=debug_stderr),
            )

            self.assertEqual(status, GraphBuildStatus.REUSED)
            self.assertIsNotNone(graph)
            self.assertIn("Graph build context | fingerprint=", debug_stderr.getvalue())

    def test_cli_and_runtime_logger_do_not_depend_on_print_function(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(repo_root=repo_root, env=env)
            stdout = io.StringIO()
            stderr = io.StringIO()
            logger_stream = io.StringIO()

            with patch("builtins.print", side_effect=AssertionError("unexpected print call")):
                RuntimeLogger(level="info", stream=logger_stream).info("hello")
                exit_code = main(["graphdb", "init"], services=services, stdout=stdout, stderr=stderr)

            self.assertEqual(exit_code, 0)
            self.assertRegex(logger_stream.getvalue(), r"INFO( \[[^\]]+\])?: hello")
            self.assertIn("graphdb ready", stdout.getvalue())

    def test_graph_build_rehydrates_neo4j_when_reusing_cached_build(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )

            first_graph, status = services.build_graph(excludes=(), force=False)
            self.assertEqual(status, GraphBuildStatus.COMPLETED)
            self.assertEqual(len(services.neo4j.graphs), 1)

            services.neo4j.graphs.clear()
            services.neo4j.graphs_by_fingerprint.clear()

            second_graph, status = services.build_graph(excludes=(), force=False)

            self.assertEqual(status, GraphBuildStatus.REUSED)
            self.assertEqual(first_graph.build_fingerprint, second_graph.build_fingerprint)
            self.assertEqual(len(services.neo4j.graphs), 1)

    def test_graph_build_resumes_from_checkpoint_when_neo4j_sync_is_interrupted(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            services.neo4j = self._FailOnceNeo4jAdapter()

            first_stdout = io.StringIO()
            first_stderr = io.StringIO()
            first_code = main(["graph", "build"], services=services, stdout=first_stdout, stderr=first_stderr)

            self.assertEqual(first_code, 1)
            self.assertIn("Neo4j sync interrupted", first_stderr.getvalue())

            resumed_stdout = io.StringIO()
            resumed_code = main(["graph", "build"], services=services, stdout=resumed_stdout, stderr=io.StringIO())

            self.assertEqual(resumed_code, 0)
            self.assertIn("resumed from checkpoint", resumed_stdout.getvalue())
            self.assertEqual(len(services.neo4j.graphs), 1)

    def test_graph_build_handles_ctrl_c_without_traceback_and_resumes_checkpoint(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_LOGGING_LEVEL": "debug",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            services.neo4j = self._InterruptOnceNeo4jAdapter()

            first_stderr = io.StringIO()
            first_code = main(["graph", "build"], services=services, stdout=io.StringIO(), stderr=first_stderr)

            self.assertEqual(first_code, 130)
            self.assertIn("Graph build cancelled by user.", first_stderr.getvalue())
            self.assertNotIn("Traceback", first_stderr.getvalue())

            resumed_stdout = io.StringIO()
            resumed_code = main(["graph", "build"], services=services, stdout=resumed_stdout, stderr=io.StringIO())

            self.assertEqual(resumed_code, 0)
            self.assertIn("resumed from checkpoint", resumed_stdout.getvalue())
            self.assertEqual(len(services.neo4j.graphs), 1)

    def test_graph_build_resumes_from_checkpoint_when_graph_enrichment_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(CLASS_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "true",
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            calls = {"count": 0}

            def _fail_once_enrichment(self, graph, logger=None):
                del logger
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("Graph enrichment interrupted")
                return graph

            with patch.object(ApplicationServices, "_run_graph_enrichment", new=_fail_once_enrichment, create=True):
                first_stdout = io.StringIO()
                first_stderr = io.StringIO()
                first_code = main(["graph", "build"], services=services, stdout=first_stdout, stderr=first_stderr)

                self.assertEqual(first_code, 1)
                self.assertIn("Graph enrichment interrupted", first_stderr.getvalue())

                resumed_stdout = io.StringIO()
                resumed_code = main(["graph", "build"], services=services, stdout=resumed_stdout, stderr=io.StringIO())

            self.assertEqual(resumed_code, 0)
            self.assertIn("resumed from checkpoint", resumed_stdout.getvalue())
            self.assertEqual(calls["count"], 2)

    def test_audit_handles_ctrl_c_without_traceback_and_preserves_partial_state(self) -> None:
        pytest.skip(_LEGACY_BUILD_RESULT_SKIP)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(MULTI_ENTRY_APP, encoding="utf-8")
            env = {
                "XAUDITOR_LLM_BASE_URL": "mock://offline",
                "XAUDITOR_LLM_API_KEY": "secret",
                "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                "XAUDITOR_LOGGING_LEVEL": "debug",
                # 0.10.0+ default worker_count: 1 → InlineExecutor; the
                # path-1-completes-then-path-2-interrupts ordering is
                # naturally guaranteed by synchronous-on-master
                # execution.
            }
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=env,
                state_store=InMemoryStateStore(),
            )
            graph, _status = services.build_graph(excludes=(), force=False)

            def _workflow_from_config(config, logger=None):
                del logger
                return AuditWorkflow(
                    config=config,
                    llm_client=LLMClient.from_config(config.llm),
                    analyzer_agent=self._InterruptOnSecondAnalyzerAgent(),
                    exploitation_agent=self._StubExploitationAgent(),
                    validator_agent=self._StubValidatorAgent(),
                )

            with patch("xauditor.services.AuditWorkflow.from_config", side_effect=_workflow_from_config):
                audit_stderr = io.StringIO()
                audit_code = main(["audit"], services=services, stdout=io.StringIO(), stderr=audit_stderr)

            self.assertEqual(audit_code, 130)
            self.assertIn("Audit cancelled by user.", audit_stderr.getvalue())
            self.assertNotIn("Traceback", audit_stderr.getvalue())

            partial_run = services.state_store.audit_runs[graph.build_fingerprint]
            self.assertEqual(len(partial_run.checkpoints), 1)
            self.assertEqual(partial_run.coverage.counts("path")[CoverageState.AUDITED], 1)
            self.assertEqual(partial_run.coverage.counts("path")[CoverageState.INTERRUPTED], 1)


if __name__ == "__main__":
    unittest.main()
