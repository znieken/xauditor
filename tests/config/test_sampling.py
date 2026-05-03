from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.config import ConfigError, load_config


def _write_project_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


_BASE_PROVIDER_YAML = """
llm:
  default_provider: p1
  providers:
    p1:
      base_url: mock://p1
      api_key: k
      model_name: m
      temperature: 0.2
      top_p: 0.9
      top_k: 40
      repetition_penalty: 1.05
"""


class SamplingLoadingTests(unittest.TestCase):
    def test_provider_sampling_loaded_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_PROVIDER_YAML)
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            provider = config.llm.providers["p1"]
            self.assertAlmostEqual(provider.temperature, 0.2)
            self.assertAlmostEqual(provider.top_p, 0.9)
            self.assertEqual(provider.top_k, 40)
            self.assertAlmostEqual(provider.repetition_penalty, 1.05)

    def test_missing_sampling_fields_default_to_none(self) -> None:
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
            provider = config.llm.providers["p1"]
            self.assertIsNone(provider.temperature)
            self.assertIsNone(provider.top_p)
            self.assertIsNone(provider.top_k)
            self.assertIsNone(provider.repetition_penalty)

    def test_agent_override_overlays_provider_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_PROVIDER_YAML
                + textwrap.dedent(
                    """
                    agents:
                      auditor:
                        llm:
                          temperature: 0.7
                          top_k: 20
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            sampling = config.llm.sampling_for("auditor")
            self.assertAlmostEqual(sampling["temperature"], 0.7)
            self.assertAlmostEqual(sampling["top_p"], 0.9)
            self.assertEqual(sampling["top_k"], 20)
            self.assertAlmostEqual(sampling["repetition_penalty"], 1.05)
            other_sampling = config.llm.sampling_for("validator")
            self.assertAlmostEqual(other_sampling["temperature"], 0.2)
            self.assertEqual(other_sampling["top_k"], 40)

    def test_provider_env_var_overrides_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_PROVIDER_YAML)
            env = {
                "HOME": tmp,
                "XAUDITOR_LLM_PROVIDERS_P1_TEMPERATURE": "0.35",
                "XAUDITOR_LLM_PROVIDERS_P1_TOP_K": "10",
            }
            config = load_config(repo_root=repo_root, env=env)
            provider = config.llm.providers["p1"]
            self.assertAlmostEqual(provider.temperature, 0.35)
            self.assertEqual(provider.top_k, 10)
            self.assertAlmostEqual(provider.top_p, 0.9)

    def test_agent_env_var_overrides_yaml_and_creates_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_PROVIDER_YAML)
            env = {
                "HOME": tmp,
                "XAUDITOR_AGENTS_VALIDATOR_LLM_TEMPERATURE": "0.0",
                "XAUDITOR_AGENTS_VALIDATOR_LLM_REPETITION_PENALTY": "1.2",
            }
            config = load_config(repo_root=repo_root, env=env)
            override = config.llm.agent_overrides["validator"]
            self.assertEqual(override.temperature, 0.0)
            self.assertAlmostEqual(override.repetition_penalty, 1.2)
            self.assertIsNone(override.top_k)
            sampling = config.llm.sampling_for("validator")
            self.assertEqual(sampling["temperature"], 0.0)
            self.assertAlmostEqual(sampling["top_p"], 0.9)

    def test_empty_env_var_does_not_overwrite_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_PROVIDER_YAML)
            env = {
                "HOME": tmp,
                "XAUDITOR_LLM_PROVIDERS_P1_TEMPERATURE": "",
                "XAUDITOR_LLM_PROVIDERS_P1_TOP_P": "   ",
            }
            config = load_config(repo_root=repo_root, env=env)
            provider = config.llm.providers["p1"]
            self.assertAlmostEqual(provider.temperature, 0.2)
            self.assertAlmostEqual(provider.top_p, 0.9)

    def test_empty_env_var_does_not_create_agent_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_PROVIDER_YAML)
            env = {
                "HOME": tmp,
                "XAUDITOR_AGENTS_EXPLOITATION_LLM_TEMPERATURE": "",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertNotIn("exploitation", config.llm.agent_overrides)

    def test_env_overrides_take_precedence_over_yaml_agent_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_PROVIDER_YAML
                + textwrap.dedent(
                    """
                    agents:
                      auditor:
                        llm:
                          temperature: 0.7
                    """
                ),
            )
            env = {
                "HOME": tmp,
                "XAUDITOR_AGENTS_AUDITOR_LLM_TEMPERATURE": "0.1",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertAlmostEqual(
                config.llm.agent_overrides["auditor"].temperature, 0.1
            )
            sampling = config.llm.sampling_for("auditor")
            self.assertAlmostEqual(sampling["temperature"], 0.1)


class SamplingRangeValidationTests(unittest.TestCase):
    def _load_with_field(
        self, provider_body: str = "", agent_body: str = ""
    ):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            body = (
                "llm:\n"
                "  default_provider: p1\n"
                "  providers:\n"
                "    p1:\n"
                "      base_url: mock://p1\n"
                "      api_key: k\n"
                "      model_name: m\n"
                f"{provider_body}"
            )
            if agent_body:
                body += "agents:\n" + agent_body
            _write_project_yaml(repo_root, body)
            return load_config(
                repo_root=repo_root, env={"HOME": tmp}, require_llm=True
            )

    def test_negative_temperature_on_provider_rejected_with_dot_path(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      temperature: -0.1\n")
        self.assertIn("llm.providers.p1.temperature", str(ctx.exception))

    def test_zero_top_p_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      top_p: 0\n")
        self.assertIn("llm.providers.p1.top_p", str(ctx.exception))

    def test_negative_top_p_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      top_p: -0.2\n")
        self.assertIn("llm.providers.p1.top_p", str(ctx.exception))

    def test_top_p_greater_than_one_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      top_p: 1.5\n")
        self.assertIn("llm.providers.p1.top_p", str(ctx.exception))

    def test_zero_top_k_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      top_k: 0\n")
        self.assertIn("llm.providers.p1.top_k", str(ctx.exception))

    def test_negative_top_k_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      top_k: -5\n")
        self.assertIn("llm.providers.p1.top_k", str(ctx.exception))

    def test_non_integer_top_k_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      top_k: 1.5\n")
        self.assertIn("llm.providers.p1.top_k", str(ctx.exception))

    def test_zero_repetition_penalty_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      repetition_penalty: 0\n")
        self.assertIn(
            "llm.providers.p1.repetition_penalty", str(ctx.exception)
        )

    def test_negative_repetition_penalty_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(provider_body="      repetition_penalty: -0.5\n")
        self.assertIn(
            "llm.providers.p1.repetition_penalty", str(ctx.exception)
        )

    def test_agent_override_range_uses_agent_dot_path(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(
                agent_body="  validator:\n    llm:\n      top_p: 1.3\n",
            )
        self.assertIn("agents.validator.llm.top_p", str(ctx.exception))

    def test_agent_override_negative_temperature_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load_with_field(
                agent_body="  auditor:\n    llm:\n      temperature: -1\n",
            )
        self.assertIn("agents.auditor.llm.temperature", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
