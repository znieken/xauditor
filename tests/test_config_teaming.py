from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import ConfigError, load_config


def _write_project_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")


class TeamingConfigTests(unittest.TestCase):
    def test_defaults_when_teaming_block_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertFalse(config.teaming.enabled)
            self.assertEqual(config.teaming.validator.debate_rounds, 5)
            self.assertEqual(config.teaming.analyzer.provider_list, ())

    def test_enabled_teaming_with_all_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                    p2:
                      base_url: mock://p2
                      api_key: k
                      model_name: m
                teaming:
                  enabled: true
                  analyzer:
                    subagent_count: 3
                    provider_list: [p1, p2]
                  validator:
                    subagent_count: 2
                    provider_list: [p1]
                    debate_rounds: 7
                  exploiter:
                    subagent_count: 2
                    provider_list: [p2]
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertTrue(config.teaming.enabled)
            self.assertEqual(config.teaming.analyzer.subagent_count, 3)
            self.assertEqual(config.teaming.analyzer.provider_list, ("p1", "p2"))
            self.assertEqual(config.teaming.validator.debate_rounds, 7)
            self.assertEqual(config.teaming.exploiter.provider_list, ("p2",))

    def test_env_overrides_teaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                    p2:
                      base_url: mock://p2
                      api_key: k
                      model_name: m
                teaming:
                  enabled: false
                  analyzer:
                    subagent_count: 1
                    provider_list: [p1]
                  validator:
                    subagent_count: 1
                    provider_list: [p1]
                  exploiter:
                    subagent_count: 1
                    provider_list: [p1]
                """,
            )
            env = {
                "HOME": tmp,
                "XAUDITOR_TEAMING_ENABLED": "true",
                "XAUDITOR_TEAMING_VALIDATOR_DEBATE_ROUNDS": "3",
                "XAUDITOR_TEAMING_EXPLOITER_PROVIDER_LIST": "p1,p2",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertTrue(config.teaming.enabled)
            self.assertEqual(config.teaming.validator.debate_rounds, 3)
            self.assertEqual(config.teaming.exploiter.provider_list, ("p1", "p2"))

    def test_enabled_without_required_fields_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                teaming:
                  enabled: true
                  analyzer:
                    subagent_count: 1
                    provider_list: [p1]
                  validator:
                    subagent_count: 1
                    provider_list: [p1]
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("teaming.exploiter", str(ctx.exception))

    def test_unknown_provider_in_list_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                teaming:
                  enabled: true
                  analyzer:
                    subagent_count: 1
                    provider_list: [p1, unknown]
                  validator:
                    subagent_count: 1
                    provider_list: [p1]
                  exploiter:
                    subagent_count: 1
                    provider_list: [p1]
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            message = str(ctx.exception)
            self.assertIn("unknown", message.lower())
            self.assertIn("teaming.analyzer.provider_list", message)

    def test_non_positive_subagent_count_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                teaming:
                  enabled: true
                  analyzer:
                    subagent_count: 0
                    provider_list: [p1]
                  validator:
                    subagent_count: 1
                    provider_list: [p1]
                  exploiter:
                    subagent_count: 1
                    provider_list: [p1]
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("teaming.analyzer.subagent_count", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
