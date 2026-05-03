from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import AgentLLMOverride, LLMConfig, LLMSettings
from xauditor.config import load_config
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.model_factory import (
    LangChainChatModel,
    MockChatModel,
    _strip_markdown_fences,
    build_chat_model,
    resolve_chat_model,
)


class ModelFactoryTests(unittest.TestCase):
    def test_pyproject_declares_langchain_openai_dependency(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        self.assertTrue(
            any(str(item).startswith("langchain-openai") for item in dependencies),
            "pyproject.toml must declare langchain-openai for real provider execution.",
        )

    def test_build_chat_model_returns_mock_for_mock_base_url(self) -> None:
        provider = LLMConfig(base_url="mock://offline", api_key="k", model_name="m")
        model = build_chat_model(provider, provider_name="shared")
        self.assertIsInstance(model, MockChatModel)
        self.assertTrue(model.is_mock)
        self.assertEqual(model.provider_name, "shared")
        self.assertEqual(model.model_name, "m")

    def test_build_chat_model_returns_mock_for_empty_base_url(self) -> None:
        provider = LLMConfig(base_url="", api_key="", model_name="")
        model = build_chat_model(provider, provider_name="default")
        self.assertIsInstance(model, MockChatModel)
        self.assertEqual(model.model_name, "mock-model")

    def test_build_chat_model_returns_langchain_for_real_base_url(self) -> None:
        provider = LLMConfig(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model_name="real-model",
            thinking_enabled=True,
        )
        model = build_chat_model(provider, provider_name="real")
        self.assertIsInstance(model, LangChainChatModel)
        self.assertFalse(model.is_mock)
        self.assertEqual(model.provider_name, "real")
        self.assertEqual(model.base_url, "https://api.example.com/v1")
        self.assertEqual(model.api_key, "sk-test")
        self.assertTrue(model.thinking_enabled)

    def test_resolve_chat_model_selects_agent_provider_with_fallback(self) -> None:
        settings = LLMSettings(
            default_provider="shared",
            providers={
                "shared": LLMConfig(base_url="mock://shared", api_key="k", model_name="shared-model"),
                "graph_specialist": LLMConfig(base_url="mock://graph", api_key="k", model_name="graph-model"),
            },
            agent_overrides={"graph_builder": AgentLLMOverride(provider="graph_specialist")},
        )
        graph_model = resolve_chat_model(settings, agent="graph_builder")
        auditor_model = resolve_chat_model(settings, agent="auditor")
        default_model = resolve_chat_model(settings)

        self.assertEqual(graph_model.provider_name, "graph_specialist")
        self.assertEqual(graph_model.model_name, "graph-model")
        self.assertEqual(auditor_model.provider_name, "shared")
        self.assertEqual(auditor_model.model_name, "shared-model")
        self.assertEqual(default_model.provider_name, "shared")

    def test_mock_chat_model_invoke_returns_deterministic_json(self) -> None:
        model = MockChatModel(provider_name="p", model_name="m")
        payload = {"function_name": "foo", "file_path": "x.py"}
        result = model.invoke_json("Summarize functions.", payload)
        self.assertIn("summary", result)
        self.assertIn("business_context", result)
        self.assertEqual(result, model.invoke_json("Summarize functions.", payload))

    def test_graph_builder_from_config_uses_graph_builder_provider_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "xauditor.yml").write_text(
                """
llm:
  default_provider: shared
  providers:
    shared:
      base_url: mock://shared
      api_key: shared-key
      model_name: shared-model
      thinking_enabled: false
    graph_specialist:
      base_url: mock://graph
      api_key: graph-key
      model_name: graph-model
      thinking_enabled: true
agents:
  graph_builder:
    llm:
      provider: graph_specialist
""".strip(),
                encoding="utf-8",
            )

            config = load_config(repo_root=repo_root, env={}, require_llm=True)
            builder = LangChainGraphBuilder.from_config(config)

            self.assertEqual(builder.llm_client.model.provider_name, "graph_specialist")
            self.assertEqual(builder.llm_client.model.model_name, "graph-model")

    def test_audit_workflow_from_config_routes_auditor_and_validator_models_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "xauditor.yml").write_text(
                """
llm:
  default_provider: shared
  providers:
    shared:
      base_url: mock://shared
      api_key: shared-key
      model_name: shared-model
      thinking_enabled: false
    audit_specialist:
      base_url: mock://audit
      api_key: audit-key
      model_name: audit-model
      thinking_enabled: true
    validator_specialist:
      base_url: mock://validator
      api_key: validator-key
      model_name: validator-model
      thinking_enabled: false
agents:
  auditor:
    llm:
      provider: audit_specialist
  validator:
    llm:
      provider: validator_specialist
""".strip(),
                encoding="utf-8",
            )

            config = load_config(repo_root=repo_root, env={}, require_llm=True)
            workflow = AuditWorkflow.from_config(config)

            self.assertEqual(workflow.llm_client.model.provider_name, "audit_specialist")
            self.assertEqual(workflow.analyzer_agent.chat_model.provider_name, "audit_specialist")
            self.assertEqual(workflow.exploitation_agent.chat_model.provider_name, "audit_specialist")
            self.assertEqual(workflow.validator_agent.chat_model.provider_name, "validator_specialist")

    def test_llm_client_does_not_use_urllib_request_module(self) -> None:
        import xauditor.llm as llm_module

        source = Path(llm_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "urllib",
            source,
            "LLMClient must route non-mock calls through the shared model factory, not urllib.",
        )


class StripMarkdownFencesTests(unittest.TestCase):
    def test_strips_json_tagged_fence(self) -> None:
        wrapped = '```json\n{"summary": "x"}\n```'
        self.assertEqual(_strip_markdown_fences(wrapped), '{"summary": "x"}')

    def test_strips_uppercase_json_tagged_fence(self) -> None:
        wrapped = '```JSON\n{"verdict": "Valid"}\n```'
        self.assertEqual(_strip_markdown_fences(wrapped), '{"verdict": "Valid"}')

    def test_strips_untagged_fence(self) -> None:
        wrapped = '```\n{"verdict": "Valid"}\n```'
        self.assertEqual(_strip_markdown_fences(wrapped), '{"verdict": "Valid"}')

    def test_strips_single_line_fence(self) -> None:
        wrapped = '```{"a": 1}```'
        self.assertEqual(_strip_markdown_fences(wrapped), '{"a": 1}')

    def test_tolerates_outer_whitespace(self) -> None:
        wrapped = '   \n```json\n{"a": 1}\n```\n  '
        self.assertEqual(_strip_markdown_fences(wrapped), '{"a": 1}')

    def test_passthrough_for_plain_json(self) -> None:
        plain = '{"summary": "x"}'
        self.assertEqual(_strip_markdown_fences(plain), plain)

    def test_passthrough_for_truncated_fence_keeps_input(self) -> None:
        # Opening fence but no closing fence (LLM cut off mid-stream).
        # The strip MUST refuse to mangle this so the existing
        # JSONDecodeError + _repair_json_typos chain still fires.
        truncated = '```json\n{"summary": "abc'
        self.assertEqual(_strip_markdown_fences(truncated), truncated)

    def test_passthrough_does_not_eat_inner_backticks(self) -> None:
        # A JSON body that legitimately contains ``` inside a string
        # value must not have its inner content consumed.
        plain = '{"snippet": "use ``` to fence"}'
        self.assertEqual(_strip_markdown_fences(plain), plain)


class InvokeJsonFenceStripIntegrationTests(unittest.TestCase):
    """Verify both LangChain JSON adapters route through the helper.

    We assert via source inspection rather than a live model invocation
    so the test stays hermetic (no langchain_anthropic / langchain_openai
    dependency at test time).
    """

    def test_openai_compat_invoke_json_uses_strip(self) -> None:
        import xauditor.model_factory as mf

        source = Path(mf.__file__).read_text(encoding="utf-8")
        # The OpenAI-compat adapter (LangChainChatModel) wraps
        # invoke_text with _strip_markdown_fences.
        self.assertIn("_strip_markdown_fences(self.invoke_text", source)

    def test_anthropic_invoke_json_uses_strip(self) -> None:
        import xauditor.model_factory as mf

        source = Path(mf.__file__).read_text(encoding="utf-8")
        # The Anthropic adapter does the same, just with line wrapping
        # because the call is split across lines for readability.
        self.assertIn("_strip_markdown_fences(", source)
        self.assertIn("_repair_json_typos", source)


if __name__ == "__main__":
    unittest.main()
