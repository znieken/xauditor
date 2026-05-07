"""Yaml parsing for `llm.providers.<name>.{max_tokens, thinking_budget_tokens}`.

Covers the operator-tunable Anthropic token ceilings: omitted fields
fall back to the model factory defaults, explicit positive integers
parse, the legacy top-level ``llm.max_tokens`` / ``llm.thinking_budget_tokens``
shape lands on the default provider, and any non-positive / non-numeric
input fails at config-parse time naming the dot path.
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


class TokenLimitLoadingTests(unittest.TestCase):
    def test_omitted_fields_default_to_none(self) -> None:
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
            self.assertIsNone(provider.max_tokens)
            self.assertIsNone(provider.thinking_budget_tokens)

    def test_explicit_values_parse(self) -> None:
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
                      max_tokens: 128000
                      thinking_budget_tokens: 32000
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            provider = config.llm.providers["p1"]
            self.assertEqual(provider.max_tokens, 128_000)
            self.assertEqual(provider.thinking_budget_tokens, 32_000)

    def test_zero_or_negative_max_tokens_rejected_with_dot_path(self) -> None:
        for bad in (0, -1, "0", "-100"):
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
                              max_tokens: {bad}
                        """,
                    )
                    with self.assertRaises(ConfigError) as ctx:
                        load_config(repo_root=repo_root, env={"HOME": tmp})
                    self.assertIn(
                        "llm.providers.p1.max_tokens", str(ctx.exception)
                    )

    def test_zero_or_negative_thinking_budget_rejected(self) -> None:
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
                      thinking_budget_tokens: 0
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn(
                "llm.providers.p1.thinking_budget_tokens", str(ctx.exception)
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
                      max_tokens: unlimited
                """,
            )
            with self.assertRaises(ConfigError):
                load_config(repo_root=repo_root, env={"HOME": tmp})

    def test_legacy_top_level_lands_on_default_provider(self) -> None:
        # Legacy single-provider shape — top-level ``llm.max_tokens`` and
        # ``llm.thinking_budget_tokens`` merge into the default provider,
        # mirroring the existing behaviour for ``request_timeout_seconds``.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  base_url: mock://default
                  api_key: k
                  model_name: m
                  max_tokens: 64000
                  thinking_budget_tokens: 16000
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            provider = config.llm.providers["default"]
            self.assertEqual(provider.max_tokens, 64_000)
            self.assertEqual(provider.thinking_budget_tokens, 16_000)

    def test_env_overrides_apply_to_default_provider(self) -> None:
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
            config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": tmp,
                    "XAUDITOR_LLM_MAX_TOKENS": "32000",
                    "XAUDITOR_LLM_THINKING_BUDGET_TOKENS": "8000",
                },
            )
            provider = config.llm.providers["p1"]
            self.assertEqual(provider.max_tokens, 32_000)
            self.assertEqual(provider.thinking_budget_tokens, 8_000)


if __name__ == "__main__":
    unittest.main()
