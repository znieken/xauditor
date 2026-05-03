"""Provider-kind yaml parsing — `llm.providers.<name>.kind`.

Covers `add-anthropic-provider-kind`: ``kind: openai`` (default) and
``kind: anthropic`` parse cleanly, mixed-case normalises to lowercase,
unknown values raise ``ConfigError`` naming the valid options.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.config import (
    ConfigError,
    LLM_THINKING_EFFORTS,
    PROVIDER_KIND_ANTHROPIC,
    PROVIDER_KIND_OPENAI,
    PROVIDER_KINDS,
    load_config,
)


def _write_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


_BASE_PROVIDER = """
llm:
  default_provider: p1
  providers:
    p1:
      base_url: mock://p1
      api_key: k
      model_name: m
"""


class ProviderKindLoadingTests(unittest.TestCase):
    def test_kind_omitted_defaults_to_openai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(repo_root, _BASE_PROVIDER)
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.llm.providers["p1"].kind, PROVIDER_KIND_OPENAI)

    def test_explicit_openai_kind_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: openai
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.llm.providers["p1"].kind, PROVIDER_KIND_OPENAI)

    def test_anthropic_kind_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: anthropic
                      base_url: https://api.anthropic.com
                      api_key: sk-ant-fake
                      model_name: claude-sonnet-4-5
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(
                config.llm.providers["p1"].kind, PROVIDER_KIND_ANTHROPIC
            )

    def test_kind_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: ANTHROPIC
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(
                config.llm.providers["p1"].kind, PROVIDER_KIND_ANTHROPIC
            )

    def test_unknown_kind_raises_with_valid_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: bedrock
                      base_url: mock://p1
                      api_key: k
                      model_name: m
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            message = str(ctx.exception)
            self.assertIn("p1", message)
            self.assertIn("bedrock", message)
            for valid_kind in PROVIDER_KINDS:
                self.assertIn(valid_kind, message)

    def test_thinking_effort_omitted_defaults_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(repo_root, _BASE_PROVIDER)
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIsNone(config.llm.providers["p1"].thinking_effort)

    def test_thinking_effort_high_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: anthropic
                      base_url: https://api.anthropic.com
                      api_key: sk-ant-fake
                      model_name: claude-sonnet-4-7
                      thinking_effort: high
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.llm.providers["p1"].thinking_effort, "high")

    def test_thinking_effort_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: anthropic
                      base_url: https://api.anthropic.com
                      api_key: sk-ant-fake
                      model_name: claude-sonnet-4-7
                      thinking_effort: MEDIUM
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.llm.providers["p1"].thinking_effort, "medium")

    def test_unknown_thinking_effort_raises_with_valid_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: anthropic
                      base_url: https://api.anthropic.com
                      api_key: sk-ant-fake
                      model_name: claude-sonnet-4-7
                      thinking_effort: turbo
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            message = str(ctx.exception)
            self.assertIn("p1", message)
            self.assertIn("turbo", message)
            for effort in LLM_THINKING_EFFORTS:
                self.assertIn(effort, message)

    def test_repr_includes_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                llm:
                  default_provider: p1
                  providers:
                    p1:
                      kind: anthropic
                      base_url: mock://p1
                      api_key: secret-value
                      model_name: m
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            text = repr(config.llm.providers["p1"])
            self.assertIn("kind='anthropic'", text)
            # api_key SHALL still be redacted in repr.
            self.assertNotIn("secret-value", text)


if __name__ == "__main__":
    unittest.main()
