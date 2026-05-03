"""Yaml parsing + resolution for `llm.providers.<name>.request_timeout_seconds`.

Covers `add-llm-request-timeout-config`: omitted = SDK default,
explicit positive value parses, agent override overlays, and any
non-positive / non-finite / non-numeric input fails at config-parse.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.config import ConfigError, load_config


def _write_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


class RequestTimeoutLoadingTests(unittest.TestCase):
    def test_omitted_field_defaults_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
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
            self.assertIsNone(provider.request_timeout_seconds)
            self.assertIsNone(config.llm.request_timeout_for(None))

    def test_explicit_value_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                      request_timeout_seconds: 1800
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(
                config.llm.providers["p1"].request_timeout_seconds, 1800.0
            )
            self.assertEqual(config.llm.request_timeout_for(None), 1800.0)

    def test_zero_or_negative_rejected_with_dot_path(self) -> None:
        for bad in (0, -1, "0", "-30"):
            with self.subTest(value=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    _write_yaml(
                        repo_root,
                        f"""
                        llm:
                          default_provider: p1
                          providers:
                            p1:
                              base_url: mock://p1
                              api_key: k
                              model_name: m
                              request_timeout_seconds: {bad}
                        """,
                    )
                    with self.assertRaises(ConfigError) as ctx:
                        load_config(repo_root=repo_root, env={"HOME": tmp})
                    self.assertIn(
                        "llm.providers.p1.request_timeout_seconds",
                        str(ctx.exception),
                    )

    def test_non_numeric_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                      request_timeout_seconds: forever
                """,
            )
            with self.assertRaises(ConfigError):
                load_config(repo_root=repo_root, env={"HOME": tmp})


class AgentOverrideTimeoutTests(unittest.TestCase):
    def test_agent_override_overlays_provider_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                      request_timeout_seconds: 600
                agents:
                  validator:
                    llm:
                      request_timeout_seconds: 1800
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.llm.request_timeout_for("validator"), 1800.0)
            self.assertEqual(config.llm.request_timeout_for("auditor"), 600.0)

    def test_agent_override_only_field_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                agents:
                  validator:
                    llm:
                      request_timeout_seconds: 900
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.llm.request_timeout_for("validator"), 900.0)
            self.assertIsNone(config.llm.request_timeout_for("auditor"))

    def test_legacy_top_level_field_lands_on_default_provider(self) -> None:
        # Legacy single-provider yaml shape — top-level
        # ``llm.request_timeout_seconds`` lands on the default provider.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  base_url: mock://default
                  api_key: k
                  model_name: m
                  request_timeout_seconds: 1500
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(
                config.llm.providers["default"].request_timeout_seconds, 1500.0
            )


if __name__ == "__main__":
    unittest.main()
