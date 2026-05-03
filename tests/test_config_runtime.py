from __future__ import annotations

import os
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import ConfigError, load_config
from xauditor.graph.cache import FileStateStore
from xauditor.graph.canonical import DiscoveredFile
from xauditor.models import ConfidenceLevel, CoverageState, ValidationStatus
from xauditor.runtime import RuntimeLayout


class ConfigRuntimeTests(unittest.TestCase):
    def test_load_config_applies_cli_env_project_user_default_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            home_root = Path(tmp) / "home"
            user_config_path = home_root / ".xauditor" / "xauditor.yml"
            user_config_path.parent.mkdir(parents=True)
            user_config_path.write_text(
                textwrap.dedent(
                    """
                    llm:
                      default_provider: shared
                      providers:
                        shared:
                          base_url: https://user.example/v1
                          api_key: user-key
                          model_name: user-model
                          thinking_enabled: false
                    runtime:
                      root_dir: .user-runtime
                    repository:
                      excludes:
                        - vendor
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    graph:
                      db:
                        image: neo4j:5-community
                        password: project-secret
                    llm:
                      providers:
                        shared:
                          base_url: https://project.example/v1
                          api_key: project-key
                          model_name: project-model
                          thinking_enabled: false
                    runtime:
                      root_dir: .project-runtime
                    repository:
                      excludes:
                        - build
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            env = {
                "HOME": str(home_root),
                "XAUDITOR_LLM_API_KEY": "env-key",
                "XAUDITOR_RUNTIME_ROOT_DIR": ".env-runtime",
                "XAUDITOR_REPOSITORY_EXCLUDES": "dist,.cache",
            }
            cli_overrides = {
                "llm.model_name": "cli-model",
                "graph.db.password": "cli-secret",
            }

            config = load_config(
                repo_root=repo_root,
                config_path=config_path,
                env=env,
                cli_overrides=cli_overrides,
                require_llm=True,
            )

            self.assertEqual(config.llm.default_provider, "shared")
            self.assertEqual(config.llm.base_url, "https://project.example/v1")
            self.assertEqual(config.llm.api_key, "env-key")
            self.assertEqual(config.llm.model_name, "cli-model")
            self.assertEqual(config.graphdb.password, "cli-secret")
            self.assertEqual(config.runtime.root_dir, repo_root / ".env-runtime")
            self.assertEqual(config.repository.excludes, ("dist", ".cache"))

    def test_missing_required_llm_values_raise_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            home_root = repo_root / "home"
            home_root.mkdir()
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": str(home_root)}, require_llm=True)

            self.assertIn("llm.default_provider", str(ctx.exception))

    def test_load_config_supports_named_provider_selection_and_default_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    llm:
                      default_provider: shared
                      providers:
                        shared:
                          base_url: https://shared.example/v1
                          api_key: shared-key
                          model_name: shared-model
                          thinking_enabled: false
                        graph_specialist:
                          base_url: https://graph.example/v1
                          api_key: graph-key
                          model_name: graph-model
                          thinking_enabled: true
                        audit_specialist:
                          base_url: https://audit.example/v1
                          api_key: audit-key
                          model_name: audit-model
                          thinking_enabled: false
                    agents:
                      graph_builder:
                        llm:
                          provider: graph_specialist
                      auditor:
                        llm:
                          provider: audit_specialist
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(repo_root=repo_root, config_path=config_path, require_llm=True)

            self.assertEqual(config.llm.default_provider, "shared")
            self.assertEqual(config.llm.base_url, "https://shared.example/v1")
            self.assertFalse(config.llm.thinking_enabled)
            self.assertEqual(config.llm.provider_for("graph_builder").model_name, "graph-model")
            self.assertTrue(config.llm.provider_for("graph_builder").thinking_enabled)
            self.assertEqual(config.llm.provider_for("auditor").model_name, "audit-model")
            self.assertEqual(config.llm.provider_for("validator").model_name, "shared-model")

    def test_unknown_agent_provider_selection_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    llm:
                      default_provider: shared
                      providers:
                        shared:
                          base_url: https://shared.example/v1
                          api_key: shared-key
                          model_name: shared-model
                          thinking_enabled: false
                    agents:
                      validator:
                        llm:
                          provider: missing
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, config_path=config_path, require_llm=True)

            self.assertIn("missing", str(ctx.exception))
            self.assertIn("shared", str(ctx.exception))

    def test_load_config_applies_logging_level_precedence_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    logging:
                      level: warning
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            config = load_config(
                repo_root=repo_root,
                config_path=config_path,
                env={"XAUDITOR_LOGGING_LEVEL": "debug"},
                cli_overrides={"logging.level": "error"},
            )

            self.assertEqual(config.logging.level, "error")

        with tempfile.TemporaryDirectory() as tmp:
            home_root = Path(tmp) / "home"
            home_root.mkdir()
            self.assertEqual(load_config(repo_root=Path(tmp), env={"HOME": str(home_root)}).logging.level, "info")

    def test_invalid_logging_level_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text("logging:\n  level: verbose\n", encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, config_path=config_path)

            self.assertIn("logging.level", str(ctx.exception))
            self.assertIn("error", str(ctx.exception))
            self.assertIn("debug", str(ctx.exception))

    def test_logging_file_loads_from_yaml_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                "logging:\n  level: info\n  file: logs/audit.log\n",
                encoding="utf-8",
            )
            config = load_config(repo_root=repo_root, config_path=config_path)
            self.assertEqual(config.logging.file, "logs/audit.log")

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(
                repo_root=repo_root,
                env={"XAUDITOR_LOGGING_FILE": "from-env.log"},
            )
            self.assertEqual(config.logging.file, "from-env.log")

    def test_logging_file_defaults_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            home_root = repo_root / "home"
            home_root.mkdir()
            config = load_config(repo_root=repo_root, env={"HOME": str(home_root)})
            self.assertIsNone(config.logging.file)

    def test_graph_build_enable_llm_enrichment_defaults_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            home_root = repo_root / "home"
            home_root.mkdir()
            config = load_config(repo_root=repo_root, env={"HOME": str(home_root)})
            self.assertTrue(config.graph.build.enable_llm_enrichment)

    def test_graph_build_enable_llm_enrichment_yaml_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                "graph:\n  build:\n    enable_llm_enrichment: false\n",
                encoding="utf-8",
            )
            config = load_config(repo_root=repo_root, config_path=config_path)
            self.assertFalse(config.graph.build.enable_llm_enrichment)

    def test_graph_build_enable_llm_enrichment_env_and_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            home_root = repo_root / "home"
            home_root.mkdir()
            config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": str(home_root),
                    "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "false",
                },
            )
            self.assertFalse(config.graph.build.enable_llm_enrichment)

            cli_config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": str(home_root),
                    "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "false",
                },
                cli_overrides={"graph.build.enable_llm_enrichment": True},
            )
            self.assertTrue(cli_config.graph.build.enable_llm_enrichment)

    def test_graph_build_enable_llm_enrichment_invalid_value_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "xauditor.yml"
            config_path.write_text(
                "graph:\n  build:\n    enable_llm_enrichment: maybe\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, config_path=config_path)
            self.assertIn("graph.build.enable_llm_enrichment", str(ctx.exception))

    def test_runtime_layout_creates_expected_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            layout = RuntimeLayout.from_root(repo_root / ".xauditor")
            layout.ensure()

            self.assertTrue(layout.root_dir.exists())
            self.assertTrue(layout.cache_dir.exists())
            self.assertTrue(layout.manifests_dir.exists())
            self.assertTrue(layout.reports_dir.exists())

    def test_graph_build_manifest_lists_pending_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeLayout.from_root(Path(tmp) / ".xauditor").ensure()
            store = FileStateStore(runtime)
            fingerprint = "build-1"

            store.save_inventory_stage(
                fingerprint,
                files=(DiscoveredFile(path="app.py", module_name="app", language="python"),),
                agent_state={"inventory": True},
            )

            manifest_path = runtime.cache_dir / "graph-builds" / fingerprint / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["completed_stages"], ["inventory"])
            self.assertEqual(
                manifest["pending_stages"],
                ["reference_tracer", "graph_synthesizer", "canonical_finalize"],
            )

    def test_domain_enums_match_open_spec_contract(self) -> None:
        self.assertEqual(
            {member.value for member in ConfidenceLevel},
            {"High", "Medium", "Low"},
        )
        self.assertEqual(
            {member.value for member in CoverageState},
            {"audited", "not_audited", "excluded", "interrupted", "failed"},
        )
        self.assertEqual(
            {member.value for member in ValidationStatus},
            {"Valid", "Partial Valid", "Inconclusive", "False Positive"},
        )


if __name__ == "__main__":
    unittest.main()
